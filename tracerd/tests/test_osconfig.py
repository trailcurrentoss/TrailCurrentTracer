"""OS-owned settings: parsing, validation, and the Settings screen contract.

WHY THIS FILE EXISTS
--------------------
These settings are the only way to fix a clock or a time zone once an image is
flashed — the device boots into a kiosk with no desktop, terminal or login
prompt behind it. Two failure modes matter, and both are silent:

  * A row whose key nothing handles. It renders, it accepts a value, and
    nothing happens. The contract tests below tie every row in the System
    group to a handler and every picker to an option list, so a typo is a test
    failure rather than a dead row discovered in a vehicle bay.

  * A locale selected but never generated. localectl accepts a LANG that does
    not exist, and the system falls back to C at the next boot with nothing
    shown anywhere. set_locale must verify, not trust an exit code.

Nothing here runs a system command: every test drives the parsers directly or
substitutes the command runner.

Run with: python3 -m unittest discover -s tests   (or `make test`)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracerd.modules import osconfig, settings  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_timedatectl_show_is_key_value(self):
        parsed = osconfig._kv(
            "Timezone=America/Denver\nLocalRTC=no\nNTP=yes\nNTPSynchronized=no\n")
        self.assertEqual(parsed["Timezone"], "America/Denver")
        self.assertEqual(parsed["NTP"], "yes")
        self.assertEqual(parsed["NTPSynchronized"], "no")

    def test_localectl_status_yields_lang_and_keymap(self):
        out = osconfig._parse_localectl(
            "   System Locale: LANG=en_GB.UTF-8\n"
            "       VC Keymap: gb\n"
            "      X11 Layout: gb\n")
        self.assertEqual(out["locale"], "en_GB.UTF-8")
        self.assertEqual(out["keymap"], "gb")

    def test_unset_keymap_is_none_not_the_string_na(self):
        """`n/a` rendered verbatim reads as a configured value."""
        out = osconfig._parse_localectl(
            "   System Locale: LANG=C.UTF-8\n       VC Keymap: n/a\n")
        self.assertIsNone(out["keymap"])

    def test_locale_names_compare_across_both_spellings(self):
        """`locale -a` says en_US.utf8; SUPPORTED says en_US.UTF-8."""
        self.assertEqual(osconfig._normalise_locale("en_US.utf8"),
                         osconfig._normalise_locale("en_US.UTF-8"))


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    """Input is rejected here, before it reaches a system command."""

    async def test_wifi_region_must_be_two_letters(self):
        for bad in ("USA", "u", "", "12", "United States"):
            with self.assertRaises(osconfig.OSConfigError):
                await osconfig.set_wifi_region(bad)

    async def test_clock_rejects_an_unparseable_time(self):
        with self.assertRaises(osconfig.OSConfigError) as cm:
            await osconfig.set_time("half past two")
        # The message has to say what shape to type — this lands on a 3.5"
        # screen with no other documentation.
        self.assertIn(osconfig.TIME_EXAMPLE, str(cm.exception))

    async def test_clock_is_refused_while_automatic_time_is_on(self):
        """systemd rejects it anyway; being told why beats snapping back."""
        calls = []

        async def fake_run(*argv, timeout=None):
            calls.append(argv)
            if argv[:2] == ("timedatectl", "show"):
                return "NTP=yes\nTimezone=UTC\n"
            return ""

        osconfig._run, real = fake_run, osconfig._run
        try:
            with self.assertRaises(osconfig.OSConfigError) as cm:
                await osconfig.set_time("2026-09-01 14:30")
        finally:
            osconfig._run = real
        self.assertIn("Automatic time", str(cm.exception))
        self.assertNotIn(("timedatectl", "set-time"),
                         [c[:2] for c in calls], "the clock was set anyway")

    async def test_a_locale_that_cannot_be_generated_is_not_selected(self):
        """The trap the sudoers grant exists for.

        localectl accepts a LANG that was never generated and the system falls
        back to C at next boot with no error anywhere, so the generate step
        must be verified rather than trusted.
        """
        ran = []

        async def fake_run(*argv, timeout=None):
            ran.append(argv)
            if argv[:2] == ("locale", "-a"):
                return "C\nC.UTF-8\nen_US.utf8\n"      # never gains the new one
            return ""

        osconfig._run, real = fake_run, osconfig._run
        # Any existing path stands in for the installed helper; the point of
        # this test is what happens AFTER it runs and produces nothing.
        osconfig.LOCALE_GEN, real_gen = "/bin/sh", osconfig.LOCALE_GEN
        try:
            with self.assertRaises(osconfig.OSConfigError) as cm:
                await osconfig.set_locale("fr_FR.UTF-8")
        finally:
            osconfig._run = real
            osconfig.LOCALE_GEN = real_gen
        self.assertIn("fall back to C", str(cm.exception))
        self.assertNotIn("set-locale", [a for c in ran for a in c],
                         "LANG was set to a locale that does not exist")

    async def test_a_missing_helper_names_itself_rather_than_the_locale(self):
        """"could not be generated" sent an operator looking at the locale.

        The real answer, on an image built before the helper existed, is that
        the machinery to generate one was never installed. Say that.
        """
        async def fake_run(*argv, timeout=None):
            return "C\nen_GB.utf8\n" if argv[:2] == ("locale", "-a") else ""

        osconfig._run, real = fake_run, osconfig._run
        osconfig.LOCALE_GEN, real_gen = "/nonexistent/tracer-locale-gen", osconfig.LOCALE_GEN
        try:
            with self.assertRaises(osconfig.OSConfigError) as cm:
                await osconfig.set_locale("fr_FR.UTF-8")
        finally:
            osconfig._run = real
            osconfig.LOCALE_GEN = real_gen
        self.assertIn("not installed", str(cm.exception))

    async def test_an_already_generated_locale_skips_generation_entirely(self):
        """The common path on a built image, where the ten are pre-generated."""
        ran = []

        async def fake_run(*argv, timeout=None):
            ran.append(argv)
            # SUPPORTED spelling in, `locale -a` spelling out.
            return "C\nen_US.utf8\n" if argv[:2] == ("locale", "-a") else ""

        osconfig._run, real = fake_run, osconfig._run
        try:
            await osconfig.set_locale("en_US.UTF-8")
        finally:
            osconfig._run = real
        self.assertIn(("localectl", "set-locale", "LANG=en_US.UTF-8"), ran)
        self.assertFalse([c for c in ran if "sudo" in c],
                         "regenerated a locale that already existed")


class LocaleGeneratorScriptTests(unittest.TestCase):
    """The wrapper the image installs to generate a locale.

    Debian's locale-gen takes no locale argument: it regenerates whatever is
    uncommented in /etc/locale.gen and exits 0 either way, so calling it with
    a locale name looks like it worked and generates nothing. That is exactly
    what happened — `en_US.UTF-8` was selected, locale-gen was handed the name
    and ignored it, and the verification correctly refused the selection.

    These tests run the real script with stubbed `locale-gen` and `locale` on
    PATH, so its editing of locale.gen is exercised without generating
    anything on the machine running the tests.
    """

    SCRIPT = (Path(__file__).resolve().parents[2]
              / "image" / "layer" / "files" / "tracer-locale-gen")

    def setUp(self):
        if not self.SCRIPT.is_file():
            self.skipTest("tracer-locale-gen not present")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = Path(self.tmp) / "bin"
        self.bin.mkdir()
        self.locale_gen = Path(self.tmp) / "locale.gen"
        self.supported = Path(self.tmp) / "SUPPORTED"
        self.supported.write_text(
            "en_US.UTF-8 UTF-8\nen_GB.UTF-8 UTF-8\nfr_FR.UTF-8 UTF-8\n"
            "ja_JP.EUC-JP EUC-JP\n")
        self.locale_gen.write_text(
            "# en_US.UTF-8 UTF-8\nen_GB.UTF-8 UTF-8\n# fr_FR.UTF-8 UTF-8\n")

    def _stub(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    def _run(self, locale, generated=("en_GB.utf8",), gen_adds=True):
        """Run the script with `locale` and `locale-gen` stubbed.

        `gen_adds` False models Debian's real behaviour for an unlisted
        locale: locale-gen succeeds and produces nothing.
        """
        state = Path(self.tmp) / "generated"
        state.write_text("\n".join(generated) + "\n")
        self._stub("locale", f'[ "$1" = "-a" ] && cat "{state}"\n')
        # The stub mimics locale-gen: it only ever generates what is
        # uncommented in locale.gen, and it always exits 0.
        add = (f'grep -vE "^[[:space:]]*#" "{self.locale_gen}" '
               f'| awk "{{print \\$1}}" | sed "s/UTF-8/utf8/" >> "{state}"\n'
               if gen_adds else "")
        self._stub("locale-gen", add + "exit 0\n")

        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        script = self.SCRIPT.read_text().replace(
            "SUPPORTED=/usr/share/i18n/SUPPORTED", f"SUPPORTED={self.supported}"
        ).replace("LOCALEGEN=/etc/locale.gen", f"LOCALEGEN={self.locale_gen}")
        path = Path(self.tmp) / "script"
        path.write_text(script)
        path.chmod(0o755)
        return subprocess.run([str(path), locale], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_a_commented_locale_is_uncommented_and_generated(self):
        """The case that failed: en_US.UTF-8 present but commented out."""
        res = self._run("en_US.UTF-8")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertRegex(self.locale_gen.read_text(),
                         r"(?m)^en_US\.UTF-8 UTF-8$")

    def test_an_absent_locale_is_appended(self):
        self.locale_gen.write_text("en_GB.UTF-8 UTF-8\n")
        res = self._run("fr_FR.UTF-8")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("fr_FR.UTF-8 UTF-8", self.locale_gen.read_text())

    def test_an_already_generated_locale_succeeds_without_editing(self):
        before = self.locale_gen.read_text()
        res = self._run("en_GB.UTF-8", generated=("en_GB.utf8",))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.locale_gen.read_text(), before)

    def test_a_locale_the_system_does_not_support_is_refused(self):
        res = self._run("xx_XX.UTF-8")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not a locale this system supports", res.stderr)

    def test_generation_that_produces_nothing_is_a_failure(self):
        """locale-gen exits 0 even when it generates nothing — verify, do not trust."""
        res = self._run("fr_FR.UTF-8", gen_adds=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("did not produce it", res.stderr)

    def test_the_line_is_not_appended_twice(self):
        """A duplicate makes locale-gen rebuild it on every future run."""
        self._run("fr_FR.UTF-8")
        self._run("fr_FR.UTF-8", generated=("en_GB.utf8",))
        self.assertEqual(self.locale_gen.read_text().count("fr_FR.UTF-8"), 1)

    def test_it_takes_exactly_one_argument(self):
        res = subprocess.run([str(self.SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("usage", res.stderr.lower())


class SudoersContractTests(unittest.TestCase):
    """The grant must name the wrapper, not Debian's locale-gen."""

    SUDOERS = (Path(__file__).resolve().parents[2]
               / "image" / "layer" / "files" / "010_tracer-system")

    def setUp(self):
        if not self.SUDOERS.is_file():
            self.skipTest("sudoers drop-in not present")
        self.text = self.SUDOERS.read_text()

    def test_the_wrapper_is_what_is_granted(self):
        self.assertIn(osconfig.LOCALE_GEN, self.text)

    def test_bare_locale_gen_is_not_granted(self):
        """Granting it directly reads as working and generates nothing."""
        for line in self.text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            self.assertNotIn("/usr/sbin/locale-gen", line)


class SystemGroupContractTests(unittest.IsolatedAsyncioTestCase):
    """Every row in the System group must actually do something.

    A row whose key no handler recognises renders normally, accepts a value
    and silently discards it. That is indistinguishable from broken hardware,
    and it is the exact shape of bug the polkit rule's own comments describe.
    """

    def setUp(self):
        self.group = next(g for g in settings.GROUPS
                          if g["meta"]["id"] == "system")
        self.mod = settings.SettingsModule.__new__(settings.SettingsModule)
        self.mod.mock = False

    def test_the_group_exists_and_is_reachable(self):
        self.assertTrue(self.group["searchIndex"])

    def test_every_row_is_marked_os_owned(self):
        """Without source:"os" the UI reads the JSON store and shows nothing."""
        for item in self.group["searchIndex"]:
            self.assertEqual(item.get("source"), "os", item["label"])

    async def test_every_row_key_has_a_handler(self):
        for item in self.group["searchIndex"]:
            with self.subTest(row=item["label"]):
                try:
                    await self.mod._set_system(item["key"], "")
                except Exception as exc:
                    self.assertNotIn("unknown system setting", str(exc),
                                     f"{item['key']} reaches no handler")

    async def test_every_picker_names_a_list_the_daemon_can_produce(self):
        mock = settings.SettingsModule.__new__(settings.SettingsModule)
        mock.mock = True
        for item in self.group["searchIndex"]:
            if item["type"] != "picker":
                continue
            with self.subTest(row=item["label"]):
                self.assertIn("options", item,
                              "a picker with no option list opens empty")
                # The mock lists cover the same keys, so this proves the name
                # is one _options() knows without touching the host system.
                self.assertTrue(await mock._options(item["options"]),
                                f"{item['options']} produced no options")

    async def test_an_unknown_option_list_is_an_error_not_an_empty_screen(self):
        mock = settings.SettingsModule.__new__(settings.SettingsModule)
        mock.mock = False
        with self.assertRaises(Exception):
            await mock._options("nonexistent")

    def test_the_clock_row_advertises_the_format_it_accepts(self):
        row = next(i for i in self.group["searchIndex"] if i["key"] == "time")
        self.assertEqual(row.get("example"), osconfig.TIME_EXAMPLE)


class MockSafetyTests(unittest.IsolatedAsyncioTestCase):
    """`make mock` runs on a workstation and must not touch its clock."""

    async def test_set_system_is_refused_in_mock(self):
        mod = settings.SettingsModule.__new__(settings.SettingsModule)
        mod.mock = True
        with self.assertRaises(Exception):
            await mod.handle("set_system", {"key": "timezone", "value": "UTC"})


if __name__ == "__main__":
    unittest.main()
