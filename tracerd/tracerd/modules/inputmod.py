"""input — the only input path. See docs/controls.md.

Reads evdev keycodes from the PocketTerm35's USB HID keyboard and translates
them to logical buttons via the keymap captured from hardware. The GUI never
sees a keycode.

Two hardware facts shape this module:

  * There is no gamepad. The device presents a plain 6KRO boot keyboard, so
    six of the twelve buttons are literal letter keys (A B X Y L R). They must
    be swallowed in nav mode and passed through in text mode — hence the modes.

  * The keyboard can vanish. It hangs off a carrier-board RP2040 whose RESET
    button is exposed on the case back; a double-tap strands it in BOOTSEL and
    every button dies while touch keeps working. So losing event0 is a NORMAL
    state that must recover on its own, never an error that stops the daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from pathlib import Path

from .base import Module, Unavailable

# struct input_event on 64-bit: timeval (2x long) + u16 type + u16 code + s32 value
_FMT = "llHHi"
_SZ = struct.calcsize(_FMT)

EV_KEY = 0x01
EV_MSC = 0x04

NAV, TEXT = "nav", "text"

# Keycode -> character, for text mode. Only what the launcher path needs;
# the Terminal will need the full table plus modifier handling.
_CHARS = {
    30: "a", 48: "b", 46: "c", 32: "d", 18: "e", 33: "f", 34: "g", 35: "h",
    23: "i", 36: "j", 37: "k", 38: "l", 50: "m", 49: "n", 24: "o", 25: "p",
    16: "q", 19: "r", 31: "s", 20: "t", 22: "u", 47: "v", 17: "w", 45: "x",
    21: "y", 44: "z", 57: " ", 11: "0", 2: "1", 3: "2", 4: "3", 5: "4",
    6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 52: ".", 51: ",", 12: "-",
    13: "=", 26: "[", 27: "]", 39: ";", 40: "'", 43: "\\", 53: "/",
}


# Editing keys, emitted by NAME rather than as characters, so the GUI never
# has to sniff control codes out of a text stream.
# Buttons that carry no character and are therefore usable as actions while a
# text field has focus. Everything else in text mode types.
_TEXT_SAFE_BUTTONS = {
    "start", "select",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
}

# What each editing key sends to a pty. Sinking these too means Enter and
# Backspace are as fast as every other key.
_EDIT_BYTES = {"enter": "\n", "backspace": "\x7f", "tab": "\t", "escape": "\x1b"}

_EDIT_KEYS = {
    14: "backspace",
    28: "enter",
    1:  "escape",
    15: "tab",
}


def _control_code(ch: str):
    """Ctrl-<key> -> its C0 control character, or None if there isn't one."""
    c = ch.lower()
    if "a" <= c <= "z":
        return chr(ord(c) - 96)          # a->0x01 ... z->0x1a
    return {"[": "\x1b", "\\": "\x1c", "]": "\x1d",
            " ": "\x00", "-": "\x1f"}.get(c)


def load_keymap(path: Path | None = None) -> dict:
    """Load the hardware-captured keymap. Falls back to the packaged default."""
    candidates = [
        path,
        Path("/var/lib/tracer/keymap.json"),
        Path("/var/lib/tracer/keymap.default.json"),
        Path(__file__).resolve().parents[2] / "keymap.default.json",
    ]
    for c in candidates:
        if c and c.is_file():
            with open(c) as fh:
                return json.load(fh)
    raise FileNotFoundError("no keymap found")


class InputModule(Module):
    name = "input"
    interval = 2.0
    backoff_interval = 2.0  # re-check for the keyboard often; it comes back

    def __init__(self, hub, mock: bool = False, device: str | None = None):
        super().__init__(hub)
        self.mock = mock
        self.mode = NAV
        self._device = device
        self._km = load_keymap()
        # keycode -> logical button
        self._by_code = {
            int(b["code"]): name for name, b in self._km["buttons"].items()
        }
        self._typeable = {
            int(b["code"]) for b in self._km["buttons"].values() if b.get("typeable")
        }
        self._reader: asyncio.Task | None = None
        self._present = False
        self._shift = False
        self._ctrl = False
        # Where text-mode characters go. "gui" broadcasts them over the
        # WebSocket; "terminal" writes straight to the pty in-process.
        self.sink = "gui"
        self._hold_ms = self._km.get("hold", {}).get("threshold_ms", 500)

    # ── discovery ────────────────────────────────────────────────────
    def _find_device(self) -> str | None:
        if self._device:
            return self._device if os.path.exists(self._device) else None
        want = self._km.get("device", {}).get("match", {}).get("name", "")
        try:
            with open("/proc/bus/input/devices") as fh:
                blocks = fh.read().split("\n\n")
        except OSError:
            return None
        for blk in blocks:
            if want and want in blk and "kbd" in blk:
                for tok in blk.split():
                    if tok.startswith("event"):
                        return f"/dev/input/{tok}"
        return None

    async def setup(self) -> None:
        if not self.mock:
            self._reader = asyncio.create_task(
                self._read_loop(), name="input:reader"
            )

    async def poll(self):
        if self.mock:
            self._present = True
            return {"mode": self.mode, "keyboard": True, "touch": True, "mock": True}

        dev = self._find_device()
        self._present = dev is not None
        if not dev:
            # Normal, recoverable state — the Pico may be in BOOTSEL.
            # Touch is unaffected, so the GUI stays usable.
            raise Unavailable("keyboard not enumerated")
        return {"mode": self.mode, "keyboard": True, "touch": True, "device": dev}

    # ── event pump ───────────────────────────────────────────────────
    async def _read_loop(self) -> None:
        """Own task so a missing keyboard never blocks poll()."""
        while True:
            dev = self._find_device()
            if not dev:
                await asyncio.sleep(2.0)
                continue
            try:
                await asyncio.to_thread(self._blocking_read, dev)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Device yanked mid-read is expected. Loop and wait for it back.
                await asyncio.sleep(1.0)

    def _blocking_read(self, dev: str) -> None:
        loop = self.hub.loop
        with open(dev, "rb", buffering=0) as fh:
            while True:
                data = fh.read(_SZ)
                if not data or len(data) < _SZ:
                    return
                _s, _us, etype, code, value = struct.unpack(_FMT, data)
                if etype != EV_KEY:
                    continue
                # 1 = press, 2 = autorepeat, 0 = release
                phase = {1: "down", 2: "hold", 0: "up"}.get(value)
                if phase is None:
                    continue
                loop.call_soon_threadsafe(self._dispatch, code, phase)

    def _dispatch(self, code: int, phase: str) -> None:
        # Track modifiers. They are never buttons, so this is safe in both
        # modes. Ctrl was missing entirely, which is why Ctrl-C typed a "c"
        # instead of interrupting — there was nothing to combine it with.
        if code in (42, 54):            # LEFTSHIFT / RIGHTSHIFT
            self._shift = phase in ("down", "hold")
            return
        if code in (29, 97):            # LEFTCTRL / RIGHTCTRL
            self._ctrl = phase in ("down", "hold")
            return
        btn = self._by_code.get(code)

        if self.mode == TEXT:
            # Route purely by whether the key produces a character. The daemon
            # does not decide what start/select MEAN — it just forwards them as
            # buttons, and the GUI binds them to Accept/Cancel.
            #
            # This is the whole reason the modal design works: A, B, X, Y, L
            # and R are literal letter keys, so in a text field they must type.
            # That leaves start and select as the only buttons that can carry
            # an action here, which is exactly what they are used for.
            if phase in ("down", "hold"):
                named = _EDIT_KEYS.get(code)
                if named:
                    # Offer it to the sink FIRST. Enter, backspace and tab are
                    # exactly the keys an interactive console needs, and
                    # routing them only to the GUI meant a sink could receive
                    # Ctrl codes but never a newline — so commands could be
                    # typed and never submitted.
                    data = _EDIT_BYTES.get(named)
                    if data and self._to_terminal(data):
                        return
                    self.hub.broadcast_ev("input", {"key": named, "ts": time.time()})
                    return
                ch = _CHARS.get(code)
                if ch:
                    if self._ctrl:
                        # Ctrl-<letter> is the control code: C -> 0x03 (SIGINT),
                        # D -> 0x04 (EOF), and so on. The tty line discipline
                        # turns 0x03 into the signal, so this is what actually
                        # breaks out of a running command.
                        ctl = _control_code(ch)
                        if ctl is not None:
                            if self._to_terminal(ctl):
                                return
                            self.hub.broadcast_ev(
                                "input", {"text": ctl, "ctrl": True,
                                          "ts": time.time()})
                            return
                    if self._shift:
                        ch = ch.upper()
                    # Plain characters go to the sink when one is claimed.
                    # Without this branch the sink only ever carried Ctrl
                    # codes: every ordinary keystroke was broadcast to the GUI
                    # instead, so a serial console received nothing at all and
                    # looked dead while being typed into.
                    if self._to_terminal(ch):
                        return
                    self.hub.broadcast_ev("input", {"text": ch, "ts": time.time()})
                    return

            # Non-typeable buttons still act as buttons: start, select, and the
            # d-pad (which moves the caret / the row cursor).
            if btn and btn in _TEXT_SAFE_BUTTONS:
                self.emit(btn, phase)
            return

        # nav mode: typeable keys are consumed as buttons
        if btn:
            self.emit(btn, phase)

    # Modules that can receive raw keystrokes. Both expose the same contract:
    # an `alive` flag and a non-blocking write_sync() that is safe to call
    # from this dispatch, which already runs on the event loop.
    _SINKS = {"terminal": "terminal", "moduledebug": "moduledebug"}

    def _to_terminal(self, data) -> bool:
        """Write straight to whichever sink owns the keyboard.

        Returns True if the keystroke was consumed, so it is not ALSO emitted
        as a navigation button — typing "b" into a serial menu must not walk
        the operator out of the app.
        """
        target = self._SINKS.get(self.sink)
        if not target or not data:
            return False
        mod = self.hub.modules.get(target)
        if mod is None or not getattr(mod, "alive", False):
            return False
        # Enter means different bytes depending on what is listening.
        #
        # A pty wants LF — that is what a shell reads. A SERIAL peer wants CR,
        # because that is what a real terminal transmits when Enter is pressed,
        # and it is what ESP-IDF's console waits for to end a line. Sending LF
        # to a module's menu delivers every typed character correctly and then
        # never submits the command, which reads exactly like "I can't send
        # anything" while the module is in fact receiving it all.
        newline = getattr(mod, "newline", None)
        if newline and data == "\n":
            data = newline
        try:
            mod.write_sync(data)
            return True
        except Exception:
            return False

    def emit(self, btn: str, phase: str) -> None:
        self.hub.broadcast_ev("input", {"btn": btn, "phase": phase, "ts": time.time()})

    def on_clients_changed(self, count: int) -> None:
        """Reset to nav when the last GUI disconnects.

        Text mode is entered by the GUI and left by the GUI. If it crashes or
        reloads mid-edit the daemon would stay in text mode, and every button
        on the device would type a letter instead of navigating — with no way
        back. Resetting on disconnect makes that unreachable.
        """
        if count == 0 and self.mode != NAV:
            self.sink = "gui"
            self.set_mode(NAV)

    def set_mode(self, mode: str) -> None:
        if mode not in (NAV, TEXT) or mode == self.mode:
            return
        self.mode = mode
        self.publish()

    async def handle(self, op: str, args: dict):
        if op == "set_mode":
            self.sink = args.get("sink") or "gui"
            self.set_mode(args.get("mode", NAV))
            return {"mode": self.mode, "sink": self.sink}
        if op == "press" and self.mock:
            # Dev-only: lets `make mock` drive buttons without hardware.
            self.emit(args["btn"], args.get("phase", "down"))
            return {"ok": True}
        raise Unavailable(f"input has no operation {op!r}")

    def tile_status(self):
        return ("--", "#666")
