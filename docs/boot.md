# Tracer boot chain

Target: **under 12 seconds** from power to interactive launcher, with no console
text, no cursor, and no desktop at any point.

Every trimmed unit below carries a one-line justification so a future maintainer
can put it back without guessing why it went.

**Status:** built — `image/deploy/tracer-os-dev.img.xz` (2026-09-03). Boot time
not yet measured; the 12 s figure is a target, not a result.

---

## What the hardware settles

Two findings from [hardware.md](hardware.md) simplify this materially:

- **The panel is HDMI at native 640×480**, driven by stock `vc4-kms-v3d`. No SPI
  framebuffer, no `fbcp-ili9341`, no userspace blitter in the boot path. This is
  the fastest possible display bring-up and it removes the largest risk to the
  12 s budget.
- **The touch overlay does nothing but register the GT911.** It is not on the
  display critical path at all.

---

## 1. `config.txt`

The authoritative source is the heredoc in
[`image/layer/tracer-base.yaml`](../image/layer/tracer-base.yaml), which
**replaces the upstream rpi-image-gen template wholesale** — anything
load-bearing from the template has to be repeated there. The block below is
what the layer actually writes (the layer's inline comments carry the full
rationale for each line):

```ini
auto_initramfs=1
disable_splash=1
boot_delay=0
dtparam=audio=off
dtparam=i2c_arm=on
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
disable_overscan=1
arm_64bit=1
arm_boost=1

# GT911 capacitive touch (i2c1 @ 0x5d, IRQ GPIO4)
dtoverlay=tracer-gt911

# USB-C host mode — required by the carrier board
dtoverlay=dwc2,dr_mode=host

# Active cooler + explicit fan curve (firmware defaults, written out)
dtparam=cooling_fan=on
dtparam=fan_temp0=50000,fan_temp0_hyst=5000,fan_temp0_speed=75
dtparam=fan_temp1=60000,fan_temp1_hyst=5000,fan_temp1_speed=125
dtparam=fan_temp2=67500,fan_temp2_hyst=5000,fan_temp2_speed=175
dtparam=fan_temp3=75000,fan_temp3_hyst=5000,fan_temp3_speed=250

[pi5]
dtoverlay=nospi10
[all]
```

**`auto_initramfs=1` is load-bearing — the board does not boot without it.**
`cmdline.txt` points root at `/dev/disk/by-slot/system`, a udev symlink that is
only ever created from inside the initramfs; without this line the firmware
never loads `initramfs_2712`, the symlink never exists, and the kernel panics
to a black panel (serial-only, since the splash strips `console=tty1`). The
bake-time verify block in `tracer-base.yaml` asserts the line is present.

**`dtparam=cooling_fan=on` is explicit, not detected.** The firmware's boot-time
fan detection does not fire on this chassis, and without it the fan spins once
at power-on and never again — the board idles at 66–70 °C with a cooler fitted.
The `fan_temp0..3` curve restates the firmware defaults so a future change
shows up as a diff.

**`dtoverlay=tracer-gt911` replaces `waveshare-35dpi-5b`.** The vendor overlay
declares the touch controller at both 0x14 and 0x5d; only 0x5d is populated, so
the 0x14 node fails `-EBUSY` and logs two error lines on **every boot**. A ~20-line
local overlay carrying just the 0x5d node removes that permanently. Without it,
the claim "the boot journal is clean" is not true.

Deliberately **not** set: any legacy `hdmi_*` key (Pi 5 ignores them under KMS),
and any display overlay (the panel needs none). `camera_auto_detect` and
`display_auto_detect` are dropped — both cost probe time for hardware Tracer does
not have.

## 2. `cmdline.txt`

**What the image actually ships:** nothing in this repo writes `cmdline.txt`.
rpi-image-gen's own template supplies

```
console=serial0,115200 console=tty1 root=… fsck.repair=yes rootwait
```

and the splash layer (`update_cmdline: y` in
[`image/config/tracer-os.yaml`](../image/config/tracer-os.yaml)) strips
`console=tty1` **only**, so kernel output stops scribbling over the logo. That
one removal is the entire cmdline treatment in the built image.

Note `console=serial0,115200` **is** present in the production image today —
the "debug variant only" split below has not happened.

### Planned — not yet implemented

The designed cmdline was:

```
quiet loglevel=0 vt.global_cursor_default=0 logo.nologo console=tty3
fastboot noswap ro root=… rootfstype=ext4 rootwait
cfg80211.ieee80211_regdom=US
```

None of `quiet` / `loglevel=0` / `vt.global_cursor_default=0` / `logo.nologo` /
`console=tty3` / `fastboot` / `noswap` / `ro` / `rootfstype=ext4` is applied by
the current build. The rationale still stands and is kept for whoever
implements it:

- `console=tty3` would move any remaining kernel output off the panel entirely,
  rather than merely dropping `tty1`.
- `console=serial0,115200` should be kept **only** in a debug variant of the
  image. Serial console on a field tool is a way for a loose UART to hang the
  boot.
- `ro` belongs with the read-only-rootfs work in §7, which is also not yet
  implemented.

## 3. Splash

**Decided: the static `rpi-splash-screen` TGA, matching Headwaters. Not Plymouth.**

The build prompt originally specified Plymouth with a `tracer` theme. Overruled in
favour of consistency with Headwaters — a technician who has seen one TrailCurrent
device boot should recognise the next one.

Built by [`image/generate-splash.sh`](../image/generate-splash.sh), ported from
`TrailCurrentHeadwaters/CM5/image/generate-splash.sh` with the same composition,
background (`#1a1a2e`), green (`#52a441`), and TGA constraints (24-bit, ≤224
colours, uncompressed, `-flip` because boot TGAs are stored bottom-to-top).

Two deliberate changes from the Headwaters original:

- **640×480, not 1920×1080** — the panel's verified native mode. Rendering at
  1080p and letting the firmware scale would soften the icon on a 3.5" screen.
  Note the aspect changes (16:9 → 4:3), so the layout is re-derived rather than
  scaled.
- **The Tracer product icon** (`Marketing/ProductNaming/brand/icons/svg/tc_tracer.svg`)
  with a "Tracer" wordmark, instead of the TrailCurrent house icon.

Output: `image/splash/tracer-splash.tga`, plus a `.png` alongside for eyeballing
without a TGA viewer. The icon SVG has no `<text>` elements, so ImageMagick
renders it correctly — if a future icon revision adds text, switch to Inkscape,
which ImageMagick's SVG text handling cannot match.

Note the splash TGA/PNG and the compiled `.dtbo` are **build products, not
committed** — after a fresh clone, regenerate them with `make splash` and
`make overlays` before building (`image/build.sh` hard-fails with instructions
if either is missing).

### What this costs

The mock's boot screen (`design/…v2.dc.html:47-59`) draws an **animated**
progress bar and a changing status line (`mounting overlay · rootfs ro` →
`starting mosquitto client` → `joining Airstream-27`). A static TGA cannot render
that, so **the boot screen is a deliberate, knowing deviation from the mock** —
the one place the mock is not followed verbatim.

Recorded here so nobody later "fixes" the splash to match the mock without
realising it was a decision. What is gained: roughly 0.5–1 s off the boot budget,
no initramfs hook, no theme to maintain, and visual continuity with Headwaters.

The hard part remains unchanged and is not affected by this choice: the splash
must hand straight off to the GUI with **no flash of console** between them.

## 4. `tracerd.service`

The installed unit, quoted from [`image/systemd/tracerd.service`](../image/systemd/tracerd.service)
with its inline comments stripped (the unit file itself carries the full
rationale):

```ini
[Unit]
Description=Tracer system daemon
Documentation=file:///opt/tracer/docs/api.md
After=network-pre.target dbus.service
Wants=network-pre.target
Before=tracer-ui.service

[Service]
Type=notify
NotifyAccess=main
User=@TRACER_USER@
Group=@TRACER_USER@
SupplementaryGroups=input netdev video i2c gpio dialout

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TRACER_STATE=/var/lib/tracer
WorkingDirectory=/opt/tracer/tracerd
ExecStart=/usr/bin/python3 -m tracerd --port 8710 --ui-dir /opt/tracer/tracer-ui

Restart=always
RestartSec=1
TimeoutStopSec=10

StateDirectory=tracer
ReadWritePaths=/var/lib/tracer

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
```

`Type=notify` is what lets the UI wait on a socket that is genuinely accepting,
rather than racing a `Type=simple` fork. Ordering is enforced by the UI unit's
`Requires=`/`BindsTo=` on this one (see §5); the client additionally retries the
socket, so a slow daemon start is not fatal.

**Not root.** `@TRACER_USER@` is substituted with the device account at image
build time (`image/build.sh`); the daemon gets exactly the privileges it needs
from group membership (`input` for the keyboard, `netdev` for NetworkManager,
`video` for DRM/backlight) plus polkit rules — a daemon serving a browser has
no business being root. The hardening block keeps it from reaching anything
else.

**`TimeoutStopSec=10`.** This unit hit systemd's 90 s default on a real
power-off — `hub.stop_all()` hung after SIGTERM and the panel sat black for a
minute and a half, indistinguishable from "the power button does nothing". The
daemon-side hang is fixed, but the bound stays as the backstop.

`After=network-pre.target`, not `network-online.target` — `tracerd` must start
and serve an `unavailable` network state instantly rather than blocking boot on a
WiFi association that may never happen. Waiting for the network here would be the
single easiest way to blow the 12 s target in the field.

## 5. `tracer-ui.service`

Cage (Wayland) hosting Chromium in kiosk mode. No Openbox, no X11, and no
cursor ever (see below). The `ExecStart`, verbatim from
[`image/systemd/tracer-ui.service`](../image/systemd/tracer-ui.service):

```ini
ExecStart=/usr/bin/cage -- /usr/bin/chromium \
    --kiosk --ozone-platform=wayland \
    --app=http://127.0.0.1:8710/ \
    --noerrdialogs --disable-infobars --hide-scrollbars \
    --disable-features=Translate,TranslateUI \
    --check-for-update-interval=31536000 \
    --force-device-scale-factor=1 \
    --enable-gpu-rasterization \
    --user-data-dir=/var/lib/tracer/chromium
```

`Restart=always`, `RestartSec=1`. `Environment=HOME=/var/lib/tracer` and the
explicit `--user-data-dir` point Chromium's profile at the only writable
location.

**No `cage -s`.** The flag used to be here with a comment claiming it hides the
cursor. It does not — `cage -h` lists `-s` as "Allow VT switching", so all it
did was let anyone Ctrl+Alt+F2 out of the kiosk to a console. Do not put it
back. Cage 0.2.0 has no cursor flag at all.

Ordering: `After=tracerd.service seatd.service`, `Requires=tracerd.service`
**and** `BindsTo=tracerd.service` — the daemon serves the UI bundle, so there
is nothing to show without it. `seatd.service` must be enabled (it is — see
§6) for Cage to get a seat.

**No autologin, no logind session.** `getty@tty1` is disabled and the kiosk is
a plain system unit under `graphical.target` running as the device account.
That is why the unit uses `RuntimeDirectory=tracer-ui` (systemd creates
`/run/tracer-ui` as part of starting the unit) instead of `/run/user/1000`,
which only exists for a logind session or a lingering user — neither of which
this image has; pointing `XDG_RUNTIME_DIR` there crash-loops Cage silently
under `Restart=always`. Similarly, `SupplementaryGroups` must never include
`seat`: Debian has no such group (`seatd` runs `seatd -g video`), and systemd
treats an unknown group name as a fatal start error (216/GROUP) — another
invisible crash-loop, found the hard way.

### Cursor suppression — decided and implemented

The PocketTerm35's RP2040 presents an idle mouse HID collection alongside the
keyboard (vendor 1209, product 0001), which is enough for a compositor to draw
a cursor even though no pointing device exists. The mock has no cursor. Fixed
at three layers:

- [`image/layer/files/70-tracer-no-pointer.rules`](../image/layer/files/70-tracer-no-pointer.rules)
  sets `LIBINPUT_IGNORE_DEVICE=1` on that mouse collection — matched narrowly,
  with explicit guards so it can never hit the keyboard or the GT911
  touchscreen. Installed and asserted (rule + both guards) at bake time in
  `tracer-base.yaml`.
- `Environment=WLR_LIBINPUT_NO_DEVICES=1` in `tracer-ui.service` lets Cage
  start even with no input devices visible.
- `cursor: none` in `tracer-ui/src/styles/tokens.css` as the client-side
  belt-and-braces (CSS alone was verified insufficient — the compositor draws
  its own cursor before the client is ever consulted).

## 6. Trimmed units

Nine units disabled, per [`image/layer/tracer-base.yaml`](../image/layer/tracer-base.yaml):

| Disabled | Why |
|---|---|
| `getty@tty1` | Would draw a login prompt on the panel. Criterion 1. |
| `ModemManager` | No cellular modem; it probes serial devices at boot and costs time. |
| `avahi-daemon.service` + `avahi-daemon.socket` | Tracer runs its own Zeroconf in `tracerd.discovery`; two mDNS responders on one host conflict over the `.local` namespace. The socket has to go too, or socket activation resurrects the daemon. |
| `unattended-upgrades` | A field tool must not change underneath a technician. |
| `triggerhappy` | Would consume the same evdev devices `tracerd.input` owns. |
| `bluetooth` | Unused; frees the UART and some boot time. |
| `apt-daily.timer` + `apt-daily-upgrade.timer` | Wakes a fixed-function device to do nothing. |

And one **enabled**: `seatd.service` — the prerequisite of
`tracer-ui.service`'s `After=seatd.service`; Cage gets its seat from it.

**`systemd-timesyncd` is deliberately kept.** It used to be disabled when
chrony was the time daemon; chrony is gone from the package set, so disabling
timesyncd would leave the unit with no clock sync at all. A wrong clock is not
cosmetic here — it breaks TLS to the Headwaters broker ("certificate is not
yet valid") and makes captures impossible to line up against Headwaters' own
logs. See the comment at the disable block in `tracer-base.yaml`.

`avahi-daemon` deserves emphasis: leaving it enabled alongside the daemon's own
Zeroconf is the kind of conflict that produces intermittent, hard-to-reproduce
discovery failures in the field.

### One manager of wlan0 — do not undo any corner of this

Three daemons ship enabled and all three want the radio, which is why WiFi did
nothing at all on the first booting image. The arrangement (the same one
Raspberry Pi OS uses) is pinned by three coordinated changes in
`tracer-base.yaml`: `iwd.service` is **masked** (not merely disabled — it is a
hard Requires of the trixie-minbase suite and its own layer re-enables it),
the generated `02-wlan0.network` is deleted so systemd-networkd keeps eth0
only, and `wifi.backend=wpa_supplicant` is pinned in
`/etc/NetworkManager/conf.d/10-tracer-wifi.conf`. Undoing any one of the three
brings back a second claimant for wlan0.

### `polkitd` is installed explicitly

The image builds without recommends, and `polkitd` is only a *Recommends* of
network-manager — so it is named directly in the package list
(`tracer-base.yaml`). Without the daemon, the polkit rules the layer installs
are inert files: WiFi scan silently degrades to a stale cache and
`timedatectl`/`localectl` answer "Interactive authentication required".
Nothing about that is visible at build time, which is why the bake-time verify
checks the package, not just the rule files.

### `kernel.sysrq=0` — required, not optional

`/etc/sysctl.d/10-tracer.conf`:

```
kernel.sysrq = 0
```

The Select button is `KEY_SYSRQ` (captured from hardware — see
[controls.md](controls.md#safety-select-is-bound-to-sysrq)), and the kernel binds
a magic-SysRq handler directly to that keyboard. The stock unit runs
`kernel.sysrq = 438`, which enables **reboot/poweroff** and **remount read-only**
among others.

So an Alt-plus-Select chord can hard-reboot the device without syncing — a
self-inflicted power cut on a button the operator presses constantly to open the
status sheet, on a device whose whole storage design exists to survive power cuts.
Setting this to 0 disables only the kernel's magic handling; `tracerd` still reads
the key as an ordinary keycode.

## 7. Read-only rootfs — planned, not implemented

**What the image actually does today:** two partitions (boot + root), a plain
read-write ext4 root, and `/var/lib/tracer` as ordinary directories on that
root — not a separate partition, no overlayfs, no `/var/log` tmpfs, journald
at its default storage. Acceptance criterion 4 (ten hard power cuts mid-use
with a clean boot every time) is **currently unmet by construction**.

The design, kept for whoever implements it:

```
/                 ext4, ro, with an overlayfs upper in tmpfs
/var/lib/tracer   small writable partition — settings.json, captures, keymap
/var/log          tmpfs — journald with Storage=volatile
```

The idea is that the only writable partition is `/var/lib/tracer`, so a power
cut can corrupt at most one recording, never the boot.

One sub-claim **is** implemented and verified: `settings.json` is written
atomically — `mkstemp` in the same directory, `fsync`, `os.replace`, then
`fsync` of the directory (`tracerd/tracerd/modules/settings.py`) — so a cut
mid-write leaves either the old file or the new one, never a truncated one.

Capture destination: the settings default `capture_dir` is `/media/usb0`
(settings-configurable), with `/var/lib/tracer/captures` on the root
filesystem as the fallback when no stick is present. Firmware packages are
searched for on `/media/usb0`, `/media/usb1`, and `/mnt/usb`
(`tracerd/tracerd/modules/firmware.py`).

---

## Budget

Untested. Recorded so the first real measurement has something to compare against.

| Stage | Target |
|---|---|
| Firmware + bootloader | ~1.5 s |
| Kernel + initramfs | ~2.5 s |
| Splash visible | ~3 s |
| `tracerd` ready (`sd_notify`) | ~5.5 s |
| Cage + Chromium first paint | ~9.5 s |
| Interactive launcher | **< 12 s** |

**Chromium's cold start dominates the back half and is the only real risk to the
target.** If 12 s is missed, look there first — not at the kernel, and not at the
splash. Dropping Plymouth in favour of the static TGA already removed the other
candidate.
