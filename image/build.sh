#!/bin/bash
# Build the Tracer OS image with rpi-image-gen.
#
# Modelled on TrailCurrentHeadwaters/CM5/image/build.sh so the two products
# build the same way. Differences: one variant not two, Debian trixie not
# bookworm, and Pi 5 not CM5.
#
# Requirements:
#   - Debian/Ubuntu host, arm64 native OR x86_64 with qemu-user-static
#   - Run with sudo (rpi-image-gen chroots the target rootfs)
#   - ~10 GB free in work/
#
# THE DEVICE ACCOUNT IS SUPPLIED AT BUILD TIME. THERE IS NO DEFAULT.
#
# This script used to default to a fixed username and an identical password.
# That is a shipped credential: every unit built from this tree had the same
# console login, the value was in git forever, and anyone with the repo had it.
# A default that is never changed is not a placeholder, it is the password.
#
# Usage:
#   sudo ./build.sh                       # prompts for both (preferred)
#   sudo ./build.sh <username>            # prompts for the password only
#   TRACER_IMAGE_USER=… TRACER_IMAGE_PASSWORD=… sudo -E ./build.sh   # CI
#
# The password is NEVER accepted as a command-line argument. Arguments are
# visible to every user on the build host via `ps`, and land in shell history
# and CI logs. Supply it by prompt, or by environment variable when there is
# no terminal.
#
# Resolution order, most explicit first:
#   1. TRACER_IMAGE_USER / TRACER_IMAGE_PASSWORD in the environment
#   2. username as $1
#   3. an interactive prompt (password read silently, typed twice)
#   4. no terminal and nothing set: fail with instructions, never a default
#
# Output:
#   work/rpi-image-gen/work/tracer-os/tracer-os.img
#   deploy/tracer-os-<version>.img.xz + .sha256

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RPIIG_DIR="$SCRIPT_DIR/work/rpi-image-gen"
DEPLOY_DIR="$SCRIPT_DIR/deploy"

CONFIG="tracer-os"

if [ $# -gt 1 ]; then
    cat >&2 <<'EOF'
ERROR: the password may not be passed as an argument.

Command-line arguments are world-readable via `ps` on the build host and are
recorded in shell history and CI logs. Supply it one of these ways instead:

    sudo ./image/build.sh <username>        # prompts for the password
    sudo ./image/build.sh                   # prompts for both

    # non-interactive (CI). -E preserves the variables through sudo:
    TRACER_IMAGE_USER=builduser \
    TRACER_IMAGE_PASSWORD="$(read -rs -p 'pw: ' p; echo "$p")" \
      sudo -E ./image/build.sh
EOF
    exit 2
fi

# ── Preflight ───────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (rpi-image-gen chroots the rootfs)." >&2
    echo "  sudo $0 $*" >&2
    exit 1
fi

for tool in git openssl xz sha256sum make python3 node; do
    command -v "$tool" >/dev/null || { echo "ERROR: '$tool' not found." >&2; exit 1; }
done

# ── Prove the software works BEFORE spending 26 minutes on an image ─
# The daemon and GUI are copied into the rootfs wholesale, so a broken module
# or a UI file that does not parse is not discovered by the build at all — it
# is discovered after a flash, on hardware, by an operator staring at a blank
# panel. The suite takes seconds; the build takes half an hour and the
# flash-and-boot cycle takes more. Run it first, every time.
#
# PYTHONDONTWRITEBYTECODE stops this leaving root-owned __pycache__ directories
# scattered through a tree the developer then cannot clean without sudo.
echo "Running the test suite before building..."
if ! PYTHONDONTWRITEBYTECODE=1 make -C "$REPO_ROOT" test; then
    echo "" >&2
    echo "ERROR: tests failed — not building." >&2
    echo "" >&2
    echo "Fix them and re-run. Building on a red suite means finding out on" >&2
    echo "hardware, half an hour and a flash later, what was knowable now." >&2
    exit 1
fi
echo "Tests passed."
echo

if [ "$(uname -m)" != "aarch64" ] && [ ! -f /usr/bin/qemu-aarch64-static ]; then
    echo "ERROR: cross-building from $(uname -m) needs qemu-user-static." >&2
    echo "  sudo apt install qemu-user-static binfmt-support" >&2
    exit 1
fi

# The splash is a build input, not a build product — fail early and clearly
# rather than letting rpi-image-gen report a confusing missing-file error.
if [ ! -f "$SCRIPT_DIR/splash/tracer-splash.tga" ]; then
    echo "ERROR: splash/tracer-splash.tga missing." >&2
    echo "  Run: ./generate-splash.sh" >&2
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/overlays/tracer-gt911.dtbo" ]; then
    echo "ERROR: overlays/tracer-gt911.dtbo missing." >&2
    echo "  Run: make overlays   (from the repo root)" >&2
    exit 1
fi

# ── Stale mounts from an interrupted build ──────────────────────────
# An interrupted build (Ctrl-C, a kill, a crash) leaves the host's /proc and
# friends mounted inside the chroot. The NEXT build then reaches genimage,
# which does `cp -a chroot/filesystem -> staging`, walks into that live procfs
# and tries to copy /proc/kcore — a pseudo-file the size of physical memory.
# The disk fills, and the failure arrives ~20 minutes in as thousands of `cp`
# errors that name everything except the actual cause.
#
# Clean it up automatically. These mounts are inside this script's own scratch
# tree, created by this script's own previous run — nothing else on the host
# has any business there, so unmounting them needs no permission from the user.
# Making the operator do it by hand is how a 26-minute build turns into an
# afternoon of detective work.
#
# Deepest path first, or unmounting a parent fails while a child is still on it.
stale_mounts="$(awk -v d="$RPIIG_DIR/work/" 'index($5, d) == 1 { print $5 }' \
                /proc/self/mountinfo 2>/dev/null | sort -r || true)"
if [ -n "$stale_mounts" ]; then
    echo "Previous build left filesystems mounted; cleaning up:"
    printf '%s\n' "$stale_mounts" | while IFS= read -r m; do
        [ -n "$m" ] || continue
        echo "  umount $m"
        # Lazy only as a fallback, and it is the right fallback here: it
        # detaches the mount immediately so nothing can recurse from that path
        # into a live procfs, which is the only property the rm below needs.
        umount "$m" 2>/dev/null \
            || umount -l "$m" 2>/dev/null \
            || echo "    WARNING: could not unmount $m" >&2
    done

    # Anything still mounted means something outside this build is holding it.
    # Stop rather than risk cp/rm walking into a live filesystem.
    still="$(awk -v d="$RPIIG_DIR/work/" 'index($5, d) == 1 { print $5 }' \
             /proc/self/mountinfo 2>/dev/null || true)"
    if [ -n "$still" ]; then
        echo "ERROR: still mounted after cleanup:" >&2
        printf '%s\n' "$still" | sed 's/^/  /' >&2
        echo "Find the holder with:  sudo fuser -vm <path>" >&2
        exit 1
    fi

    # Mounts left behind mean the previous run died partway, so the chroot it
    # was building is of unknown completeness. Discard it: a stale chroot is
    # how a build silently produces an image missing whatever the dead run had
    # not yet installed. Costs a re-bootstrap, which is cheap next to shipping
    # a card that is wrong in a way nothing reports.
    for c in "$RPIIG_DIR"/work/chroot-*; do
        [ -e "$c" ] || continue
        echo "  discarding incomplete chroot $(basename "$c")"
        rm -rf "$c"
    done
fi

# ── Device account ──────────────────────────────────────────────────
# No defaults. See the header for why, and do not reintroduce one: a fallback
# here is indistinguishable from a shipped credential.

TC_USER="${TRACER_IMAGE_USER:-${1:-}}"
TC_PASS="${TRACER_IMAGE_PASSWORD:-}"

if [ -z "$TC_USER" ]; then
    if [ ! -t 0 ]; then
        echo "ERROR: no device username given and no terminal to ask on." >&2
        echo "  Pass it:  sudo ./image/build.sh <username>" >&2
        echo "  Or set:   TRACER_IMAGE_USER=<username> sudo -E ./image/build.sh" >&2
        exit 1
    fi
    printf 'Device account username: '
    read -r TC_USER
    [ -n "$TC_USER" ] || { echo "No username entered." >&2; exit 1; }
fi

# Reject names the image cannot actually create, before a 40-minute build.
case "$TC_USER" in
    root|daemon|bin|sys|nobody) echo "ERROR: '$TC_USER' is a system account." >&2; exit 1 ;;
esac
printf '%s' "$TC_USER" | grep -qE '^[a-z_][a-z0-9_-]{0,31}$' \
    || { echo "ERROR: '$TC_USER' is not a valid Linux username." >&2; exit 1; }

if [ -z "$TC_PASS" ]; then
    if [ ! -t 0 ]; then
        echo "ERROR: no device password set and no terminal to ask on." >&2
        echo "  Set TRACER_IMAGE_PASSWORD in the environment and use 'sudo -E'." >&2
        exit 1
    fi
    # -s so it is not echoed to the screen or captured by a scrollback buffer.
    printf 'Device account password for %s: ' "$TC_USER" >&2
    read -rs TC_PASS; echo >&2
    printf 'Repeat password: ' >&2
    read -rs TC_PASS2; echo >&2
    [ "$TC_PASS" = "$TC_PASS2" ] || { echo "ERROR: passwords do not match." >&2; exit 1; }
    unset TC_PASS2
fi

[ -n "$TC_PASS" ] || { echo "ERROR: the device password may not be empty." >&2; exit 1; }
# Refusing the username as the password blocks the single most likely weak
# choice — and it is exactly what this script used to ship as the default.
if [ "$TC_PASS" = "$TC_USER" ]; then
    echo "ERROR: the password must not equal the username." >&2
    exit 1
fi

# ── Fetch rpi-image-gen ─────────────────────────────────────────────
# PINNED, not tracking main.
#
# rpi-image-gen changes its own interface. This script was written against a
# version taking `-D <dir>` with bare key=value overrides; upstream renamed the
# flag to `-S` and now requires a `--` separator. Following main means the
# build breaks on someone else's schedule, and it breaks confusingly: the error
# names the directory that fell through as an override, not the flag that no
# longer exists.
#
# Bump deliberately, after checking `./rpi-image-gen build --help` in the new
# checkout and running a full build. Override for a one-off test with
# RPIIG_REF=<sha> sudo ./image/build.sh
RPIIG_REF="${RPIIG_REF:-cb909cb}"

if [ ! -d "$RPIIG_DIR" ]; then
    echo "Cloning rpi-image-gen..."
    mkdir -p "$(dirname "$RPIIG_DIR")"
    git clone https://github.com/raspberrypi/rpi-image-gen.git "$RPIIG_DIR"
fi

# Re-pin on EVERY run, not just after cloning: an existing checkout left on
# main by an earlier version of this script would otherwise stay there. The
# fetch is best-effort so a correctly pinned checkout still builds offline.
git -C "$RPIIG_DIR" fetch --quiet origin 2>/dev/null || true
if ! git -C "$RPIIG_DIR" -c advice.detachedHead=false \
        checkout --quiet "$RPIIG_REF" 2>/dev/null; then
    echo "ERROR: rpi-image-gen ref '$RPIIG_REF' not found in $RPIIG_DIR." >&2
    echo "  Delete image/work/rpi-image-gen and re-run to re-clone." >&2
    exit 1
fi
echo "rpi-image-gen pinned at $(git -C "$RPIIG_DIR" rev-parse --short HEAD)"

# -stdin, NOT `openssl passwd -6 "$TC_PASS"`. As an argument the plaintext
# password is visible in `ps` to every user on the build host for as long as
# openssl runs. Piping it keeps it off the process table entirely.
#
# The resulting HASH is still passed to rpi-image-gen on its command line
# below — that is the tool's only interface for it. A yescrypt/sha512 hash is
# not a plaintext leak, but treat a shared build host accordingly.
TC_PASSHASH="$(printf '%s' "$TC_PASS" | openssl passwd -6 -stdin)"
# Done with the plaintext; do not leave it in the environment for the child
# process tree (rpi-image-gen runs a great many hooks).
unset TC_PASS TRACER_IMAGE_PASSWORD

VERSION="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo "dev")"

# ── Build ───────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════"
echo " Tracer OS  ·  $CONFIG  ·  $VERSION"
echo " Debian trixie · Raspberry Pi 5 · 640x480 HDMI panel"
echo "════════════════════════════════════════════════════════════════"
echo
echo "NOTE: this image installs tracerd and tracer-ui and enables both units,"
echo "      so it boots to the splash and then the launcher. State is NOT"
echo "      carried over from a previous card: the Headwaters CA, the SSH key"
echo "      and settings.json are generated per unit under /var/lib/tracer."
echo

# ── Discard previous image output ───────────────────────────────────
# Exactly one flashable image must exist when this finishes.
#
# Builds take ~26 minutes, so a fix-and-rebuild cycle is slow enough that the
# previous image is still sitting there looking plausible when the new one
# lands. Worse, the names collide only sometimes: `git describe` gives
# c1bd446-dirty for every build off a dirty tree, but falls back to "dev" when
# it cannot run, so two builds of DIFFERENT code can produce two differently
# named images, both current-looking. That is how a stale card gets flashed and
# a fix gets diagnosed as not working — which cost most of a day.
#
# So: delete the previous outputs up front, not at the end. Doing it here means
# a build that fails leaves NOTHING flashable behind, which is the honest
# outcome — a failed build has no image, and an empty deploy/ says so plainly.
#
# Only build PRODUCTS are removed. The chroot is left alone: it is expensive to
# recreate and rpi-image-gen overwrites it anyway.
echo "Clearing previous image output..."
for stale in "$DEPLOY_DIR" "$RPIIG_DIR"/work/image-* "$RPIIG_DIR"/work/deploy-*; do
    [ -e "$stale" ] || continue
    # Refuse to walk into anything still mounted. The preflight above should
    # have caught it; this is the second line of defence, because an `rm -rf`
    # that recurses into a live /proc is far worse than a failed build.
    if awk -v d="$stale" 'index($5, d) == 1 { found = 1 } END { exit !found }' \
           /proc/self/mountinfo 2>/dev/null; then
        echo "ERROR: '$stale' has something mounted under it; refusing to remove it." >&2
        exit 1
    fi
    echo "  removing $(basename "$stale")"
    rm -rf "$stale"
done
mkdir -p "$DEPLOY_DIR"

cd "$RPIIG_DIR"
# -S, not -D: upstream renamed the custom-sources flag. And overrides MUST come
# after a bare `--`, or the first one is parsed as an option argument and the
# tool rejects the whole set with "Overrides must be provided as key=value
# pairs" — naming the directory, not the flag, which sends you looking in the
# wrong place. Check `./rpi-image-gen build --help` in the pinned checkout
# before changing any of this.
./rpi-image-gen build \
    -c "$SCRIPT_DIR/config/${CONFIG}.yaml" \
    -S "$SCRIPT_DIR" \
    -- \
    IGconf_device_user1="$TC_USER" \
    IGconf_device_user1passhash="$TC_PASSHASH" \
    IGconf_device_user1sudo=nopasswd

# ── Package ─────────────────────────────────────────────────────────
IMG="$(find "$RPIIG_DIR/work" -name "${CONFIG}.img" -print -quit)"
if [ -z "$IMG" ] || [ ! -f "$IMG" ]; then
    echo "ERROR: build finished but no ${CONFIG}.img was produced." >&2
    echo "  Look under $RPIIG_DIR/work/ for partial output." >&2
    exit 1
fi

mkdir -p "$DEPLOY_DIR"
OUT="$DEPLOY_DIR/tracer-os-${VERSION}.img.xz"

echo "Compressing $(basename "$IMG") -> $(basename "$OUT") ..."
xz -T0 -c "$IMG" > "$OUT"
( cd "$DEPLOY_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )

echo
echo "════════════════════════════════════════════════════════════════"
echo " Built: $OUT"
echo "        $(du -h "$OUT" | cut -f1)"
echo "        $(cat "${OUT}.sha256")"
echo "════════════════════════════════════════════════════════════════"
echo
echo "Flash it:"
echo "  xzcat $OUT | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  sync"
echo
echo "/dev/sdX is a placeholder — get the real device from lsblk, and"
echo "check it twice. See docs/building.md."
