# Tracer boot chain

Target: **under 12 seconds** from power to interactive launcher, with no console
text, no cursor, and no desktop at any point.

Every trimmed unit below carries a one-line justification so a future maintainer
can put it back without guessing why it went.

**Status:** designed against verified hardware, not yet built or measured. The
12 s figure is a target, not a result.

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

```ini
disable_splash=1          # kill the rainbow test pattern
boot_delay=0              # no artificial wait
dtparam=audio=off         # SoC PWM audio unused; speaker is fed from HDMI
dtparam=i2c_arm=on        # required by the GT911
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
disable_overscan=1
arm_64bit=1
arm_boost=1

dtoverlay=tracer-gt911    # 0x5d only — see below

[pi5]
dtoverlay=nospi10
```

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

```
quiet loglevel=0 vt.global_cursor_default=0 logo.nologo console=tty3
fastboot noswap ro root=… rootfstype=ext4 rootwait
cfg80211.ieee80211_regdom=US
```

`console=tty3` moves kernel output off the panel. The stock image currently has
`console=tty1`, which puts it directly on the display — that alone breaks
acceptance criterion 1.

`console=serial0,115200` is kept **only** in a debug variant of the image. Serial
console on a field tool is a way for a loose UART to hang the boot.

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

```ini
[Unit]
After=network-pre.target
Before=tracer-ui.service

[Service]
Type=notify          # sd_notify READY=1 once the socket is listening
Restart=always
RestartSec=1
```

`Type=notify` is what lets the UI wait on a socket that is genuinely accepting,
rather than racing a `Type=simple` fork. The UI unit orders itself after this one
and additionally retries the socket, so neither ordering nor timing is load-bearing.

`After=network-pre.target`, not `network-online.target` — `tracerd` must start
and serve an `unavailable` network state instantly rather than blocking boot on a
WiFi association that may never happen. Waiting for the network here would be the
single easiest way to blow the 12 s target in the field.

## 5. `tracer-ui.service`

Cage (Wayland) hosting Chromium in kiosk mode. No Openbox, no X11, no cursor
unless a mouse is attached.

```
cage -s -- chromium --kiosk --ozone-platform=wayland
  --app=http://127.0.0.1:8710/ui
  --noerrdialogs --disable-infobars --hide-scrollbars
  --disable-features=Translate,TranslateUI
  --check-for-update-interval=31536000
  --force-device-scale-factor=1
  --enable-gpu-rasterization
```

`Restart=always`, `RestartSec=1`.

Note the PocketTerm35's RP2040 **does** present a mouse collection alongside the
keyboard, so Cage will show a cursor whenever the Pico is alive. The mock has no
cursor. Either hide it via Cage/Chromium, or accept it — needs a decision, and it
is not something `--kiosk` handles on its own.

## 6. Trimmed units

| Disabled | Why |
|---|---|
| `getty@tty1` | Would draw a login prompt on the panel. Criterion 1. |
| `ModemManager` | No cellular modem; it probes serial devices at boot and costs time. |
| `avahi-daemon` | Tracer runs its own Zeroconf in `tracerd.discovery`; two mDNS responders on one host conflict over the `.local` namespace. |
| `unattended-upgrades` | A field tool must not change underneath a technician, and the rootfs is read-only anyway. |
| `systemd-timesyncd` | Superseded by GNSS/PPS time where available; otherwise pointless offline. |
| `triggerhappy` | Would consume the same evdev devices `tracerd.input` owns. |
| `bluetooth` | Unused; frees the UART and some boot time. |
| `apt-daily{,-upgrade}.timer` | Wakes the device on a read-only rootfs to do nothing. |

`avahi-daemon` deserves emphasis: leaving it enabled alongside the daemon's own
Zeroconf is the kind of conflict that produces intermittent, hard-to-reproduce
discovery failures in the field.

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

## 7. Read-only rootfs

```
/                 ext4, ro, with an overlayfs upper in tmpfs
/var/lib/tracer   small writable partition — settings.json, captures, keymap
/var/log          tmpfs — journald with Storage=volatile
```

Acceptance criterion 4 is ten hard power cuts mid-use with a clean boot every
time. The only writable partition is `/var/lib/tracer`, and `settings.json` is
written atomically (write to temp, `fsync`, `rename`) so a cut mid-write leaves
either the old file or the new one, never a truncated one.

Captures land on `/media/usb0` when a stick is present — a corrupted capture
costs one recording, not the boot.

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
