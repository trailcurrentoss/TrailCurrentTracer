#!/bin/bash
# Apply the image's SYSTEM files to a running dev board.
#
# WHY THIS EXISTS
# ---------------
# dev-deploy.sh copies tracerd and the GUI and nothing else, deliberately: it
# runs unprivileged under ~/tracer-dev so the dev loop never needs a password.
# Everything the IMAGE installs as root — polkit rules, the sudoers drop-in,
# the locale helper, generated locales — is therefore absent on a board that
# was provisioned by hand rather than flashed.
#
# That gap has repeatedly looked like a code bug. The Settings rows for time
# zone and locale render and then fail, because the polkit rule granting
# timedatectl and localectl is missing. Locale generation fails, because the
# helper is missing and only en_GB was ever generated. In both cases the
# daemon is correct and the board is under-provisioned, and the failure only
# reproduces here — never on a real image.
#
# This script closes that gap. It installs exactly what image/layer/tracer-base.yaml
# installs, from the same source files, so a dev board behaves like a flashed
# one. It is NOT a substitute for building an image and it installs no systemd
# units: dev-deploy.sh still owns running the daemon.
#
# Requires sudo ON THE BOARD, so it will prompt for a password once. That is
# the reason it is a separate script and not part of the deploy.
#
# Usage:
#   ./scripts/dev-provision.sh                       # install, then verify
#   ./scripts/dev-provision.sh --check               # verify only, change nothing
#   ./scripts/dev-provision.sh --device user@host    # when there is no prompt
#
# The board address is asked for on first use and saved to scripts/dev.env
# (gitignored). --device / TRACER_DEVICE override it.

set -euo pipefail

# Board address, key and port. No default is baked in — see dev-env.sh. It
# consumes --device/--key/--port and hands the rest back in DEV_REST.
# shellcheck source=scripts/dev-env.sh
. "$(dirname "$0")/dev-env.sh" "$@"
eval set -- "$DEV_REST"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES="$ROOT/image/layer/files"

# The locales the image pre-generates. Kept in step with tracer-base.yaml so a
# dev board offers the same instant choices a flashed one does.
LOCALES="en_US.UTF-8 en_GB.UTF-8 de_DE.UTF-8 fr_FR.UTF-8 es_ES.UTF-8 \
it_IT.UTF-8 pt_BR.UTF-8 nl_NL.UTF-8 sv_SE.UTF-8 ja_JP.UTF-8"

# ── verify ───────────────────────────────────────────────────────────
# Mirrors the image build's verify step, but tests CAPABILITIES rather than
# files. Two reasons:
#
#   * /etc/polkit-1/rules.d is mode 0750 and /etc/sudoers.d/* is 0440, so the
#     unprivileged account this runs as cannot see either. File tests would
#     report "missing" even when correctly installed.
#   * A rule that is present but malformed grants nothing. pkcheck asks polkit
#     the same question the daemon's call will ask, so a JS syntax error in
#     the rule shows up here instead of as a Settings row that does nothing.
#
# Each check names what BREAKS, not what is absent — every one of these fails
# silently at runtime, which is how they went unnoticed in the first place.
check() {
    ssh $SSH_OPTS "$DEVICE" bash -s <<'SEOF'
bad=0
ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; bad=1; }
skip() { printf '  ?     %s\n' "$1"; }

# Asks polkit whether THIS user may take the action, without taking it.
authorized() { pkcheck --action-id "$1" --process $$ >/dev/null 2>&1; }

if command -v pkcheck >/dev/null; then
    authorized org.freedesktop.NetworkManager.wifi.scan \
      && ok "WiFi scan authorized" \
      || fail "WiFi scan not authorized: the network list is a stale cache showing only the joined SSID"

    authorized org.freedesktop.timedate1.set-timezone \
      && ok "time zone / clock authorized" \
      || fail "timedate1 not authorized: time zone, automatic time and the clock all fail silently"

    authorized org.freedesktop.locale1.set-locale \
      && ok "locale selection authorized" \
      || fail "locale1 not authorized: the locale picker cannot apply a choice"
else
    skip "pkcheck absent — cannot verify polkit grants"
fi

# sudo -l reports the grant without running anything, and unlike reading the
# drop-in it needs no privilege.
sudo -n -l /usr/local/sbin/tracer-locale-gen >/dev/null 2>&1 \
  && ok "locale wrapper is granted in sudoers" \
  || fail "tracer-locale-gen not granted: a new locale cannot be generated"

sudo -n -l /usr/bin/raspi-config nonint get_wifi_country >/dev/null 2>&1 \
  && ok "WiFi region is granted in sudoers" \
  || fail "raspi-config not granted: WiFi region cannot be read or set"

[ -x /usr/local/sbin/tracer-locale-gen ] \
  && ok "locale wrapper installed" \
  || fail "tracer-locale-gen missing: only locales built at image time are selectable"

# Uncommented in locale.gen is NOT generated, and only the second makes a
# locale selectable. This is the check the image build itself was missing.
if locale -a 2>/dev/null | grep -qiE '^en_US\.utf-?8$'; then
    ok "locales generated ($(locale -a 2>/dev/null | grep -ci utf) UTF-8 locales present)"
else
    fail "locales not generated: every picker choice would set an ungenerated LANG"
fi
exit $bad
SEOF
}

if [ "${1:-}" = "--check" ]; then
    echo "Checking $DEVICE"
    check && { echo; echo "Board matches the image's system provisioning."; exit 0; }
    echo
    echo "Run ./scripts/dev-provision.sh to fix." >&2
    exit 1
fi

# ── install ──────────────────────────────────────────────────────────
for f in 49-tracer-networkmanager.rules 50-tracer-timedate.rules \
         010_tracer-system tracer-locale-gen; do
    [ -f "$FILES/$f" ] || { echo "missing source file: $FILES/$f" >&2; exit 1; }
done

# The sudoers drop-in is a TEMPLATE. @TRACER_USER@ is the device account, which
# is chosen when an image is built and is never baked into the repo — so it has
# to be substituted here too, with whoever this dev board actually runs as.
# Installing the template verbatim would grant nothing and fail silently, which
# is precisely the class of bug this script exists to eliminate.
#
# Ask the board rather than parsing $DEVICE: the SSH destination may carry no
# user at all (relying on ~/.ssh/config), and guessing it wrong writes a
# sudoers rule for an account that does not exist.
BOARD_USER="$(ssh $SSH_OPTS "$DEVICE" 'id -un')"
[ -n "$BOARD_USER" ] || { echo "could not determine the board's username" >&2; exit 1; }
echo "board account: $BOARD_USER"

STAGE_LOCAL="$(mktemp -d)"
trap 'rm -rf "$STAGE_LOCAL"' EXIT
# Both templates: the sudoers grant names the user, and the polkit rule matches
# on subject.user. A wrong or unsubstituted name in either is silent at runtime.
for f in 010_tracer-system 50-tracer-timedate.rules; do
    sed "s/@TRACER_USER@/$BOARD_USER/g" "$FILES/$f" > "$STAGE_LOCAL/$f"
    grep -q '@TRACER_USER@' "$STAGE_LOCAL/$f" \
      && { echo "substitution failed for $f — refusing to install" >&2; exit 1; }
done

# A malformed sudoers drop-in locks sudo out of the board entirely, and the
# board has no console to recover from. Validate the SUBSTITUTED file HERE,
# before anything is copied, exactly as the image build does. Validating the
# template would always fail: @TRACER_USER@ is not a valid user name.
if command -v visudo >/dev/null; then
    visudo -c -f "$STAGE_LOCAL/010_tracer-system" >/dev/null \
      || { echo "010_tracer-system is malformed — refusing to install it" >&2; exit 1; }
else
    echo "note: visudo not on this workstation; the board will validate instead"
fi

echo "→ $DEVICE"
STAGE="/tmp/tracer-provision.$$"
ssh $SSH_OPTS "$DEVICE" "mkdir -p $STAGE"
scp -q $SSH_OPTS \
    "$FILES/49-tracer-networkmanager.rules" \
    "$STAGE_LOCAL/50-tracer-timedate.rules" \
    "$STAGE_LOCAL/010_tracer-system" \
    "$FILES/tracer-locale-gen" \
    "$DEVICE:$STAGE/"

echo "sudo on the board — you will be asked for the password once."
ssh $SSH_OPTS -t "$DEVICE" "STAGE=$STAGE LOCALES='$LOCALES' sudo -p 'password for %u on %h: ' bash -s" <<'SEOF'
set -euo pipefail

install -d -m755 /etc/polkit-1/rules.d
install -m644 "$STAGE/49-tracer-networkmanager.rules" /etc/polkit-1/rules.d/
install -m644 "$STAGE/50-tracer-timedate.rules"       /etc/polkit-1/rules.d/
echo "installed polkit rules"

install -m755 "$STAGE/tracer-locale-gen" /usr/local/sbin/tracer-locale-gen
echo "installed /usr/local/sbin/tracer-locale-gen"

# Validate before it is in place. A bad drop-in in /etc/sudoers.d takes the
# whole sudoers file with it, and this board has no console to recover from.
install -d -m755 /etc/sudoers.d
install -m440 "$STAGE/010_tracer-system" /etc/sudoers.d/.010_tracer-system.new
if visudo -c -f /etc/sudoers.d/.010_tracer-system.new >/dev/null; then
    mv /etc/sudoers.d/.010_tracer-system.new /etc/sudoers.d/010_tracer-system
    echo "installed sudoers drop-in"
else
    rm -f /etc/sudoers.d/.010_tracer-system.new
    echo "sudoers drop-in is malformed — left the existing one in place" >&2
    exit 1
fi

# Uncomment the image's locale set and GENERATE it. Uncommenting alone leaves
# `locale -a` unchanged, localectl then accepts a LANG that does not exist and
# the system falls back to C at next boot with nothing shown anywhere.
changed=0
for L in $LOCALES; do
    if grep -qE "^# *${L} UTF-8" /etc/locale.gen; then
        sed -i "s/^# *\(${L} UTF-8\)/\1/" /etc/locale.gen
        changed=1
    fi
done
if [ "$changed" = 1 ] || ! locale -a 2>/dev/null | grep -qiE '^en_US\.utf-?8$'; then
    echo "generating locales (this takes a minute)…"
    locale-gen --keep-existing
fi

rm -rf "$STAGE"
SEOF

echo
echo "Verifying:"
check && { echo; echo "Board now matches the image's system provisioning."; } || {
    echo; echo "Some checks still fail — see above." >&2; exit 1; }
