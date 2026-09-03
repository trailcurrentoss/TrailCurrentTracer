"""Smoke tests for the input dispatch path.

WHY THIS FILE EXISTS
--------------------
`_EDIT_KEYS` was referenced inside `_dispatch()` but never defined. Python
compiles that happily — a NameError only fires when the line actually runs.
`compileall` passed, the daemon started, every module reported `ok`, and text
entry was silently dead: every keystroke in text mode raised NameError inside
an asyncio callback, so passwords accepted nothing.

Anything reached only by a real keypress needs a test that presses a key.
Run with: python3 -m unittest discover -s tests   (or `make test`)
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracerd.modules import inputmod  # noqa: E402
from tracerd.modules.inputmod import NAV, TEXT  # noqa: E402


class FakeHub:
    """Captures broadcasts instead of sending them."""

    def __init__(self):
        self.events = []
        self.snaps = []
        self.loop = None
        self.modules = {}

    def broadcast_ev(self, name, data):
        self.events.append((name, data))

    def broadcast_snap(self, name, seq, snap):
        self.snaps.append((name, seq, snap))


class InputDispatchTests(unittest.TestCase):
    def setUp(self):
        self.hub = FakeHub()
        self.mod = inputmod.InputModule(self.hub, mock=True)

    # ── the regression this file was written for ─────────────────────
    def test_edit_keys_defined(self):
        self.assertTrue(hasattr(inputmod, "_EDIT_KEYS"))
        self.assertEqual(inputmod._EDIT_KEYS[14], "backspace")
        self.assertEqual(inputmod._EDIT_KEYS[28], "enter")

    def test_text_mode_emits_characters(self):
        """The exact path that raised NameError."""
        self.mod.set_mode(TEXT)
        self.mod._dispatch(30, "down")          # KEY_A
        self.assertEqual(self.hub.events[-1][1].get("text"), "a")

    def test_text_mode_emits_named_editing_keys(self):
        self.mod.set_mode(TEXT)
        self.mod._dispatch(14, "down")          # backspace
        self.assertEqual(self.hub.events[-1][1].get("key"), "backspace")
        self.mod._dispatch(28, "down")          # enter
        self.assertEqual(self.hub.events[-1][1].get("key"), "enter")

    def test_shift_capitalises(self):
        """Passwords contain capitals; without this they cannot be typed."""
        self.mod.set_mode(TEXT)
        self.mod._dispatch(42, "down")          # LEFTSHIFT held
        self.mod._dispatch(30, "down")          # KEY_A
        self.assertEqual(self.hub.events[-1][1].get("text"), "A")
        self.mod._dispatch(42, "up")            # released
        self.mod._dispatch(30, "down")
        self.assertEqual(self.hub.events[-1][1].get("text"), "a")

    # ── the modal contract from docs/controls.md ─────────────────────
    def test_nav_mode_letters_are_buttons_not_text(self):
        self.mod.set_mode(NAV)
        for code, btn in [(30, "a"), (48, "b"), (45, "x"),
                          (21, "y"), (38, "l"), (19, "r")]:
            self.hub.events.clear()
            self.mod._dispatch(code, "down")
            self.assertEqual(self.hub.events[-1][1].get("btn"), btn)
            self.assertIsNone(self.hub.events[-1][1].get("text"))

    def test_start_and_select_reach_the_gui_in_text_mode(self):
        """The Accept/Cancel contract for text fields.

        A, B, X, Y, L and R are literal letter keys, so in a text field they
        MUST type — which leaves B unable to cancel. start and select are the
        only two buttons carrying no character, so they are the only possible
        Accept/Cancel. The daemon forwards them; the GUI binds the meaning.
        """
        self.mod.set_mode(TEXT)
        for code, btn in [(119, "start"), (99, "select")]:
            self.hub.events.clear()
            self.mod._dispatch(code, "down")
            self.assertEqual(self.hub.events[-1][1].get("btn"), btn)
            self.assertIsNone(self.hub.events[-1][1].get("text"))

    def test_b_types_a_letter_in_text_mode(self):
        """The bug this contract exists to fix: B cannot be Cancel here."""
        self.mod.set_mode(TEXT)
        self.mod._dispatch(48, "down")          # KEY_B
        self.assertEqual(self.hub.events[-1][1].get("text"), "b")
        self.assertIsNone(self.hub.events[-1][1].get("btn"))

    def test_mode_resets_when_last_client_disconnects(self):
        """Otherwise a GUI crash mid-edit strands every button typing letters
        with no way back."""
        self.mod.set_mode(TEXT)
        self.mod.on_clients_changed(1)
        self.assertEqual(self.mod.mode, TEXT)
        self.mod.on_clients_changed(0)
        self.assertEqual(self.mod.mode, NAV)

    def test_shift_is_never_a_button(self):
        self.mod.set_mode(NAV)
        self.hub.events.clear()
        self.mod._dispatch(42, "down")
        self.assertEqual(self.hub.events, [])

    def test_dpad_still_navigates_in_text_mode(self):
        self.mod.set_mode(TEXT)
        self.mod._dispatch(103, "down")         # KEY_UP
        self.assertEqual(self.hub.events[-1][1].get("btn"), "dpad_up")


class KeymapTests(unittest.TestCase):
    def test_every_button_has_a_keycode(self):
        km = inputmod.load_keymap()
        expected = {
            "dpad_up", "dpad_down", "dpad_left", "dpad_right",
            "a", "b", "x", "y", "l", "r", "start", "select",
        }
        self.assertEqual(set(km["buttons"]), expected)

    def test_typeable_buttons_have_characters(self):
        """A button flagged typeable must actually produce a character, or
        text mode swallows it and the key appears dead."""
        km = inputmod.load_keymap()
        for name, b in km["buttons"].items():
            if b.get("typeable"):
                self.assertIn(int(b["code"]), inputmod._CHARS,
                              f"{name} is typeable but has no character")

    def test_start_and_select_are_not_typeable(self):
        """The modal escape depends on these two producing no character."""
        km = inputmod.load_keymap()
        for name in ("start", "select"):
            self.assertFalse(km["buttons"][name].get("typeable"))
            self.assertNotIn(int(km["buttons"][name]["code"]), inputmod._CHARS)


if __name__ == "__main__":
    unittest.main()
