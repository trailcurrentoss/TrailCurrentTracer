# Tracer hardware — verified findings

**Status:** verified against the live device on 2026-09-01, not inferred from the
build prompt. Where a finding contradicts the build prompt, the finding wins and
the contradiction is called out explicitly under [Divergences](#divergences-from-the-build-prompt).

Verification method: live SSH to the running unit plus `dtc` decompilation of the
vendor overlays, now vendored at
[image/vendor/3.5HDMI_E_DTBO/](../image/vendor/3.5HDMI_E_DTBO/PROVENANCE.md).
Every claim below is backed by a command whose output is quoted.

---

## Platform

| | Verified value | Source |
|---|---|---|
| SoC / board | Raspberry Pi 5, BCM2712 | `uname -a` → `aarch64`, `/sys/.../soc@107c000000` |
| Hostname | `tracer` | `uname -a` |
| OS | Debian GNU/Linux 13 (trixie) | `/etc/os-release` |
| Kernel | `6.18.39+rpt-rpi-2712` | `uname -a` |
| Access | `. scripts/dev-env.sh && ssh $SSH_OPTS "$DEVICE"` — address in `scripts/dev.env` | — |

`sudo` on this unit **requires a password**. Assume non-interactive `sudo` fails;
any privileged step must be handed to the operator as a command to run, not
attempted from a script. `i2c-tools` is not installed; `picotool` is.

---

## Display — HDMI, not DPI or SPI

This is the single most important finding, because the build prompt flagged it as
the decision that "changes the display path materially".

**The panel is HDMI.** No SPI framebuffer driver, no `fbcp-ili9341`, no tinydrm
shim. The stock `vc4-kms-v3d` KMS path drives it natively.

```
$ for c in /sys/class/drm/card*-HDMI*; do echo "$c: $(cat $c/status)"; cat $c/modes; done
/sys/class/drm/card1-HDMI-A-1: connected
640x480
800x600
640x480
/sys/class/drm/card1-HDMI-A-2: disconnected
```

The connector reports **640x480 as its native and preferred mode**, which matches
the design mock's fixed 640×480 canvas exactly. No scaling, no letterboxing, no
`--force-device-scale-factor` gymnastics beyond `=1`.

### The overlay name is misleading

`dtoverlay=waveshare-35dpi-5b` says "dpi", and the vendor ships it in a folder
called `3.5HDMI_E_DTBO`. Decompiling it settles the question — it contains **only
a touch controller node, and no display node whatsoever**:

```
$ dtc -I dtb -O dts waveshare-35dpi-5b.dtbo     # annotated for readability
/ {
    compatible = "brcm,bcm2835";
    fragment@0 {
        target = <i2c1>;
        __overlay__ {
            ft6236@14 { compatible = "goodix,gt911"; reg = <0x14>; ... };
            ft6236@5d { compatible = "goodix,gt911"; reg = <0x5d>; ... };
        };
    };
};
```

(Annotated: real `dtc` output prints the unresolved phandle `target =
<0xffffffff>` plus a `__fixups__` node naming `i2c1`; the resolved target is
substituted above for readability.)

So the overlay's *entire* job is registering the touch controller. Display and
touch are configured independently — the display comes up over HDMI with no
overlay assistance at all.

Waveshare's own [Software Guide](https://docs.waveshare.com/PocketTerm35/Software-Guide)
corroborates this. Its complete configuration instruction is:

```
dtparam=i2c_arm=on
dtoverlay=waveshare-35dpi-4b
dtoverlay=waveshare-35dpi-5b
dtoverlay=dwc2,dr_mode=host
```

There is **no** resolution, refresh rate, framebuffer, video-mode, rotation, or
touch-calibration step anywhere in the vendor documentation — because the display
needs none. (The guide tells users to enable both `-4b` and `-5b` regardless of
board; on a Pi 5 only `-5b` applies. Harmless, but it is why the stock config
carries a line that does nothing.)

**Consequence:** the display path is boring and reliable. Do not add a display
overlay, do not set legacy `hdmi_*` keys (Pi 5 ignores them under KMS), and do
not pin a mode unless the panel ever fails to advertise EDID. If pinning becomes
necessary, the correct lever on Pi 5 is a `cmdline.txt` fragment
(`video=HDMI-A-1:640x480@60`), not `config.txt`.

`dtoverlay=dwc2,dr_mode=host` is required and must be carried into the Tracer
image — it is already present in the stock config.

---

## Touch — Goodix GT911 on i2c1 @ 0x5d

```
$ dmesg | grep -i goodix
Goodix-TS 1-005d: ID 911, version: 1060
input: Goodix Capacitive TouchScreen as .../i2c-1/1-005d/input/input7
Goodix-TS 1-0014: error -EBUSY: Failed to get irq GPIO
Goodix-TS 1-0014: probe with driver Goodix-TS failed with error -16
```

- Live at **`/dev/input/event7`**, `PROP=2` (`INPUT_PROP_DIRECT`) — a real
  touchscreen, correctly classified. Wayland/Cage will pick it up with no
  configuration.
- Reports 640×480 (`touchscreen-size-x = 0x280`, `-y = 0x1e0`), physical
  70 mm × 53 mm. 1:1 with the display, so no calibration matrix is needed.
- In the **vendor** overlay the DT nodes are named `ft6236@...` but their
  `compatible` is `goodix,gt911`. Waveshare's naming is misleading, not a bug —
  do not "fix" it in the vendored blobs, which are kept byte-identical to
  upstream as a provenance record. The Tracer overlay, being ours, **does**
  rename the node to `gt911@5d` deliberately — DT binding is by `compatible`,
  never by node name, so the rename is cosmetic and safe (see the header
  comment in `tracer-gt911-overlay.dts`).
- Neither overlay carries a `reset-gpios` property, so the Goodix driver
  cannot perform an address-select reset — which is exactly why a panel
  strapped to 0x14 needs the vendor overlay as fallback rather than being
  coaxed onto 0x5d.
- Requires `dtparam=i2c_arm=on`, already set.

### The 0x14 probe failure is expected — and it is log noise we must suppress

The `-5b` overlay declares the controller at **both** 0x14 and 0x5d because
Waveshare ships panels with either address. Only 0x5d is populated here, so the
0x14 node fails `-EBUSY` on **every single boot**.

This is harmless, but acceptance criterion 5 requires `journalctl` to be quiet at
rest, and criterion 1 requires no console text during boot. Two `err`-level lines
per boot violate the spirit of both.

**Fixed.** [`image/overlays/tracer-gt911-overlay.dts`](../image/overlays/tracer-gt911-overlay.dts)
is the vendor `-5b` overlay with the 0x14 node removed, the remaining node
renamed `ft6236@5d` → `gt911@5d` (with a `gt911:` label), and `status = "okay"`
added on the fragment. It compiles clean with `dtc -@`, and a
decompile-and-diff confirms the 0x5d node's **property values** are
byte-identical to upstream — no drift in the interrupt, GPIO, or
touchscreen-size values. (The rename is deliberate and cosmetic; binding is by
`compatible`. See the overlay's header comment.)

The vendor blobs are kept at [`image/vendor/3.5HDMI_E_DTBO/`](../image/vendor/3.5HDMI_E_DTBO/PROVENANCE.md)
as the fallback for a replacement panel strapped to 0x14, and as the provenance
record. The upstream zip, the copy that shipped with the project, and the overlay
actually running on the unit were all verified byte-identical.

**Not yet installed on the device.** Installing it needs `sudo`, which requires a
password here. When you want it live:

```bash
# 1. Copy it over (from the repo root; .dtbo built by `make overlays`)
#    $DEVICE and $SSH_OPTS come from scripts/dev.env — see the SSH note below.
. scripts/dev-env.sh
scp $SSH_OPTS image/overlays/tracer-gt911.dtbo "$DEVICE:/tmp/"

# 2. Log in and run these interactively — sudo will prompt for a password,
#    so they cannot be piped through a one-shot ssh command.
ssh $SSH_OPTS "$DEVICE"
    sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak
    sudo install -m644 /tmp/tracer-gt911.dtbo /boot/firmware/overlays/
    sudo sed -i 's/^dtoverlay=waveshare-35dpi-5b/dtoverlay=tracer-gt911/' \
        /boot/firmware/config.txt
    sudo reboot

# 3. After reboot, confirm:
#    dmesg | grep -i goodix    → one "ID 911" line, and NO -EBUSY lines
#    ls /dev/input/event7      → still present
#    (touch still works)
```

If touch does not come back, restore with `sudo cp /boot/firmware/config.txt.bak
/boot/firmware/config.txt` — the vendor overlay is still installed alongside, so
the rollback is a one-line revert.

**SSH note:** this workstation has ~6 keys in `ssh-agent` and no `~/.ssh/config`,
so SSH offers all of them and exhausts the server's default `MaxAuthTries 6`
before authentication can succeed. Always pass `-o IdentitiesOnly=yes -i <key>`.
Symptoms without it are "Too many authentication failures" and, once fail2ban
reacts, an immediate connection reset that looks like a broken server.

---

## Input — a USB HID keyboard, **not** a gamepad

The build prompt specifies "D-pad, X/Y/A/B, L/R shoulders, Start, Select — GPIO or
USB HID". The reality is narrower than either option and it materially shapes the
`input` module.

There is **one** USB device on the bus:

```
$ lsusb
Bus 001 Device 002: ID 1209:0001 Generic pid.codes Test PID
```

It presents exactly three HID collections. Decoding the report descriptor from
`/sys/bus/hid/devices/0003:1209:0001.0001/report_descriptor`:

| Report ID | Usage page | Collection | Contents |
|---|---|---|---|
| 1 | `05 01` Generic Desktop | `09 06` **Keyboard** | 8 modifier bits, 1 reserved byte, 5 LED bits, **6 keycode bytes** (standard 6KRO boot keyboard) |
| 2 | `05 01` Generic Desktop | `09 02` **Mouse** | 5 buttons, X, Y, wheel |
| 3 | `05 0c` Consumer | — | one 16-bit usage, range `0x001`–`0x28c` |

**There is no Gamepad (`09 05`) and no Joystick (`09 04`) collection.** The device
cannot emit gamepad events, and `/proc/bus/input/devices` confirms only two nodes
come out of it:

```
N: Name="My Company My Custom Pico Keyboard"   H: Handlers=sysrq kbd leds event0
N: Name="My Company My Custom Pico Mouse"      H: Handlers=mouse0 event1
```

### What this means for the design

The mock's A/B/X/Y/D-pad/L/R model is an **abstraction**, not a hardware
description. It stays — it is the right interaction model — but `tracerd.input`
must implement it as a **keycode→logical-button keymap over evdev `event0`**,
not as a gamepad reader.

This does not weaken the prompt's architectural rule. The rule was "the GUI never
binds raw keyboard codes in production", and it still holds exactly: the keymap
lives in `tracerd`, the GUI still receives only `dpad_up` / `a` / `b` / … over the
WebSocket. The translation just happens one layer lower than the prompt assumed.

Helpfully, the design mock already anticipated this — the Settings screen carries
a `Button mapping · Gamepad-first` row, which is precisely the remap UI this needs.

### The physical key → keycode mapping — captured

Resolved 2026-09-01: two independent passes over all twelve controls on the
live unit produced identical codes, committed as
[`tracerd/keymap.default.json`](../tracerd/keymap.default.json). See
[controls.md — "Default keymap — captured from hardware"](controls.md#default-keymap--captured-from-hardware)
for the full table. Notably, A/B are the literal letter keys `KEY_A`/`KEY_B`
(not Enter/Esc as the pre-capture plan assumed), and Start/Select are
`KEY_PAUSE`/`KEY_SYSRQ`.

The capture tool that ships is
[`tracerd/tools/capture_keymap.py`](../tracerd/tools/capture_keymap.py) — a
standalone script, run on the device; there is no `tracerd --capture-keymap`
flag.

---

## The RP2040 is a single point of failure for input *and* audio

The keyboard, the mouse, and the speaker amplifier all hang off one RP2040 on the
carrier board running CircuitPython 10.0.0-beta.0. It is a separate chip from the
Pi 5 and it fails independently of it.

Two documented failure modes, both previously confirmed on this unit:

- Its **BOOT and RESET buttons are exposed on the back of the case**. That
  CircuitPython build advertises `double reset -> BOOTSEL`, so an accidental
  double-tap of RESET drops it into the USB bootloader. USB ID flips
  `1209:0001` → `2e8a:0003` and **every button on the device stops working**.
  Recovery is `sudo picotool reboot -a`; a power cycle does *not* fix it.
- The speaker amp is **gated by the RP2040**, so the same fault also silences
  audio. "No buttons and no sound" is one fault, not two.

### Design consequence — touch fallback is load-bearing, not a nicety

Acceptance criterion 2 already requires every app to be fully operable by touch
alone, independently of the buttons. This finding upgrades that from a courtesy
to **the device's only recovery path from a stranded Pico**. The GT911 is on I2C
and is completely unaffected by the RP2040, so touch keeps working through the
fault.

`tracerd.input` must therefore treat loss of `event0` as an expected runtime
state, not an error: surface it in the status bar, keep serving touch, and never
exit. Reconnect must be automatic when the Pico re-enumerates.

---

## Current boot configuration (as flashed)

```
$ grep -vE '^\s*#|^\s*$' /boot/firmware/config.txt
dtparam=i2c_arm=on
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1
dtoverlay=waveshare-35dpi-5b
dtoverlay=dwc2,dr_mode=host
[pi5]
dtoverlay=nospi10
```

```
$ cat /boot/firmware/cmdline.txt
console=serial0,115200 console=tty1 root=PARTUUID=9cef145c-02 rootfstype=ext4
fsck.repair=yes rootwait quiet splash plymouth.ignore-serial-consoles
cfg80211.ieee80211_regdom=US
```

This is stock Raspberry Pi OS configuration plus the touch overlay. It is not yet
a Tracer image — `console=tty1` still puts kernel output on the panel, and there
is no kiosk. See [boot.md](boot.md) for the target chain.

Note `dtparam=audio=on` is currently set; the build prompt calls for
`dtparam=audio=off`. Since the speaker is fed from **HDMI** (not the SoC's PWM
audio), turning off the legacy audio block does not silence the speaker. Safe to
set `off` as the prompt specifies.

---

## Thermals — the active cooler must be enabled explicitly

The base DTB ships `cooling_fan` with `status="disabled"` and relies on the
firmware detecting a fan on the 4-pin header at boot. On this chassis that
detection does not fire: with the official Active Cooler correctly fitted,
`/proc/device-tree/cooling_fan/status` stayed `disabled`, no cooling device
appeared, and the board idled at **66–70 °C**. The fan still spins briefly at
power-on (5 V reaches it before anything drives PWM), which makes the failure
easy to misread as working.

The Tracer image therefore sets `dtparam=cooling_fan=on` explicitly, with the
firmware-default fan curve written out for reviewability — see the config.txt
block in [`image/layer/tracer-base.yaml`](../image/layer/tracer-base.yaml) and
[boot.md §1](boot.md). The bake-time verify asserts the line is present.

---

## CAN

Not present on this unit — no `can0` interface and no CAN HAT fitted. The
Headwaters CM5 images support two wirings, and Tracer should support the same two
so a technician can move a HAT between units:

| Carrier | Controller | Interrupt |
|---|---|---|
| CM5 IO Base + Waveshare RS485 CAN HAT (B) | MCP2515 on SPI | GPIO25 |
| Waveshare CM5-IO-Wireless-Base | onboard MCP2515 | GPIO17 |

Bitrate is **500 kbit/s** everywhere in the fleet (`can-to-mqtt.py` hardcodes
`bitrate=500000`).

Caveat for Pi 5: this unit currently sets `dtoverlay=nospi10` under `[pi5]`. Any
MCP2515 overlay must be reconciled against that line — verify on hardware before
claiming CAN works, and do not assume the Headwaters `config.txt` fragment ports
across unchanged.

---

## Divergences from the build prompt

Five, all resolved in favour of the hardware or the existing in-house tooling.

| # | Build prompt says | Verified reality | Resolution |
|---|---|---|---|
| 1 | Panel may be DSI/SPI; SPI "changes the display path materially" | **HDMI**, native 640×480, driven by stock `vc4-kms-v3d` | Simplest possible path. No `fbcp-ili9341`, no tinydrm. |
| 2 | Buttons are "GPIO or USB HID" gamepad | USB HID **keyboard + mouse + consumer control**; no gamepad collection exists | Keep the A/B/X/Y model; implement it as a keymap in `tracerd.input`. GUI contract unchanged. |
| 3 | Build the image with **pi-gen** and a custom `stage-tracer` | TrailCurrent already has a working **`rpi-image-gen`** wrapper at `TrailCurrentHeadwaters/CM5/image/` | **Decided: `rpi-image-gen`**, pinned at `RPIIG_REF=cb909cb` in `image/build.sh`. See below. |
| 4 | Splash via **Plymouth** with a `tracer` theme | Headwaters uses rpi-image-gen's `rpi-splash-screen` layer and a 1920×1080 TGA | Port `generate-splash.sh`, but retarget to **640×480**. Plymouth vs splash-layer is a real choice — see [boot.md](boot.md). |
| 5 | `dtparam=audio=off` | Currently `on`; speaker is fed from HDMI regardless | Safe to set `off` as specified. |

### On #3 — pi-gen vs rpi-image-gen

**Decided: `rpi-image-gen`.** The checkout is pinned at `RPIIG_REF=cb909cb` in
[`image/build.sh`](../image/build.sh) — pinned deliberately, because upstream
changes its own interface (`-D` became `-S`), so following main would break
the build on someone else's schedule.

The reasoning that led there: `TrailCurrentHeadwaters/CM5/image/` is a mature,
working `rpi-image-gen` setup that already solves most of what `stage-tracer`
would have to solve from scratch: declarative layer YAML, a Docker-less
reproducible build, bake-time verification, first-boot hooks, a captive-portal
AP, and the splash pipeline. And no base bump was needed: upstream
rpi-image-gen ships a **trixie**-minbase that already targets rpi5, which is
exactly what Tracer includes
([`image/config/tracer-os.yaml`](../image/config/tracer-os.yaml)).

Reusing it means one image toolchain across TrailCurrent instead of two, and
every fix to one benefits the other. Choosing pi-gen would have meant
rebuilding that groundwork to satisfy a prompt line rather than a requirement.
The prompt's actual hard requirements — `make image`, reproducible,
x86-capable via binfmt, `.img.xz` + `.sha256` output — are all satisfied.

---

## Battery and backlight — resolved

Both former open items were settled on hardware and are now deliberate
decisions:

**Battery: no indicator at all — deliberately.** The PocketTerm35 gives the Pi
no way to read charge state, verified on the device rather than assumed:
`/sys/class/power_supply/` is empty, no fuel-gauge module loads, nothing
answers on i2c-1, and the devices on i2c-13 (0x37 0x3a 0x4a 0x4b 0x50) are the
HDMI DDC/EDID bus, not power. Waveshare documents battery state only as four
front-panel LEDs on the UPS board. A gauge that permanently reads `--` is
worse than no gauge — on a handheld it reads as a flat or failing battery,
exactly the wrong thing to tell a technician mid-diagnosis. The decision and
its evidence live in the comment block in
`tracer-ui/src/chrome/chrome.js` (`statusBar`); if a future carrier exposes a
real supply, reinstate from `/sys/class/power_supply` rather than guessing.

**Backlight: software dim, enabled — with a floor.** No kernel backlight
device exists, so the Settings `Brightness` slider is backed by a compositing
dim the GUI applies itself, labelled "% (dim)" so it never implies backlight
control (`tracerd/tracerd/modules/settings.py`, help text: "This panel has no
backlight control, so this dims what is drawn"). It reduces emitted light and
glare — the actual need at night beside a vehicle — but not power draw. The
dim is floored at 0.35 (`tracer-ui/src/main.js`, `applyBrightness`) so the
screen can never be dimmed to the point where the brightness control itself is
unreadable and unrecoverable.

---

## Still to verify on hardware

Honest list of what is *not* yet confirmed, so nothing here reads as more settled
than it is:

- **CAN on Pi 5** with the `nospi10` interaction noted above. `CanModule`
  currently raises "no can0 interface" on this unit — no HAT fitted, nothing
  exercised.
- **Boot time.** The image is built, but the 12 s target is untested; no
  measurement taken yet.
