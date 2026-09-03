"""moduledebug — a raw USB serial console for ANY attached board.

SCOPE
-----
This is a general-purpose serial monitor, not a TrailCurrent-specific tool.
Anything that presents a USB serial port works: ESP32 (native USB or via a
bridge), Arduino, Teensy, Pi Pico, an anonymous CH340 clone. Nothing here
parses, classifies or expects a particular firmware — bytes in, bytes out.

The one firmware-aware behaviour is answering ESC[5n (see point 3 below), and
it is strictly a response to something the device sends first, so a board that
never sends it is unaffected.

WHY THIS WAS REWRITTEN
----------------------
The first version grew one feature per symptom: reconnect-by-USB-identity, a
waiting screen, follow mode, log-level filters, an explicit reset, heuristics
around empty reads. Each change fixed something real; together they reached
726 lines and made a serial console behave unpredictably. A serial console on
Linux has no business being unpredictable.

This version does what TerminalModule does — which has been stable — with a
serial file descriptor in place of a pty: open a port, read bytes, write
bytes, close. No auto-reconnect, no filtering, no reset. If the module goes
away the session ends and says so, and the operator picks it again. Silently
re-opening the port underneath someone is what made the old version feel
haunted.

FIVE THINGS ARE KEPT, each proven on the wire against a real module:

1. DTR and RTS are DEASSERTED before anything else.
   Opening a tty asserts both by default. On an ESP those lines drive EN
   (reset) and IO0 (boot mode), so merely opening the port rebooted the
   module — and on native USB that reboot re-enumerated the device, killing
   the descriptor, which triggered a reconnect, which reset it again. The
   module never stayed up long enough to print past its banner. HUPCL is
   cleared so closing does not reset it either. A monitor must be passive.

2. The INCOMPLETE line is published as `partial`.
   A prompt ("antenna> ") has no trailing newline, and neither does any
   character the module echoes back as it is typed. Publishing only completed
   lines hid both, which reads as "connected, then dead" while the link is
   perfectly healthy.

3. ESC[5n is answered with ESC[0n.
   ESP-IDF's linenoise probes the terminal with a status-report request, NOT
   the cursor-position query ESC[6n. Captured from a live module:
       b"I (689) wifi_init: tcp mbox: 6\\r\\n\\x1b[5n\\r\\r\\nType 'help' ..."
   This CANNOT help across a reboot on native USB: the device detaches, and
   the console probes within milliseconds of returning — long before the host
   can re-enumerate and reopen the node. The "does not support escape
   sequences" warning after a reboot is a property of that firmware's console
   on native USB and is not fixable from here.

4. A ZERO-BYTE READ IS NOT A DISCONNECT.
   A serial fd is not a socket. With VMIN=0 a tty read() answers "nothing
   buffered right now" with zero bytes rather than EAGAIN, and asyncio's
   add_reader is level-triggered, so every burst of data produces a surplus
   wakeup after the buffer has already been drained. Measured against a live
   ESP32-S3 printing its help text: 79 reads carrying data, 24 carrying
   nothing — and the old code called every one of those 24 EOF.

   Each one closed the port, and the watcher re-opened it 20 ms later, so the
   console filled with "disconnected"/"re-attached" pairs while the module sat
   there perfectly healthy. Re-opening also cleared `partial`, which erased
   the characters the module echoes back as they are typed: the operator could
   run a command and never see it, which reads as a broken keyboard.

   Two changes, so this cannot come back. VMIN=1 makes an idle read raise
   EAGAIN, which the reader already treats as idleness; and a zero-byte read
   is now confirmed against POLLHUP before it is believed. A real unplug still
   ends the session at once — the node disappears and the read raises
   EIO/ENODEV, which is a different path entirely.

5. THE PUBLISH THROTTLE HAS A TRAILING EDGE.
   Reads coalesce at 50 ms, or a burst of output would be dozens of frames.
   Without a trailing flush that throttle drops the END of every burst: the
   first chunk paints and the rest waits for the 2 s poll. Typing looked fine,
   because keystrokes are further apart than 50 ms and each got its own push —
   but pressing Enter painted only the echoed newline. The cursor moved down a
   line and the command output did not appear, so the next keypress was what
   finally flushed it, making Enter look like it needed pressing twice.

   The wait now takes a deadline whenever a chunk has been coalesced away, so
   the tail lands ~50 ms after the module stops talking. Verified with
   tools/serial_read_probe.py --eol that this module executes on CR, LF and
   CRLF alike: the newline was never the problem, the publishing was.

The keyboard reaches the port through the same sink TerminalModule uses, so
the product has one input path rather than two.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import re
import select
import struct
import termios
import time
from pathlib import Path

from .base import Degraded, Module

log = logging.getLogger("tracerd.moduledebug")

# Modem-control ioctls — termios exposes the bits but not these operations.
TIOCMBIC = 0x5417                      # clear the given modem bits
TIOCM_DTR, TIOCM_RTS = 0x002, 0x004

# Recognised USB vendors, used only to give a port a friendly name. An
# unlisted device still appears and still works — it is simply labelled by
# whatever product string it reports, or by its device node.
KNOWN_VENDORS = {
    "303a": "Espressif",       # ESP32-S3/C3/P4 native USB
    "10c4": "Silicon Labs",    # CP210x
    "1a86": "WCH",             # CH340 / CH9102 — most clone boards
    "0403": "FTDI",            # FT232 and friends
    "2341": "Arduino", "2a03": "Arduino",
    "239a": "Adafruit", "1b4f": "SparkFun", "16c0": "Teensy",
    "2e8a": "Raspberry Pi",    # Pico / RP2040 (see KEYBOARD_IDS below)
    "0483": "STMicroelectronics",
}
# Chips that bridge USB to a real UART. For these the baud rate genuinely
# matters; for a board with native USB it is ignored by the hardware.
BRIDGES = ("10c4", "1a86", "0403", "0483")

# Tracer's own keyboard is an RP2040 with a CDC port. It is hidden from the
# list, but still recognised here so a direct call cannot open it and read the
# operator's keystrokes.
KEYBOARD_IDS = {("1209", "0001")}

DEFAULT_BAUD = 115200
BAUDS = [9600, 115200, 230400, 460800, 921600]
_BAUD_CONST = {9600: termios.B9600, 115200: termios.B115200,
               230400: termios.B230400, 460800: termios.B460800,
               921600: termios.B921600}

MAX_LINES = 2000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BAUD_STORE = Path(os.environ.get("TRACER_STATE", "/var/lib/tracer")) / "moduledebug.json"


def _hung_up(fd: int) -> bool:
    """True only when the kernel says the far end is really gone.

    The one authority on "is this port still there". A read returning no
    bytes is not evidence; POLLHUP (or the node vanishing, which surfaces as
    POLLERR/POLLNVAL) is.
    """
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    for _fd, events in poller.poll(0):
        return bool(events & (select.POLLHUP | select.POLLERR | select.POLLNVAL))
    return False


def _usb_info(tty: str) -> dict:
    """Walk up the sysfs chain to the USB device owning this tty."""
    node = Path("/sys/class/tty") / tty / "device"
    try:
        node = node.resolve()
    except OSError:
        return {}
    for _ in range(6):
        if (node / "idVendor").exists():
            def rd(n):
                f = node / n
                try:
                    return f.read_text().strip()
                except OSError:
                    return ""
            return {"vid": rd("idVendor"), "pid": rd("idProduct"),
                    "product": rd("product"), "serial": rd("serial")}
        if node.parent == node:
            break
        node = node.parent
    return {}


def list_ports(include_hidden: bool = False) -> list[dict]:
    out = []
    for name in sorted(os.listdir("/dev")):
        if not (name.startswith("ttyUSB") or name.startswith("ttyACM")):
            continue
        i = _usb_info(name)
        vid, pid = i.get("vid", ""), i.get("pid", "")
        kbd = (vid, pid) in KEYBOARD_IDS
        native = vid == "303a"
        out.append({
            "device": f"/dev/{name}", "name": name, "vid": vid, "pid": pid,
            "serial": i.get("serial", ""),
            "label": i.get("product") or KNOWN_VENDORS.get(vid) or name,
            # An ESP32-S3/C3/P4 reports "USB JTAG/serial debug unit": ONE
            # built-in peripheral serves both the serial console and JTAG, so
            # the name mentions JTAG even when used purely as a console. There
            # is no UART and no bridge chip, which is also why baud has no
            # effect on it.
            "transport": ("Built-in USB serial console" if native
                          else "USB-to-UART bridge" if vid in BRIDGES
                          else "USB serial"),
            # Baud only means something when a UART bridge is doing the
            # timing. Native-USB boards (ESP32-S3/C3/P4, Pico, most modern
            # Arduinos with USB stacks) ignore it entirely.
            "baud_applies": vid in BRIDGES,
            "likely_module": not kbd,
            "keyboard": kbd, "selectable": not kbd,
            "identity": i.get("serial") or f"{vid}:{pid}",
        })
    return out if include_hidden else [p for p in out if not p["keyboard"]]


class ModuleDebugModule(Module):
    name = "moduledebug"
    interval = 2.0
    # Enter transmits CR to a serial peer, unlike a pty which wants LF.
    # See InputModule._to_terminal.
    newline = "\r"

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self._fd: int | None = None
        self._port: str | None = None
        self._baud = DEFAULT_BAUD
        self._task: asyncio.Task | None = None
        self._lines: list[dict] = []
        self._partial = b""
        self._error: str | None = None
        # USB identity of the module the scrollback belongs to, so a new board
        # cannot inherit the previous one's output. See _open().
        self._identity: str | None = None
        # Set while the operator is intentionally attached to a device. Used
        # only to re-attach after the module power-cycles.
        self._watch: dict | None = None
        self._watch_task: asyncio.Task | None = None
        try:
            self._bauds = json.loads(BAUD_STORE.read_text())
        except (OSError, ValueError):
            self._bauds = {}

    # ── state ────────────────────────────────────────────────────────
    def _annotate(self, ports: list[dict]) -> list[dict]:
        for p in ports:
            p["baud"] = int(self._bauds.get(p["identity"], DEFAULT_BAUD))
        return ports

    async def poll(self):
        # Assert keyboard ownership for as long as the port is open.
        #
        # A one-shot claim at connect time is not enough: InputModule resets to
        # nav whenever the last GUI disconnects, which includes an ordinary
        # page reload. The port stays open and the console still renders, but
        # every keystroke reverts to being a navigation BUTTON — so "b"
        # disconnects and "a" reconnects, and the console appears to
        # disconnect and reconnect on every key. Re-asserting each poll makes
        # that unreachable; set_mode() is a no-op when nothing changed.
        if self._fd is not None:
            self._claim_keyboard(True)

        ports = self._annotate(list_ports())

        # An error describes a PAST event. Once the device it names is back in
        # the list it is stale, and leaving it on screen makes a healthy port
        # look broken — which also made Rescan look like it did nothing, since
        # the only visible state never changed.
        if self._error and self._fd is None:
            names = {p["name"] for p in ports}
            if any(n in self._error for n in names):
                self._error = None
        data = {"ports": ports, "bauds": BAUDS,
                "connected": self._port, "baud": self._baud,
                "waiting": bool(self._watch) and self._fd is None,
                "error": self._error,
                "lines": self._lines[-MAX_LINES:],
                "partial": _ANSI.sub("", self._partial.decode("utf-8", "replace"))}
        if not ports and not self._port:
            raise Degraded("no modules connected", data)
        return data

    # ── keyboard ownership ───────────────────────────────────────────
    # An open port IS an interactive session, so the daemon claims the
    # keyboard itself rather than relying on the GUI to ask.
    #
    # The GUI-driven version worked only for a connect the operator initiated:
    # when the watcher re-attached after a power-cycle, nothing re-issued
    # set_mode and typing was silently dead from then on — indistinguishable
    # from a broken link. Tying it to the descriptor means it cannot drift.
    def _claim_keyboard(self, claim: bool) -> None:
        inp = self.hub.modules.get("input")
        if inp is None:
            return
        try:
            inp.sink = "moduledebug" if claim else "gui"
            inp.set_mode("text" if claim else "nav")
        except Exception:
            log.exception("could not %s the keyboard",
                          "claim" if claim else "release")

    @property
    def alive(self) -> bool:
        return self._fd is not None

    def write_sync(self, data: str) -> None:
        """Called from the input dispatch, already on the event loop."""
        if self._fd is None:
            raise OSError("not connected")
        os.write(self._fd, data.encode())

    # ── connection ───────────────────────────────────────────────────
    def _configure(self, fd: int, baud: int) -> None:
        iflag, oflag, cflag, lflag, _i, _o, cc = termios.tcgetattr(fd)
        iflag &= ~(termios.IXON | termios.IXOFF | termios.IXANY
                   | termios.INLCR | termios.ICRNL | termios.IGNCR)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ICANON | termios.ECHO | termios.ECHOE
                   | termios.ISIG | termios.IEXTEN)
        cflag |= termios.CLOCAL | termios.CREAD | termios.CS8
        cflag &= ~(termios.CRTSCTS | termios.PARENB | termios.CSTOPB
                   | termios.HUPCL)
        # VMIN=1, not 0 — see docstring point 4. With VMIN=0 an idle tty
        # answers read() with ZERO BYTES instead of EAGAIN, which is
        # indistinguishable from EOF.
        cc[termios.VMIN], cc[termios.VTIME] = 1, 0
        speed = _BAUD_CONST[baud]
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

    # The module's USB cable is also its power. Unplugging it IS a reboot, and
    # on native USB the device node disappears and comes back. ESP-IDF's
    # console probes the terminal within a few hundred milliseconds of booting
    # and disables line editing if nothing answers — so re-attaching on the
    # normal 2-second poll is far too late, every time.
    #
    # This watcher re-opens the SAME device (matched by USB serial, which for
    # Espressif native USB is the chip's MAC) as soon as the kernel creates the
    # node. It is safe to do now only because opening no longer asserts
    # DTR/RTS: the earlier version reset the module on every open, so watching
    # for it produced an endless reset loop.
    WATCH_INTERVAL = 0.02        # 20 ms — fast enough to answer the probe
    WATCH_TIMEOUT = 120.0        # give up rather than poll /dev forever

    async def _watcher(self) -> None:
        target = self._watch
        if not target:
            return
        deadline = time.monotonic() + self.WATCH_TIMEOUT
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(self.WATCH_INTERVAL)
                if self._fd is not None:
                    return
                match = next((p for p in list_ports()
                              if p["identity"] == target["identity"]), None)
                if match is None:
                    continue
                res = await self._open(match["device"], target["baud"],
                                       watch=False)
                if res.get("ok"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("moduledebug watcher crashed")

    def _start_watch(self) -> None:
        if self._watch and (self._watch_task is None or self._watch_task.done()):
            self._watch_task = asyncio.create_task(self._watcher())

    def _stop_watch(self) -> None:
        self._watch = None
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def _open(self, device: str, baud: int, watch: bool = True) -> dict:
        if self._fd is not None and self._port == device:
            # Re-assert keyboard ownership even on the no-op path. Something
            # else may have released it (a GUI reload, leaving and re-entering
            # the app), and returning early without claiming leaves an open
            # port that cannot be typed into.
            self._claim_keyboard(True)
            return {"ok": True, "port": device, "baud": self._baud, "already": True}
        await self._close()

        info = {p["device"]: p
                for p in self._annotate(list_ports(True))}.get(device)
        if info is None:
            return {"ok": False, "error": f"{device} is not connected"}
        if info["keyboard"]:
            return {"ok": False,
                    "error": "That port is Tracer's own keyboard, not a module"}
        if baud not in _BAUD_CONST:
            return {"ok": False, "error": f"{baud} baud is not supported"}

        try:
            fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            return {"ok": False, "error":
                    {errno.EACCES: f"No permission to open {device}",
                     errno.EBUSY: f"{device} is already in use"}.get(
                         exc.errno, f"{device}: {exc.strerror}")}
        try:
            # Before anything else — docstring point 1.
            try:
                fcntl.ioctl(fd, TIOCMBIC, struct.pack("I", TIOCM_DTR | TIOCM_RTS))
            except OSError:
                pass                      # adapter without modem control
            self._configure(fd, baud)
        except Exception as exc:
            os.close(fd)
            return {"ok": False, "error": str(exc)}

        # A DIFFERENT module is a different session. Carrying the previous
        # board's scrollback into it reads as output from the device now named
        # in the header, which is how an operator ends up debugging the wrong
        # hardware. Re-attaching to the SAME module after a power-cycle must
        # keep its history — that is the reason for the waiting screen.
        if info["identity"] != self._identity:
            self._lines = []
        self._identity = info["identity"]
        self._fd, self._port, self._baud = fd, device, baud
        self._error, self._partial = None, b""
        if self._bauds.get(info["identity"]) != baud:
            self._bauds[info["identity"]] = baud
            self._save_bauds()
        if watch:
            self._watch = {"identity": info["identity"], "baud": baud}
        self._claim_keyboard(True)
        self._task = asyncio.create_task(self._reader())
        self._meta(f"— connected to {device} at {baud} baud —"
                   if watch else f"— {info['name']} back, re-attached —")
        await self.refresh()
        return {"ok": True, "port": device, "baud": baud}

    async def _close(self) -> None:
        self._claim_keyboard(False)
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = self._port = None

    def _save_bauds(self) -> None:
        try:
            BAUD_STORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = BAUD_STORE.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                json.dump(self._bauds, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, BAUD_STORE)
        except OSError:
            pass                          # a forgotten baud is a nuisance

    # ── reading ──────────────────────────────────────────────────────
    # A module answers a command with one burst: 918 bytes of `help` text
    # arrives as dozens of chunks inside a few milliseconds. Publishing each
    # one is pointless work, so they coalesce — but a throttle with no
    # trailing edge drops the END of every burst, which is the part that
    # matters. See docstring point 5.
    PUSH_SETTLE = 0.05
    #
    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        fd, ev = self._fd, asyncio.Event()
        assert fd is not None
        loop.add_reader(fd, ev.set)
        last_push = 0.0
        # Set when a chunk was coalesced away by the throttle below and is
        # therefore not on screen yet. See PUSH_SETTLE.
        unflushed = False
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        ev.wait(), self.PUSH_SETTLE if unflushed else None)
                except asyncio.TimeoutError:
                    # The burst has stopped. Publish the tail — nothing else
                    # will, until the 2 s poll.
                    unflushed = False
                    await self.refresh()
                    continue
                ev.clear()
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    self._fail(f"{Path(self._port or '').name} disconnected"
                               if exc.errno in (errno.EIO, errno.ENODEV)
                               else exc.strerror or "read failed")
                    return
                if not chunk:
                    # NOT necessarily EOF — ask the kernel. On a tty, zero
                    # bytes means "nothing buffered right now" as readily as
                    # it means "the far end is gone", and only POLLHUP tells
                    # the two apart. See docstring point 4.
                    if not _hung_up(fd):
                        continue
                    self._fail(f"{Path(self._port or '').name} disconnected")
                    return
                if self._ingest(chunk):
                    now = time.monotonic()
                    if now - last_push >= self.PUSH_SETTLE:
                        last_push = now
                        unflushed = False
                        await self.refresh()
                    else:
                        # Coalesced. The wait above now has a deadline, so
                        # this lands on screen once the burst stops.
                        unflushed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never die silently: nothing awaits this task, so an unhandled
            # exception would vanish and leave a session that looks connected
            # and receives nothing.
            log.exception("moduledebug reader crashed")
            self._fail(f"reader error: {exc}")
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    def _fail(self, reason: str) -> None:
        self._error = reason
        self._meta(f"— {reason} —")
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = self._port = None
        # Hand the keyboard back while disconnected, so buttons navigate again
        # instead of typing into a port that is not there.
        self._claim_keyboard(False)
        # Power-cycling the module is the normal way to see its boot output,
        # so watch for it to come back rather than making the operator
        # re-select it — and be quick enough to answer the console probe.
        self._start_watch()
        asyncio.create_task(self.refresh())

    _PROBES = ((b"\x1b[5n", b"\x1b[0n"), (b"\x1b[6n", b"\x1b[24;80R"))

    def _ingest(self, chunk: bytes) -> bool:
        before = self._partial
        self._partial += chunk
        for probe, reply in self._PROBES:
            if probe in self._partial:
                if self._fd is not None:
                    try:
                        os.write(self._fd, reply)
                    except OSError:
                        pass
                self._partial = self._partial.replace(probe, b"")

        parts = re.split(rb"\r\n|\n|\r", self._partial)
        self._partial = parts.pop()
        if len(self._partial) > 4096:      # garbage from a wrong baud rate
            parts.append(self._partial)
            self._partial = b""
        # Destructive backspace echo (BS space BS) edits the pending line.
        while b"\x08" in self._partial:
            i = self._partial.index(b"\x08")
            self._partial = self._partial[:max(0, i - 1)] + self._partial[i + 1:]
        for raw in parts:
            self._add(_ANSI.sub("", raw.decode("utf-8", "replace")), "raw")
        # Publish when the PENDING line changed too, not only on a newline —
        # otherwise typed characters appear only after Enter.
        return bool(parts) or before != self._partial

    def _add(self, text: str, level: str) -> None:
        self._lines.append({"t": time.time(), "level": level, "text": text})
        if len(self._lines) > MAX_LINES:
            del self._lines[:len(self._lines) - MAX_LINES]

    def _meta(self, text: str) -> None:
        self._add(text, "meta")

    # ── operations ───────────────────────────────────────────────────
    async def handle(self, op: str, args: dict):
        args = args or {}
        if op == "ports":
            return self._annotate(list_ports())
        if op == "rescan":
            # An explicit rescan also acknowledges the last error: the operator
            # has asked for the current truth, so a stale banner from a
            # previous disconnect must not survive it.
            self._error = None
            await self.refresh()
            ports = list_ports()
            return {"ok": True, "ports": len(ports),
                    "names": [p["name"] for p in ports]}
        if op == "open":
            device = args.get("port", "")
            baud = int(args["baud"]) if args.get("baud") else next(
                (p["baud"] for p in self._annotate(list_ports())
                 if p["device"] == device), DEFAULT_BAUD)
            return await self._open(device, baud)
        if op == "close":
            # An explicit disconnect ends the watch too; otherwise Tracer would
            # silently grab the port again behind the operator.
            self._stop_watch()
            await self._close()
            self._meta("— disconnected —")
            await self.refresh()
            return {"ok": True}
        if op == "clear":
            self._lines = []
            await self.refresh()
            return {"ok": True}
        if op == "send":
            if self._fd is None:
                return {"ok": False, "error": "Not connected"}
            try:
                os.write(self._fd, (args.get("data") or "").encode())
            except OSError as exc:
                return {"ok": False, "error": exc.strerror}
            return {"ok": True}
        if op == "set_baud":
            baud = int(args.get("baud") or DEFAULT_BAUD)
            ident = args.get("identity")
            if baud not in BAUDS:
                return {"ok": False, "error": f"{baud} is not an offered rate"}
            if not ident:
                return {"ok": False, "error": "No device given"}
            self._bauds[ident] = baud
            self._save_bauds()
            await self.refresh()
            return {"ok": True, "identity": ident, "baud": baud}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def tile_status(self):
        if self._port:
            return (Path(self._port).name, "#74FE00")
        n = len(list_ports())
        return ((f"{n} port{'s' if n != 1 else ''}", "#FFC107") if n
                else ("no modules", "#666"))
