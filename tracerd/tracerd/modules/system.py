"""system — OS-level settings the kiosk would otherwise make unreachable.

WHY THIS MODULE EXISTS
----------------------
The device boots straight into a full-screen GUI. There is no desktop, no
terminal emulator and no login prompt, so time zone, clock, locale and WiFi
regulatory region have no other route in. On any normal Debian box these are
"just run timedatectl" — here that option does not exist for the operator.

The clock in particular is not a preference. A wrong clock breaks TLS to the
Headwaters broker (certificate not yet valid) and makes captured sessions
impossible to line up against Headwaters' own logs, so a technician chasing a
bug can be defeated by a dead RTC before they start.

PRIVILEGE
---------
Nothing here runs as root by itself. Two grants installed by the image do the
work, and both are narrow by construction:

  * 50-tracer-timedate.rules  — polkit, covers timedatectl/localectl (D-Bus)
  * 010_tracer-system         — sudoers, covers locale-gen and raspi-config,
                                which have no D-Bus interface

If either is missing the commands fail with "Interactive authentication
required" on stderr while still exiting through a pipe cleanly, which reads
in the GUI as "the setting silently does nothing". `_run` therefore checks
the return code and surfaces stderr verbatim rather than trusting exit paths.

LIST SIZES
----------
There are 485 time zones and 279 countries. Those never change at runtime, so
they are read once, cached, and served only when the GUI asks — polling them
would put a quarter of a megabyte through the WebSocket every few seconds for
data that is constant.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .base import Module, Unavailable

# Locales the image pre-generates. Offering a locale that was never generated
# is a trap: localectl accepts it, and the system falls back to C at the next
# boot with nothing shown anywhere. Kept in step with tracer-base.yaml.
CURATED_LOCALES = [
    "en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8",
    "es_ES.UTF-8", "it_IT.UTF-8", "pt_BR.UTF-8", "nl_NL.UTF-8",
    "sv_SE.UTF-8", "ja_JP.UTF-8",
]

ISO3166 = Path("/usr/share/zoneinfo/iso3166.tab")


async def _run(*argv: str, timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a command, returning (rc, stdout, stderr).

    Every caller checks rc. The failure mode this guards against is a polkit
    or sudo refusal, which writes to stderr and is trivially lost if only
    stdout is inspected.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return 124, "", f"{argv[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not installed"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _friendly(stderr: str, what: str) -> str:
    """Turn a tool's error into something a technician can act on."""
    s = stderr.strip()
    if "Interactive authentication required" in s:
        return (f"Not permitted to change {what}. The polkit rule or sudoers "
                f"drop-in is missing from this image.")
    return s.splitlines()[0] if s else f"Could not change {what}"


class SystemModule(Module):
    name = "system"
    interval = 5.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self._zones: list[str] | None = None
        self._countries: list[dict] | None = None
        self._locales: list[str] | None = None
        self._region: str | None = None
        self._region_read = 0.0

    # ── read ─────────────────────────────────────────────────────────
    async def poll(self):
        rc, out, err = await _run("timedatectl", "show")
        if rc != 0:
            raise Unavailable("systemd time service not answering")
        kv = {}
        for line in out.splitlines():
            k, _, v = line.partition("=")
            kv[k] = v

        loc_rc, loc_out, _ = await _run("localectl", "status")
        locale, keymap = "", ""
        for line in loc_out.splitlines():
            t = line.strip()
            if t.startswith("System Locale:"):
                locale = t.split(":", 1)[1].strip()
                if locale.startswith("LANG="):
                    locale = locale[5:]
            elif t.startswith("VC Keymap:"):
                keymap = t.split(":", 1)[1].strip()

        # raspi-config shells out to read config files; once a minute is
        # plenty for a value that only changes when the operator changes it.
        if time.time() - self._region_read > 60:
            r_rc, r_out, _ = await _run(
                "sudo", "-n", "/usr/bin/raspi-config", "nonint",
                "get_wifi_country")
            self._region = r_out.strip() if r_rc == 0 and r_out.strip() else None
            self._region_read = time.time()

        ntp = kv.get("NTP") == "yes"
        synced = kv.get("NTPSynchronized") == "yes"

        return {
            "timezone": kv.get("Timezone", ""),
            "ntp": ntp,
            "synced": synced,
            # Read from our own clock, NOT from timedatectl. systemd v250+
            # pretty-prints TimeUSec as "Tue 2026-09-01 17:05:22 CDT" rather
            # than microseconds, so parsing it as an int throws — and the
            # module then reports `unavailable` for a machine whose time
            # service is perfectly healthy. The daemon shares this clock, so
            # there is nothing to parse in the first place.
            "epoch": int(time.time()),
            "rtc_local": kv.get("LocalRTC") == "yes",
            "locale": locale,
            "keymap": keymap or None,
            "region": self._region,
            # The clock being unsynchronized while NTP is on is the state that
            # actually bites (TLS failures), so it is called out rather than
            # left for the GUI to infer.
            "clock_warning": (
                "Clock not yet synchronized — TLS to Headwaters may fail"
                if ntp and not synced else None
            ),
        }

    # ── lists (static, cached, served on demand) ─────────────────────
    async def _zone_list(self) -> list[str]:
        if self._zones is None:
            rc, out, _ = await _run("timedatectl", "list-timezones")
            self._zones = out.split() if rc == 0 else []
        return self._zones

    async def _country_list(self) -> list[dict]:
        if self._countries is None:
            rows = []
            try:
                for line in ISO3166.read_text(errors="replace").splitlines():
                    if line.startswith("#") or "\t" not in line:
                        continue
                    code, name = line.split("\t", 1)
                    rows.append({"code": code.strip(), "name": name.strip()})
            except OSError:
                rows = []
            self._countries = sorted(rows, key=lambda r: r["name"])
        return self._countries

    async def _locale_list(self) -> list[str]:
        """Curated list, annotated with whether each is actually generated."""
        if self._locales is None:
            rc, out, _ = await _run("locale", "-a")
            have = {l.strip().lower().replace("utf8", "utf-8")
                    for l in out.splitlines()}
            self._locales = [
                {"value": l, "generated": l.lower() in have}
                for l in CURATED_LOCALES
            ]
        return self._locales

    # ── write ────────────────────────────────────────────────────────
    async def handle(self, op: str, args: dict):
        args = args or {}

        if op == "regions":
            zones = await self._zone_list()
            return sorted({z.split("/", 1)[0] for z in zones if "/" in z})

        if op == "zones":
            region = args.get("region", "")
            zones = await self._zone_list()
            return [z for z in zones if z.startswith(region + "/")]

        if op == "locales":
            return await self._locale_list()

        if op == "countries":
            return await self._country_list()

        if op == "set_timezone":
            tz = args.get("value", "")
            if tz not in await self._zone_list():
                return {"ok": False, "error": f"Unknown time zone {tz!r}"}
            rc, _, err = await _run("timedatectl", "set-timezone", tz)
            if rc != 0:
                return {"ok": False, "error": _friendly(err, "the time zone")}
            await self.refresh()
            return {"ok": True, "timezone": tz}

        if op == "set_ntp":
            on = bool(args.get("value"))
            rc, _, err = await _run("timedatectl", "set-ntp",
                                    "true" if on else "false")
            if rc != 0:
                return {"ok": False, "error": _friendly(err, "automatic time")}
            await self.refresh()
            return {"ok": True, "ntp": on}

        if op == "set_time":
            # systemd refuses a manual set while NTP owns the clock, and its
            # own error does not say so plainly. Say it here instead.
            if (self.data or {}).get("ntp"):
                return {"ok": False,
                        "error": "Turn Automatic time off before setting the clock"}
            stamp = args.get("value", "")
            rc, _, err = await _run("timedatectl", "set-time", stamp)
            if rc != 0:
                return {"ok": False, "error": _friendly(err, "the clock")}
            await self.refresh()
            return {"ok": True}

        if op == "set_locale":
            lang = args.get("value", "")
            known = {l["value"] for l in await self._locale_list()}
            if lang not in known:
                return {"ok": False, "error": f"Unsupported locale {lang!r}"}
            # Generate first. Setting a LANG that was never generated leaves
            # the system falling back to C at the next boot, silently.
            self.set_busy(True, f"generating {lang}")
            try:
                rc, _, err = await _run("sudo", "-n", "/usr/sbin/locale-gen",
                                        lang, timeout=180)
                if rc != 0:
                    return {"ok": False, "error": _friendly(err, "the locale")}
                rc, _, err = await _run("localectl", "set-locale", f"LANG={lang}")
                if rc != 0:
                    return {"ok": False, "error": _friendly(err, "the locale")}
            finally:
                self.set_busy(False)
            self._locales = None      # regenerated set has changed
            await self.refresh()
            return {"ok": True, "locale": lang,
                    "note": "Takes full effect after a restart"}

        if op == "set_region":
            cc = (args.get("value") or "").upper()
            if len(cc) != 2 or not cc.isalpha():
                return {"ok": False, "error": "Region must be a 2-letter code"}
            rc, _, err = await _run("sudo", "-n", "/usr/bin/raspi-config",
                                    "nonint", "do_wifi_country", cc, timeout=30)
            if rc != 0:
                return {"ok": False, "error": _friendly(err, "the WiFi region")}
            self._region_read = 0.0   # force a re-read on the next poll
            await self.refresh()
            return {"ok": True, "region": cc,
                    "note": "Rescan WiFi to see channels for this region"}

        return {"ok": False, "error": f"unknown op {op!r}"}

    def tile_status(self):
        d = self.data or {}
        tz = d.get("timezone") or "--"
        return tz.split("/")[-1].replace("_", " ")
