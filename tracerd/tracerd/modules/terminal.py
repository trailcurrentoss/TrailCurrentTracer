"""terminal — a real local shell on a pty, rendered inside the app.

Local by design: it is a shell ON Tracer. Reaching Headwaters is just `ssh
headwaters.local` typed into it, using the same credentials the other modules
hold — no separate remote-terminal mode to build or get wrong.

Contained rather than free-standing so back-navigation still works. The letter
keys must reach the shell, so while the Terminal has focus tracerd is in text
mode and A/B/X/Y/L/R type. That leaves Select as the only way out, which is
exactly the universal Back binding — see docs/controls.md.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import shutil
import signal
import struct
import termios
import time

from .base import Module, Unavailable

COLS, ROWS = 78, 22          # fits the 640x480 panel at 11px mono
SCROLLBACK = 2000            # kept in the daemon
SEND = 400                   # shipped to the GUI so it has something to scroll


class TerminalModule(Module):
    name = "terminal"
    interval = 5.0           # the pty pushes; this is only a liveness check

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.lines: list[str] = []
        self.pid = 0
        self.fd = -1
        self.alive = False
        self._partial = ""
        self._reader: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    async def _spawn(self):
        if self.alive or self.mock:
            return
        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        pid, fd = pty.fork()
        if pid == 0:                      # child
            # xterm, not dumb. The dumb terminfo entry has no clear
            # capability, so `clear` emits literally nothing and appears
            # broken (verified on the pty). xterm emits ESC[2J / ESC[3J,
            # which _absorb acts on. Everything else xterm adds — colour,
            # bracketed paste, OSC titles — is already stripped by _clean,
            # and filtered echo is byte-identical between the two.
            os.environ["TERM"] = "xterm"
            os.environ["PS1"] = "$ "
            os.execv(shell, [shell, "-i"])
            os._exit(1)
        self.pid, self.fd, self.alive = pid, fd, True
        self._resize(COLS, ROWS)
        os.set_blocking(fd, False)
        loop = asyncio.get_running_loop()
        loop.add_reader(fd, self._on_readable)
        self.lines = []
        await self.refresh()

    def _resize(self, cols, rows):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _on_readable(self):
        try:
            data = os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._reap()
            return
        if not data:
            self._reap()
            return
        self._absorb(data.decode("utf-8", "replace"))
        # `partial` carries the un-terminated tail — which is exactly where a
        # shell prompt lives, since a prompt has no trailing newline. Shipping
        # only `lines` meant the prompt (and every echoed keystroke) waited for
        # the next poll, 30 s away: the terminal looked dead and untypeable.
        # broadcast_now, not broadcast_ev: echo has to be immediate or typing
        # feels laggy. See Hub.broadcast_now.
        # Send a window far larger than the screen. Sending only the visible
        # ROWS meant output that scrolled past never reached the browser at
        # all — there was nothing to scroll back to, no matter what the UI did.
        self.hub.broadcast_now("terminal", {
            "lines": self.lines[-SEND:],
            "partial": self._partial,
            "alive": True,
        })

    def _absorb(self, text: str):
        text = self._partial + text

        # `clear` (and Ctrl-L) send ESC[2J / ESC[3J. Act on them here, before
        # _clean strips every escape: drop the scrollback and keep only what
        # came after the last clear. Without this, `clear` scrolled the prompt
        # down but left the whole history sitting above it.
        cut = max(text.rfind("\x1b[2J"), text.rfind("\x1b[3J"))
        if cut != -1:
            self.lines = []
            self._partial = ""
            text = text[cut + 4:]

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _clean(text)
        parts = text.split("\n")
        self._partial = parts.pop()
        self.lines.extend(parts)
        del self.lines[:-SCROLLBACK]

    def _reap(self):
        if self.fd >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self.fd)
            except Exception:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1
        self.alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        self.pid = 0

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        if self.mock:
            return {"alive": True, "cols": COLS, "rows": ROWS,
                    "lines": ["pi@tracer:~$ docker ps", "(mock terminal)"],
                    "partial": "$ ", "user": "tracer"}
        return {"alive": self.alive, "cols": COLS, "rows": ROWS,
                "lines": self.lines[-SEND:], "partial": self._partial,
                "user": os.environ.get("USER", "tracer")}

    def write_sync(self, data: str) -> None:
        """Called from the input module's dispatch, which is already on the
        event loop. Non-blocking write to the pty; no coroutine, no RPC."""
        if not self.alive or self.fd < 0:
            raise OSError("shell not running")
        os.write(self.fd, data.encode())

    async def handle(self, op, args):
        if op == "open":
            await self._spawn()
            return {"alive": self.alive}
        if op == "close":
            self._reap()
            await self.refresh()
            return {"alive": False}
        if op == "write":
            if not self.alive:
                await self._spawn()
            data = args.get("data", "")
            if not isinstance(data, str):
                raise Unavailable("data must be a string")
            try:
                os.write(self.fd, data.encode())
            except OSError as exc:
                self._reap()
                raise Unavailable(f"shell closed: {exc}")
            return {"wrote": len(data)}
        if op == "signal":
            # Ctrl-C without a tty of our own: send the character, the line
            # discipline turns it into SIGINT for the foreground job.
            if self.alive:
                os.write(self.fd, b"\x03")
            return {"sent": "SIGINT"}
        raise Unavailable(f"terminal has no operation {op!r}")

    async def stop(self):
        self._reap()
        await super().stop()

    def tile_status(self):
        if self.data and self.data.get("alive"):
            return ("running", "#7BC96A")
        return (f"{os.environ.get('USER', 'tracer')}@tracer", "#666")


def _clean(text: str) -> str:
    """Remove escape sequences, apply backspace, drop stray control bytes.

    The fast path checks for ANY control byte, not just ESC. Checking only for
    ESC meant a chunk containing just an erase sequence (\x08 \x08 — the
    common case, since the pty echoes that for every backspace) was returned
    untouched, so deleting a character appended control bytes instead of
    removing one.
    """
    if not any(c < " " or c == "\x7f" for c in text if c not in "\n\t"):
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "\x1b" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "[":
                i += 2
                while i < n and not ("@" <= text[i] <= "~"):
                    i += 1
                i += 1
                continue
            if nxt == "]":
                i += 2
                while i < n and text[i] not in ("\x07", "\x1b"):
                    i += 1
                i += 1
                continue
            i += 2
            continue
        if c == "\x08":
            if out:
                out.pop()
            i += 1
            continue
        # Drop every other C0 control byte. BEL in particular reaches us
        # whenever readline rejects a key, and appending it renders a stray
        # glyph in the middle of the line — which is what "weird characters"
        # in the shell actually were. Keep \n and \t; everything else in
        # 0x00-0x1f and 0x7f has no printable meaning here.
        if (c < " " and c not in "\n\t") or c == "\x7f":
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)
