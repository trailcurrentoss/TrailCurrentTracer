"""The serial reader's disconnect contract.

WHY THIS FILE EXISTS
--------------------
The reader treated any zero-byte read as EOF and ended the session. On a tty
that is wrong: with VMIN=0 a read of an idle port returns zero bytes rather
than EAGAIN, and asyncio's add_reader is level-triggered, so each burst of
data produced a surplus wakeup after the buffer was already drained. Against a
live ESP32-S3 printing its help text, 24 of 103 wakeups read nothing — and
every one of them was called a disconnect.

The visible damage was not "it disconnects". The watcher re-attached within
20 ms, so the port was almost always open and typed commands still ran. What
the operator saw was the console filling with disconnected/re-attached pairs,
and the line they were typing vanishing as each re-open cleared `partial`.

So: a zero-byte read is only a disconnect when the kernel reports POLLHUP,
and the port is configured VMIN=1 so an idle read raises EAGAIN instead.
Both are asserted here because either one alone would let the bug back in.

Run with: python3 -m unittest discover -s tests   (or `make test`)
"""

import asyncio
import os
import pty
import sys
import termios
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracerd.modules import moduledebug  # noqa: E402


class HangupDetectionTests(unittest.TestCase):
    """_hung_up() is the only thing allowed to declare a port gone."""

    def setUp(self):
        self.controller, self.peer = pty.openpty()
        self.addCleanup(self._close, self.peer)
        self.addCleanup(self._close, self.controller)

    @staticmethod
    def _close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_idle_port_is_not_hung_up(self):
        """The exact case that was misread as EOF: open, quiet, still there."""
        self.assertFalse(moduledebug._hung_up(self.peer))

    def test_port_with_data_waiting_is_not_hung_up(self):
        os.write(self.controller, b"antenna> ")
        self.assertFalse(moduledebug._hung_up(self.peer))

    def test_closed_far_end_is_hung_up(self):
        """A real unplug must still end the session immediately."""
        os.close(self.controller)
        self.controller = -1
        self.assertTrue(moduledebug._hung_up(self.peer))


class PortConfigurationTests(unittest.TestCase):
    def test_vmin_is_one_so_an_idle_read_raises_eagain(self):
        """VMIN=0 makes an idle read indistinguishable from EOF.

        The reader already handles BlockingIOError as idleness, so VMIN=1
        routes the harmless case to the harmless branch.
        """
        controller, peer = pty.openpty()
        try:
            mod = moduledebug.ModuleDebugModule.__new__(
                moduledebug.ModuleDebugModule)
            mod._configure(peer, 115200)
            cc = termios.tcgetattr(peer)[6]
            self.assertEqual(cc[termios.VMIN], 1)
            self.assertEqual(cc[termios.VTIME], 0)
        finally:
            os.close(peer)
            os.close(controller)

    def test_configure_leaves_echo_and_canonical_off(self):
        """A monitor must not edit or echo — the module owns the line."""
        controller, peer = pty.openpty()
        try:
            mod = moduledebug.ModuleDebugModule.__new__(
                moduledebug.ModuleDebugModule)
            mod._configure(peer, 115200)
            lflag = termios.tcgetattr(peer)[3]
            self.assertFalse(lflag & termios.ECHO)
            self.assertFalse(lflag & termios.ICANON)
        finally:
            os.close(peer)
            os.close(controller)


class FakeHub:
    """Captures published snapshots instead of sending them."""

    def __init__(self):
        self.snaps = []
        self.modules = {}
        self.loop = None

    def broadcast_snap(self, name, seq, snap):
        self.snaps.append(snap)

    def broadcast_ev(self, name, data):
        pass


class BurstPublishTests(unittest.IsolatedAsyncioTestCase):
    """A command's output must reach the GUI without another keypress.

    The reader coalesces reads at 50 ms so one `help` reply is not dozens of
    frames. With no trailing flush that threw away the END of every burst: the
    first chunk painted, the rest waited for the 2 s poll. Typing looked fine
    (keystrokes are slower than 50 ms), but Enter painted only the echoed
    newline — so the operator pressed Enter again, and that keypress is what
    flushed the output. Enter appeared to need pressing twice.
    """

    async def asyncSetUp(self):
        self.controller, peer = pty.openpty()
        # Match how _open() presents the fd. Without O_NONBLOCK a spurious
        # readable event blocks the whole event loop inside os.read().
        os.set_blocking(peer, False)
        self.hub = FakeHub()
        self.mod = moduledebug.ModuleDebugModule(self.hub, mock=True)
        self.mod._configure(peer, 115200)
        self.mod._fd = peer
        self.mod._port = "/dev/pts/test"
        self.reader = asyncio.create_task(self.mod._reader())
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.reader.cancel()
        for fd in (self.controller, self.mod._fd):
            try:
                os.close(fd)
            except (OSError, TypeError):
                pass

    async def test_the_tail_of_a_burst_is_published(self):
        """Chunks arriving inside one throttle window must still land."""
        # Deliberately faster than PUSH_SETTLE, like a real module answering.
        for line in (b"help  [<string>]\r\n", b"  Print the summary\r\n",
                     b"scan  One-off AP census\r\n", b"antenna> "):
            os.write(self.controller, line)
            await asyncio.sleep(0.002)

        # Settle, but nowhere near the 2 s poll that used to be the only
        # thing that published this.
        await asyncio.sleep(self.mod.PUSH_SETTLE * 4)

        published = [s for s in self.hub.snaps if s.get("data")]
        self.assertTrue(published, "nothing was published at all")
        texts = [line["text"] for line in published[-1]["data"]["lines"]]
        self.assertIn("scan  One-off AP census", texts,
                      "the last line of the burst never reached the GUI")
        # The prompt has no newline, so it can only arrive via `partial`.
        self.assertEqual(published[-1]["data"]["partial"], "antenna> ")

    async def test_a_single_keystroke_echo_still_publishes_at_once(self):
        """The coalescing must not add latency to ordinary typing."""
        os.write(self.controller, b"h")
        await asyncio.sleep(self.mod.PUSH_SETTLE * 2)
        published = [s for s in self.hub.snaps if s.get("data")]
        self.assertTrue(published)
        self.assertEqual(published[-1]["data"]["partial"], "h")


class ScrollbackOwnershipTests(unittest.TestCase):
    """Scrollback belongs to a module, not to the app.

    Plugging in a second board and connecting to it left the first board's
    output on screen under the second board's name in the header — which is
    how an operator ends up reading one module's log while debugging another.
    The same reset must NOT fire when the watcher re-attaches to the same
    module after a power-cycle: that history is the reason for the wait.
    """

    def setUp(self):
        self.mod = moduledebug.ModuleDebugModule.__new__(
            moduledebug.ModuleDebugModule)
        self.mod._lines = []
        self.mod._identity = None

    def _connect(self, identity):
        """The scrollback decision _open() makes, in isolation."""
        if identity != self.mod._identity:
            self.mod._lines = []
        self.mod._identity = identity
        self.mod._add(f"— connected to {identity} —", "meta")

    def test_a_different_module_starts_a_clean_console(self):
        self._connect("44:1B:F6:84:18:90")
        self.mod._add("antenna> scan", "raw")
        self._connect("7C:DF:A1:00:11:22")
        self.assertEqual(len(self.mod._lines), 1)
        self.assertNotIn("scan", self.mod._lines[0]["text"])

    def test_the_same_module_keeps_its_history(self):
        """A power-cycle is how you see boot output — do not throw it away."""
        self._connect("44:1B:F6:84:18:90")
        self.mod._add("antenna> scan", "raw")
        self._connect("44:1B:F6:84:18:90")
        texts = [line["text"] for line in self.mod._lines]
        self.assertIn("antenna> scan", texts)


if __name__ == "__main__":
    unittest.main()
