# Tracer controls

Two independent input paths, both mandatory. Acceptance criterion 2 requires every
app to be fully operable by **buttons alone** and, separately, by **touch alone**.

That redundancy is not a courtesy. The buttons and the speaker both hang off the
carrier board's RP2040, which can strand itself in BOOTSEL from an accidental
double-tap of the case-back RESET button — see
[hardware.md](hardware.md#the-rp2040-is-a-single-point-of-failure-for-input-and-audio).
The GT911 touch controller is on I2C and is unaffected. **Touch is the recovery
path from a dead Pico**, so it has to be complete.

---

## Logical buttons

`tracerd.input` emits exactly these, and nothing else reaches the GUI:

```
dpad_up  dpad_down  dpad_left  dpad_right
a  b  x  y  l  r  start  select
```

Each with `phase` = `down` | `up` | `hold`.

## Global semantics

| Input | Action |
|---|---|
| D-pad | Move focus / scroll cursor |
| **Start** | **Open / Confirm — everywhere, in every mode** |
| **Select** | **Back / Cancel — everywhere, in every mode** |
| A | Open / confirm — shortcut, navigation only |
| B | Back — shortcut, navigation only |
| X | Context action 1 — usually search/filter |
| Y | Context action 2 — usually pin/mark/toggle |
| L / R | Adjust a focused slider; otherwise jump to first/last |
| Esc | Cancel a text field (real key on the keyboard) |
| Touch | Everything reachable by button is tappable; hit targets ≥ 44 px |

### Start and Select are the universal pair

**Start = Open, Select = Back, in every context** — launcher, inside an app,
and inside a text field. This is not a preference; it falls out of the
hardware.

A and B are the literal letter keys `A` and `B`. They work as shortcuts while
navigating, but the instant a text field has focus they **must type**, because
the operator needs those letters. Start (`KEY_PAUSE`) and Select (`KEY_SYSRQ`)
carry no character, so they are the only two buttons that can mean the same
thing in every mode.

Binding them per-screen would give the operator two navigation models to hold
in their head. Instead `main.js` normalises once, at the top of the input
router:

```js
if (btn === "start") btn = "a";
else if (btn === "select") btn = "b";
```

Every existing handler then works unchanged, and inside a text field an
incoming "a"/"b" can only have come from Start/Select — the letter keys are
already consumed as text.

**This supersedes the mock**, which assigned Start to an app-switcher overlay
and Select to a status sheet. Those need different bindings; consistency of
the primary navigation pair matters more.

### Escaping a text field

B cannot cancel a text field — it types the letter "b". Three things do:

| | |
|---|---|
| **Select** | The universal Back |
| **Esc** | A real key on the QWERTY keyboard, and what most people reach for |
| **Enter** | Accepts rather than cancels |

This was a real trap: with an editor open, pressing B did nothing at all, and
the still-open overlay made the *next* thing selected look like it had opened
an input box. Every text field now shows `Enter Save` / `Esc Cancel` inline
and warns that B types a letter.

## Per-app hints

The bottom hint bar always shows the **current** meaning of A/X/Y/B for the
focused context. Values are lifted verbatim from the design mock's `HINTS` block:

| App | A | B | X | Y |
|---|---|---|---|---|
| Launcher | Open | Back | Search | Pin to top row |
| MQTT Inspector | Expand | Home | Pause | Capture |
| Discovery | Confirm | Home | Rescan | Details |
| Capture | Record | Home | Filter | Upload |
| Firmware | Toggle target | Home | Push | Manifest |
| Network | Recheck | Home | Rejoin | Static IP |
| Terminal | — | Home | Ctrl-C | Paste (L/R = Tab) |
| Logs | Expand | Home | Level | Follow |
| CAN Sniffer | Decode | Home | Freeze | Send frame |
| Headwaters | Container | Home | Restart | Logs |
| GNSS & Map | Center | Home | Sat view | Mark |
| Checklist | Toggle | Home | Rerun | Export |
| Settings | Edit | Home | Reset | Reboot |

---

## How buttons actually arrive

The PocketTerm35 has **no gamepad**. Its HID report descriptor declares a
keyboard, a mouse, and a consumer-control collection — no Gamepad or Joystick
usage. So the table above is an interaction model, not a hardware description.

The translation lives in `tracerd.input`:

```
/dev/input/event0  →  evdev keycode  →  keymap  →  logical button  →  WebSocket
   (USB HID kbd)                      (settings.json)
```

The prompt's rule holds exactly as written — the GUI never binds a raw keyboard
code in production. The keymap is data, editable from the Settings app's existing
`Button mapping` row, not code.

### Default keymap — captured from hardware

**Verified 2026-09-01 on the live unit.** Two independent passes over all twelve
controls produced identical codes. This is measured, not assumed.

| Logical | Keycode | `KEY_*` | HID usage | Also a character? |
|---|---|---|---|---|
| `dpad_up` | 103 | `KEY_UP` | `0x70052` | no |
| `dpad_down` | 108 | `KEY_DOWN` | `0x70051` | no |
| `dpad_left` | 105 | `KEY_LEFT` | `0x70050` | no |
| `dpad_right` | 106 | `KEY_RIGHT` | `0x7004f` | no |
| `a` | 30 | `KEY_A` | `0x70004` | **yes** |
| `b` | 48 | `KEY_B` | `0x70005` | **yes** |
| `x` | 45 | `KEY_X` | `0x7001b` | **yes** |
| `y` | 21 | `KEY_Y` | `0x7001c` | **yes** |
| `l` | 38 | `KEY_L` | `0x7000f` | **yes** |
| `r` | 19 | `KEY_R` | `0x70015` | **yes** |
| `start` | 119 | `KEY_PAUSE` | `0x70048` | no |
| `select` | 99 | `KEY_SYSRQ` | `0x70046` | no |

Committed as [`tracerd/keymap.default.json`](../tracerd/keymap.default.json).

There are **no physical shoulder buttons**. L and R are the letter keys L and R.
The D-pad is the keyboard's real arrow cluster.

### Modal input — a direct consequence of the mapping

Six of the twelve buttons are **literal QWERTY letters**. The device also has to
support typing: the Terminal, WiFi passwords, MQTT topic filters, static IP
entry. If `tracerd.input` always consumed `KEY_A` as button A, the operator could
never type the letter "a".

So `input` is modal:

| Mode | Behaviour | Entered by |
|---|---|---|
| **nav** (default) | Typeable keys are swallowed and emitted as logical buttons. | Default; leaving a text field |
| **text** | Every key passes through verbatim as text. Arrows still navigate within the field. | Focusing a text field or the Terminal |

The mode is owned by `tracerd`, not the GUI, so there is exactly one source of
truth about whether a keystroke is a button or a character.

**Start and Select are the only two non-typeable buttons.** That makes them the
only keys guaranteed safe as a universal escape from text mode, which is why
`select` is bound to the mode toggle. Nothing else can do that job.

### No on-screen keyboard — decided

The original build plan called for the Terminal to have "an on-screen keyboard
driven by the D-pad". **That is cut. Do not build one, anywhere in Tracer.**

The device has a real QWERTY keyboard, so an on-screen one would be strictly
slower — D-pad-walking a grid to pick letters you could simply type.

This applies to **every** text input in the product, not just the Terminal: WiFi
passwords, MQTT topic filters, static IP entry, capture filenames. All of them
switch to text mode and take input from the physical keyboard. No virtual
keyboard, no character-picker grid, no D-pad letter selection.

The one thing text fields still need is an unmistakable indication of which mode
they are in, since the same physical keys mean different things in each. That is
a status-bar affordance, not a keyboard.

### Safety: `select` is bound to SysRq

`KEY_SYSRQ` is the magic SysRq key, and the kernel binds a `sysrq` handler
directly to this device (`H: Handlers=sysrq kbd leds event0`). The unit currently
runs with:

```
$ cat /proc/sys/kernel/sysrq
438
```

438 enables, among others, **reboot/poweroff (128)** and **remount read-only
(32)**. So an Alt-plus-Select chord can hard-reboot the device without syncing —
on a field tool whose acceptance criterion is surviving unclean shutdowns, that
is a self-inflicted power cut, and Select is a button the operator presses
constantly to open the status sheet.

**The Tracer image must set `kernel.sysrq=0`** via `/etc/sysctl.d/`. Reading the
key as an ordinary keycode is unaffected — only the kernel's magic handling is
disabled.

### Still open

`start` and `select` came through as `KEY_PAUSE` and `KEY_SYSRQ`, which are not
normally standalone keys on a thumb keyboard — they are probably Fn chords
handled inside the RP2040 firmware (no separate modifier event was recorded). If
they are two-handed to reach, they are poor choices for frequently-used global
actions, and the app switcher / status sheet may want a different binding. Worth
confirming how they are physically produced.

### Re-capturing after a firmware change

The mapping lives in the RP2040's CircuitPython `code.py`, so reflashing the Pico
can move it. To re-capture, run [`tracerd/tools/capture_keymap.py`](../tracerd/tools/capture_keymap.py)
on the device — it needs no `sudo` (the device account is in the `input`
group) and no `evtest`, and it does not grab the keyboard:

```bash
# $TRACER_DEVICE is your board, from scripts/dev.env — see scripts/dev.env.example.
. scripts/dev-env.sh
scp $SSH_OPTS tracerd/tools/capture_keymap.py "$DEVICE:/tmp/"
ssh $SSH_OPTS "$DEVICE" \
    'python3 /tmp/capture_keymap.py 300 /tmp/keymap.log & '
# press each control once, in the order of the table above, then:
#   cat /tmp/keymap.log
```

The `hid_usage` column in the keymap exists for exactly this: comparing raw HID
usages tells you whether the Pico firmware changed or the kernel mapping did.

---

## Development bindings

In the browser under `make mock`, behind a dev flag that is **never** enabled on
device:

| Key | Button |
|---|---|
| Arrows | D-pad |
| Z / X / C / V | A / B / X / Y |
| Q / E | L / R |
| Enter / Shift | Start / Select |

Dev mode also wraps the app in a 640×480 frame so the laptop view matches the
panel exactly.

Note these differ from the *device* default keymap above (dev uses Z/X/C/V for
A/B/X/Y; the device default uses Enter/Esc/X/Y). The dev bindings are the
prompt's; the device defaults mirror the mock's. Both are data, and once the real
hardware mapping is captured the device side stops being a guess.
