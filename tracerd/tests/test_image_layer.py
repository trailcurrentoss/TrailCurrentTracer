"""Gates on the image layer, so upstream drift fails in a second not an hour.

WHY THIS FILE EXISTS
--------------------
A build that fails after `sudo ./image/build.sh` has already cost 30-45 minutes,
and the three failures that actually happened all pointed somewhere other than
their cause:

  * `-D` was renamed `-S` upstream, so the source directory fell through and
    was parsed as an override. The error named the DIRECTORY, which reads as a
    problem with the path rather than with a flag that no longer exists.
  * `rpi-user-credentials` was renamed `device-user-credentials`. The error was
    "Missing required dependency", which reads as though our layer were broken
    rather than as upstream having moved.
  * A `#` comment added INSIDE the METABEGIN/METAEND block. That block is
    DEB822: an unindented line that is not `Key: value` is parsed as a
    malformed continuation, and the layer silently fails to load with
    "Layer 'tracer-base' not found" — which does not mention comments, DEB822,
    or the line at fault.

Each of those was written up as a comment warning the next person. A comment
does not stop anyone doing it again; these tests do. They need no root, no
network and no build — they read the same files rpi-image-gen reads.

Run with: python3 -m unittest discover -s tests   (or `make test`)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]
LAYER = REPO / "image" / "layer" / "tracer-base.yaml"
BUILD_SH = REPO / "image" / "build.sh"
RPIIG = REPO / "image" / "work" / "rpi-image-gen"


def _meta_lines():
    """The raw lines between METABEGIN and METAEND, comment marker stripped."""
    out, inside = [], False
    for raw in LAYER.read_text().splitlines():
        stripped = raw.strip()
        if stripped == "# METABEGIN":
            inside = True
            continue
        if stripped == "# METAEND":
            break
        if inside:
            # Strip the comment marker AND the single space that conventionally
            # follows it. Stripping only "#" leaves every line beginning with a
            # space, which makes an indentation check pass for everything —
            # including the prose line this whole test exists to catch.
            if raw.startswith("# "):
                out.append(raw[2:])
            elif raw.startswith("#"):
                out.append(raw[1:])
            else:
                out.append(raw)
    return out


class LayerMetadataTests(unittest.TestCase):
    """The METABEGIN block is DEB822 and must parse as such."""

    FIELD = re.compile(r"^ ?([A-Za-z0-9][A-Za-z0-9-]*): ?(.*)$")

    def setUp(self):
        if not LAYER.is_file():
            self.skipTest("image layer not present")
        self.lines = _meta_lines()

    def test_the_block_exists(self):
        self.assertTrue(self.lines, "METABEGIN/METAEND block missing or empty")

    def test_every_line_is_a_field_or_an_indented_continuation(self):
        """The exact failure: a prose comment inside the block.

        DEB822 reads an unindented non-field line as a continuation of the
        previous field, and rpi-image-gen then reports only
        "Layer 'tracer-base' not found".
        """
        for line in self.lines:
            with self.subTest(line=line):
                if line.startswith((" ", "\t")):
                    continue                       # valid continuation
                self.assertRegex(
                    line, self.FIELD,
                    "not a 'Key: value' field and not indented — DEB822 will "
                    "read this as a malformed continuation and the layer will "
                    "fail to load. Put prose ABOVE METABEGIN.")

    def test_required_fields_present(self):
        fields = {m.group(1) for m in
                  (self.FIELD.match(l) for l in self.lines) if m}
        for name in ("X-Env-Layer-Name", "X-Env-Layer-Requires",
                     "X-Env-Layer-Provides"):
            self.assertIn(name, fields)


class LayerDependencyTests(unittest.TestCase):
    """Every required layer must exist in the PINNED rpi-image-gen checkout."""

    def setUp(self):
        if not LAYER.is_file():
            self.skipTest("image layer not present")
        if not RPIIG.is_dir():
            # The checkout only exists after a build has been attempted. Skip
            # rather than fail: a fresh clone has not fetched it yet.
            self.skipTest("rpi-image-gen not cloned yet (run image/build.sh)")
        self.requires = []
        for line in _meta_lines():
            m = re.match(r"^ ?X-Env-Layer-Requires: ?(.*)$", line)
            if m:
                self.requires = [x.strip() for x in m.group(1).split(",") if x.strip()]

    def test_requires_is_not_empty(self):
        self.assertTrue(self.requires)

    def test_every_required_layer_resolves_to_a_file(self):
        """Catches an upstream rename before it costs a build.

        rpi-image-gen reports these as "Missing required dependency", which
        reads as though this layer were at fault rather than upstream having
        moved the target.
        """
        for name in self.requires:
            with self.subTest(layer=name):
                self.assertTrue(
                    self._resolves(name),
                    f"{name!r} does not exist in the pinned rpi-image-gen "
                    f"checkout. Upstream most likely renamed it — find the new "
                    f"name under {RPIIG}/layer and update X-Env-Layer-Requires.")

    @staticmethod
    def _resolves(name: str) -> bool:
        """Search only where layers live.

        NOT rglob over the whole checkout: `work/` holds a root-owned chroot
        with /proc entries in it, and walking that raises PermissionError for
        reasons that have nothing to do with the layer being present.
        """
        for sub in ("layer", "config", "device", "image"):
            root = RPIIG / sub
            if not root.is_dir():
                continue
            try:
                if any(root.rglob(f"{name}.yaml")):
                    return True
            except (PermissionError, OSError):
                continue
        return False


class BuildInvocationTests(unittest.TestCase):
    """The build.sh call must match the pinned tool's actual CLI."""

    def setUp(self):
        if not BUILD_SH.is_file():
            self.skipTest("build.sh not present")
        self.text = BUILD_SH.read_text()

    def test_upstream_is_pinned(self):
        """Tracking main means the build breaks on someone else's commit."""
        self.assertRegex(self.text, r"RPIIG_REF=",
                         "rpi-image-gen is not pinned; a CLI change upstream "
                         "will break the build with an unrelated-looking error")

    def test_the_renamed_flag_is_not_used(self):
        """-D was renamed -S; with -D the source dir is parsed as an override."""
        # (?m) so ^ anchors per line — without it these only match at the very
        # start of the file and the assertions are meaningless.
        self.assertNotRegex(self.text, r"(?m)^\s*-D\s",
                            "-D no longer exists upstream; use -S")
        self.assertRegex(self.text, r"(?m)^\s*-S\s",
                         "the custom-sources flag (-S) is missing")

    def test_overrides_are_separated(self):
        """Without a bare `--`, the first key=value is eaten as an option arg."""
        start = self.text.find("rpi-image-gen build")
        self.assertNotEqual(start, -1, "could not find the build invocation")
        call = self.text[start:]
        # A separator line is `--` alone, allowing for a trailing line
        # continuation backslash: the real call reads "    -- \".
        sep = re.search(r"(?m)^\s*--\s*\\?\s*$", call)
        self.assertIsNotNone(
            sep, "overrides must follow a bare `--` separator")
        first_override = call.find("IGconf_")
        self.assertNotEqual(first_override, -1, "no IGconf_ overrides found")
        self.assertLess(sep.start(), first_override,
                        "`--` must come BEFORE the IGconf_ overrides")


class GeneratedConfigTests(unittest.TestCase):
    """Boot settings that fail silently on hardware if they go missing."""

    def setUp(self):
        if not LAYER.is_file():
            self.skipTest("image layer not present")
        self.text = LAYER.read_text()

    def _directive(self, line: str):
        """Assert a real config.txt line, not a mention inside a comment.

        A substring search passes as long as the string appears ANYWHERE —
        including in the paragraph explaining why the directive matters. That
        is a test that cannot fail for the reason it was written, which is
        exactly the failure mode these gates exist to prevent.
        """
        self.assertRegex(
            self.text, rf"(?m)^\s*{re.escape(line)}\s*$",
            f"{line!r} is not present as a config.txt directive (a mention in "
            f"a comment does not count)")

    def test_cooling_fan_is_enabled(self):
        """Without it the fan spins at power-on and then never again.

        The base DTB ships cooling_fan disabled and relies on firmware
        detection, which does not fire on this chassis. No cooling device gets
        bound, so the trip points have nothing to drive.
        """
        self._directive("dtparam=cooling_fan=on")

    def test_touch_overlay_is_ours_not_the_vendor_one(self):
        self._directive("dtoverlay=tracer-gt911")

    def test_usb_host_mode_is_set(self):
        """Without it the keyboard never enumerates and no button works."""
        self._directive("dtoverlay=dwc2,dr_mode=host")


if __name__ == "__main__":
    unittest.main()
