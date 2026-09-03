#!/usr/bin/env python3
"""Answer two questions about a serial port that guessing gets wrong.

WHY THIS EXISTS
---------------
moduledebug has to make two decisions it cannot make from first principles,
and both have already shipped wrong once. This tool measures them instead.

  --classify   What does read() actually return on this port?

    The reader must decide, on every wakeup, whether the port is idle or gone.
    In Python those look identical: both can surface as a read returning
    nothing. Calling every idle read EOF is what filled the console with
    disconnected/re-attached pairs and wiped the line being typed. Counts the
    three outcomes separately — data, EAGAIN, and the ambiguous zero-byte read
    that is the bug's fingerprint. See moduledebug.py docstring point 4.

  --eol        Which line ending does this module's console act on?

    A serial peer ends a line with CR, and that is what moduledebug sends. But
    ESP-IDF's linenoise has two modes: full editing, where CR (13) submits,
    and DUMB mode, entered when the console decides the terminal cannot do
    escape sequences, where it reads until LF (10) instead. Both modes echo
    every character, so the two are indistinguishable by watching the echo —
    the only visible difference is that the wrong ending moves the cursor down
    a line and never runs the command. Sends the same command with CR, LF and
    CRLF and reports which ones the module actually executed.

Nothing here resets the module: DTR/RTS are deasserted exactly as moduledebug
does before any other I/O.

Usage:
    serial_read_probe.py /dev/ttyACM1 --classify
    serial_read_probe.py /dev/ttyACM1 --classify --vmin 0   # reproduce the bug
    serial_read_probe.py /dev/ttyACM1 --eol
    serial_read_probe.py /dev/ttyACM1 --eol --command version
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import struct
import sys
import termios
import time

# Modem-control ioctls — termios exposes the bits but not these operations.
TIOCMBIC = 0x5417
TIOCM_DTR, TIOCM_RTS = 0x002, 0x004

_BAUD_CONST = {9600: termios.B9600, 115200: termios.B115200,
               230400: termios.B230400, 460800: termios.B460800,
               921600: termios.B921600}

# Named so the report says "CR", not "b'\\r'".
ENDINGS = [("CR", b"\r"), ("LF", b"\n"), ("CRLF", b"\r\n")]

# linenoise discards the pending line on Ctrl-C. Sent between attempts so a
# terminator that did NOT submit cannot leave "help" in the module's buffer
# and contaminate the next one.
CANCEL = b"\x03"


def _open(device: str, baud: int, vmin: int) -> int:
    """moduledebug's open + configure, with VMIN left adjustable."""
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    # Before anything else — opening a tty asserts DTR/RTS, which on an ESP
    # drives EN and IO0. A probe must not reboot the thing it is probing.
    try:
        fcntl.ioctl(fd, TIOCMBIC, struct.pack("I", TIOCM_DTR | TIOCM_RTS))
    except OSError:
        pass                              # adapter without modem control
    iflag, oflag, cflag, lflag, _i, _o, cc = termios.tcgetattr(fd)
    iflag &= ~(termios.IXON | termios.IXOFF | termios.IXANY
               | termios.INLCR | termios.ICRNL | termios.IGNCR)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ICANON | termios.ECHO | termios.ECHOE
               | termios.ISIG | termios.IEXTEN)
    cflag |= termios.CLOCAL | termios.CREAD | termios.CS8
    cflag &= ~(termios.CRTSCTS | termios.PARENB | termios.CSTOPB | termios.HUPCL)
    cc[termios.VMIN], cc[termios.VTIME] = vmin, 0
    speed = _BAUD_CONST[baud]
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, speed, speed, cc])
    return fd


class Listener:
    """Runs moduledebug's reader loop, recording what each read returned."""

    def __init__(self, fd: int, verbose: bool):
        self.fd = fd
        self.verbose = verbose
        self.buf = bytearray()
        self.counts = {"data": 0, "eagain": 0, "zero": 0, "bytes": 0}
        self.dead: str | None = None
        self._ev = asyncio.Event()
        self._t0 = time.monotonic()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        asyncio.get_running_loop().add_reader(self.fd, self._ev.set)
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except Exception:
            pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out

    def _stamp(self) -> str:
        return f"{time.monotonic() - self._t0:7.3f}"

    async def _run(self) -> None:
        while True:
            await self._ev.wait()
            self._ev.clear()
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                self.counts["eagain"] += 1
                if self.verbose:
                    print(f"{self._stamp()}  EAGAIN")
                continue
            except OSError as exc:
                self.dead = f"errno={exc.errno} {exc.strerror}"
                print(f"{self._stamp()}  OSError {self.dead}")
                return
            if not chunk:
                self.counts["zero"] += 1
                # Do NOT stop. The point is to count how often this happens on
                # a port that keeps delivering data afterwards.
                print(f"{self._stamp()}  ZERO  <- reader would call this a disconnect")
                continue
            self.counts["data"] += 1
            self.counts["bytes"] += len(chunk)
            self.buf += chunk
            if self.verbose:
                print(f"{self._stamp()}  {len(chunk):5d}  {chunk[:60]!r}")


async def _type(fd: int, data: bytes, gap: float = 0.04) -> None:
    """A byte at a time, like a person — the timing the bugs showed up under."""
    for byte in data:
        os.write(fd, bytes([byte]))
        await asyncio.sleep(gap)


async def classify(device: str, baud: int, vmin: int, seconds: float,
                   type_text: str, quiet: bool) -> int:
    fd = _open(device, baud, vmin)
    listener = Listener(fd, verbose=not quiet)
    listener.start()
    try:
        if type_text:
            await asyncio.sleep(0.5)
            await _type(fd, type_text.encode().decode("unicode_escape").encode(),
                        gap=0.4)
        await asyncio.sleep(seconds)
    finally:
        listener.stop()
        os.close(fd)

    c = listener.counts
    print(f"\n{device} @ {baud} baud, VMIN={vmin}")
    print(f"  data reads : {c['data']:5d}  ({c['bytes']} bytes)")
    print(f"  EAGAIN     : {c['eagain']:5d}  (idle — harmless)")
    print(f"  ZERO       : {c['zero']:5d}  (ambiguous — must be 0)")
    if c["zero"] and c["data"]:
        print("\nFAIL: the port delivered data AND returned zero-byte reads.\n"
              "      moduledebug would have ended the session on each of those.")
        return 1
    if c["zero"]:
        print("\nZero-byte reads and no data at all — the module may really be "
              "gone. Check that it is powered and enumerated.")
        return 1
    print("\nOK: no ambiguous reads.")
    return 0


async def eol(device: str, baud: int, command: str, settle: float,
              quiet: bool) -> int:
    """Send `command` three ways and report which ones the module ran."""
    fd = _open(device, baud, 1)
    listener = Listener(fd, verbose=not quiet)
    listener.start()
    results = []
    try:
        await asyncio.sleep(0.6)
        listener.take()                   # discard the banner / prompt
        for name, terminator in ENDINGS:
            # Clear anything a previous terminator failed to submit.
            os.write(fd, CANCEL)
            await asyncio.sleep(0.3)
            listener.take()

            await _type(fd, command.encode())
            await asyncio.sleep(0.2)
            listener.take()               # drop the echo of the command itself

            print(f"\n--- {name}: sending {terminator!r} ---")
            os.write(fd, terminator)
            await asyncio.sleep(settle)
            reply = listener.take()
            # The module echoes the terminator whether or not it acted on it,
            # so a couple of bytes proves nothing. Real output is much larger.
            ran = len(reply.strip(b"\r\n")) > 0
            results.append((name, terminator, len(reply), ran))
            print(f"    {len(reply)} bytes back: {reply[:120]!r}")
            if listener.dead:
                break
    finally:
        listener.stop()
        os.close(fd)

    print(f"\n{device} @ {baud} baud — line ending probe, command {command!r}")
    for name, terminator, n, ran in results:
        print(f"  {name:<5} {terminator!r:<8} {n:5d} bytes  "
              f"{'EXECUTED' if ran else 'no output — not submitted'}")

    worked = [name for name, _t, _n, ran in results if ran]
    if not worked:
        print("\nNo line ending produced output. Either the command is not "
              f"valid on this module (try --command), or its console is not "
              "reading. Check it responds in idf.py monitor.")
        return 1
    print(f"\nThis module submits on: {', '.join(worked)}")
    if "CR" not in worked:
        print("moduledebug sends CR (ModuleDebugModule.newline). This module "
              "does NOT act on CR, so Enter will move the cursor down a line "
              "and never run the command. That is the bug to fix.")
        return 1
    print("CR works — moduledebug's newline is correct for this module.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("device", help="e.g. /dev/ttyACM1")
    ap.add_argument("--baud", type=int, default=115200,
                    choices=sorted(_BAUD_CONST))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--classify", action="store_true",
                      help="count what read() returns (default)")
    mode.add_argument("--eol", action="store_true",
                      help="find which line ending submits a command")
    ap.add_argument("--vmin", type=int, default=1, choices=(0, 1),
                    help="--classify only. 1 = shipping; 0 reproduces the bug")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="--classify only. How long to listen")
    ap.add_argument("--type", dest="type_text", default="",
                    help=r"--classify only. Send this text, e.g. 'help\r'")
    ap.add_argument("--command", default="help",
                    help="--eol only. Command to submit (default: help)")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="--eol only. Seconds to wait for output")
    ap.add_argument("--quiet", action="store_true",
                    help="only report anomalies and the summary")
    args = ap.parse_args()

    try:
        if args.eol:
            return asyncio.run(eol(args.device, args.baud, args.command,
                                   args.settle, args.quiet))
        return asyncio.run(classify(args.device, args.baud, args.vmin,
                                    args.seconds, args.type_text, args.quiet))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"{args.device}: {exc.strerror}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
