# Building and flashing Tracer OS

> **Read this first.** The image installs `tracerd` and `tracer-ui` and enables
> both units, so it boots to the splash and then the launcher.
>
> A fresh card is a **clean slate**: everything under `/var/lib/tracer` is
> generated per unit and does not carry over. That means the Headwaters CA, the
> SSH key enrolled with Headwaters, `settings.json` and any captures are gone,
> and the WiFi credentials must be entered again on the boot gate.
>
> Two things are **not** on the card and survive reflashing: the bootloader
> EEPROM (including `PSU_MAX_CURRENT`) and anything written to a USB stick.

Tracer uses [`rpi-image-gen`](https://github.com/raspberrypi/rpi-image-gen), the
same tool as Headwaters, so both TrailCurrent products build the same way and a
fix to one transfers to the other.

---

## Prerequisites

| | |
|---|---|
| Host OS | Debian or Ubuntu. arm64 native, or x86_64 with `qemu-user-static`. |
| Privileges | `sudo` — `rpi-image-gen` chroots the target rootfs. |
| Disk | ~10 GB free under `image/work/`. |
| Packages | `git openssl xz-utils coreutils device-tree-compiler imagemagick` |

Cross-building from x86_64 also needs:

```bash
sudo apt install qemu-user-static binfmt-support
```

`build.sh` checks all of this and fails early with a specific message rather
than part-way through a 30-minute build.

---

## Build

Three steps from a clean checkout:

```bash
# 1. Compile the device tree overlay (GT911 touch)
make overlays

# 2. Render the boot splash (needs the Marketing repo alongside this one)
./image/generate-splash.sh

# 3. Build the image (~30-45 min first run; later runs reuse caches)
sudo ./image/build.sh
```

Or just `sudo make image`, which runs all three in order.

### Device account

**There is no default username and no default password.** The build asks for
both, every time, and refuses to run without them. Nothing about the account is
stored in this repository.

This is deliberate. An earlier version of `build.sh` defaulted to a fixed
username with an identical password. A default nobody changes is not a
placeholder — it is a shipped credential, identical on every unit ever built,
recorded in git history forever and readable by anyone with a copy of the repo.

Interactive — the normal case. The password is read silently and typed twice:

```bash
sudo ./image/build.sh
```

Supply the username up front and be prompted only for the password:

```bash
sudo ./image/build.sh myuser
```

Non-interactive, for CI. `sudo -E` is required, or the variables do not survive
into the build:

```bash
TRACER_IMAGE_USER=myuser TRACER_IMAGE_PASSWORD='…' sudo -E ./image/build.sh
```

**The password is never accepted as a command-line argument.** Arguments are
visible to every user on the build host through `ps`, and they persist in shell
history and CI logs. `build.sh` rejects a second positional argument and tells
you this. In CI, take the value from your runner's secret store — never a
literal in a workflow file.

The build also refuses an empty password, a password equal to the username, and
anything that is not a valid Linux username.

The account exists for console troubleshooting. Tracer ships **no SSH server** —
it is a handheld tool, not a host to log into. Add `openssh-server` to
`image/layer/tracer-base.yaml` if you want a debug variant.

#### Where the account name goes

The name is substituted into the files that need it at build time; it is not
written down anywhere in the tree. These ship as templates containing
`@TRACER_USER@`:

| Template | Substituted by |
|---|---|
| `image/systemd/tracerd.service` | `image/layer/tracer-base.yaml` |
| `image/systemd/tracer-ui.service` | `image/layer/tracer-base.yaml` |
| `image/layer/files/010_tracer-system` (sudoers) | `tracer-base.yaml`, and `scripts/dev-provision.sh` for a dev board |

The image's verify step fails the build if any `@TRACER_USER@` survives into the
rootfs, if a unit does not run as the created account, or if that account does
not exist. Each of those fails silently at runtime otherwise.

Note that `010_tracer-system` does **not** pass `visudo -c` as it sits in the
repo — `@TRACER_USER@` is not a valid user name. Validate the substituted copy.
Both installers already do.

### Output

```
image/deploy/tracer-os-<version>.img.xz
image/deploy/tracer-os-<version>.img.xz.sha256
```

`<version>` comes from `git describe --tags --always --dirty`, so an image built
from uncommitted work is labelled `-dirty` and cannot be confused with a release.

---

## Flash

```bash
lsblk                     # identify the card — check this twice
xzcat image/deploy/tracer-os-<version>.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

`/dev/sdX` is a placeholder. `dd` to the wrong device destroys it without asking;
confirm against `lsblk` output immediately before running, not from memory.

Raspberry Pi Imager works too — point it at the `.img.xz` and skip its OS
customisation, which would fight the image's own boot configuration.

---

## What the image contains

| | |
|---|---|
| Base | Debian 13 (trixie), `trixie-minbase`, device layer `rpi5` |
| Display | Cage (Wayland kiosk) + Chromium. No desktop, no X11, no window manager. |
| Boot config | `config.txt` / `cmdline.txt` per [boot.md](boot.md) |
| Touch | `tracer-gt911.dtbo` — our overlay, not Waveshare's ([why](hardware.md#the-0x14-probe-failure-is-expected--and-it-is-log-noise-we-must-suppress)) |
| Splash | 640×480 TGA via the `rpi-splash-screen` layer |
| Networking | NetworkManager (`nmcli` backs the `net` module) |
| Diagnostics | `can-utils`, `gpsd`, `chrony`, `openssh-client` |
| Trimmed | 10 systemd units disabled, each justified in [boot.md](boot.md#6-trimmed-units) |
| Hardening | `kernel.sysrq=0` — [required, not cosmetic](controls.md#safety-select-is-bound-to-sysrq) |
| **Missing** | **`tracerd` and `tracer-ui`** — not implemented yet |

---

## Layout

```
image/
  config/tracer-os.yaml       rpi-image-gen image config
  layer/tracer-base.yaml      packages + customize hooks (the substance)
  overlays/                   tracer-gt911-overlay.dts and its build product
  vendor/3.5HDMI_E_DTBO/      unmodified Waveshare blobs + PROVENANCE.md
  splash/                     generated TGA (+ PNG for eyeballing)
  build.sh                    the wrapper you actually run
  work/                       rpi-image-gen checkout and build tree (gitignored)
  deploy/                     output images (gitignored)
```

`layer/tracer-base.yaml` is the file worth reading. Top to bottom it is the whole
image: packages, then one hook per concern, each with a comment saying why.

---

## Troubleshooting

**`splash/tracer-splash.tga missing`** — run `./image/generate-splash.sh`. It
needs the `Marketing` repo checked out next to this one; override with
`MARKETING_DIR=/path ./image/generate-splash.sh`.

**`overlays/tracer-gt911.dtbo missing`** — run `make overlays`. Needs
`device-tree-compiler`.

**`cross-building needs qemu-user-static`** — install it, per the top of this
page. binfmt registration is what lets the arm64 chroot run on x86.

**Build dies mid-chroot with mount errors** — usually a stale work tree from an
interrupted run:

```bash
sudo rm -rf image/work/rpi-image-gen/work
```

**Image boots to a blank screen after the splash.** Expected today — see the note
at the top. Confirm the OS itself is healthy over the serial console or by
checking that the card's partitions mounted.

---

## Verifying on hardware

Once flashed, the things worth checking before trusting the image:

```bash
# touch controller — one "ID 911" line, and NO -EBUSY
dmesg | grep -i goodix

# panel came up at native resolution
cat /sys/class/drm/card*-HDMI-A-1/modes | head -1     # expect 640x480

# magic sysrq disabled
cat /proc/sys/kernel/sysrq                             # expect 0

# boot journal is quiet
journalctl -p err -b --no-pager
```

Boot time against the 12 s target:

```bash
systemd-analyze
systemd-analyze critical-chain
```

No measurement has been taken yet — the 12 s figure in [boot.md](boot.md#budget)
is a target, not a result. The first real number should be recorded there.
