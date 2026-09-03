"""osconfig — the OS-owned settings: clock, time zone, locale, WiFi region.

WHY THIS IS SEPARATE FROM settings.py
-------------------------------------
Everything in settings.py lives in one JSON file that the daemon owns. None of
the values here do. The clock, the time zone, the system locale and the WiFi
regulatory domain belong to the OS, and the OS is the only source of truth for
them. Keeping a copy in settings.json would drift the moment anything else
touched them — an NTP sync, a `timedatectl` over SSH, a fresh image — and a
settings screen that confidently displays a stale value is worse than one that
displays nothing.

So nothing here is stored. Every read asks the system, every write tells the
system, and the answer to "what is it now" is always a fresh read.

WHY THIS EXISTS AT ALL
----------------------
The device boots into a full-screen kiosk. There is no desktop, no terminal
emulator and no login prompt, so once an image is flashed there is no other
way to correct a time zone or set the clock. The privileges for exactly this
were shipped ahead of the feature — see image/layer/files/50-tracer-timedate.rules
(polkit, for the systemd D-Bus interfaces) and 010_tracer-system (sudo, for the
two binaries that have no D-Bus interface). This module is what uses them.

A wrong clock is not a cosmetic problem: it breaks TLS to the Headwaters broker
with "certificate is not yet valid", and it makes captured sessions impossible
to line up against Headwaters' own logs. Setting the time is a diagnostic
prerequisite.

EVERY FAILURE IS REPORTED
-------------------------
The one trap this module exists to avoid is the silent no-op. Without the
polkit rule, `timedatectl set-timezone` fails with "Interactive authentication
required" on stderr and a non-zero exit — and if the caller ignores either, the
setting simply appears not to work. So every command here checks the return
code and raises with the stderr text attached. Nothing swallows an error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime

log = logging.getLogger("tracerd.osconfig")

# How long any one system command may take. `timedatectl list-timezones` reads
# a bundled table and is instant; locale-gen genuinely takes a few seconds.
TIMEOUT = 20.0
LOCALE_GEN_TIMEOUT = 120.0

# The clock format the operator types. Seconds are optional because typing
# them on a handheld is a nuisance and nobody sets a wall clock to the second.
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
TIME_EXAMPLE = "2026-09-01 14:30"

# ISO 3166-1 alpha-2, which is what the regulatory domain is keyed on.
_REGION_RE = re.compile(r"^[A-Z]{2}$")


class OSConfigError(Exception):
    """A system command refused or failed. The message is shown to the operator."""


async def _run(*argv: str, timeout: float = TIMEOUT) -> str:
    """Run a command, or raise OSConfigError carrying what it said.

    Never returns partial success. A caller that only checks for an exception
    is correct; there is no exit code to remember to inspect.
    """
    if shutil.which(argv[0]) is None:
        raise OSConfigError(f"{argv[0]} is not installed on this image")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise OSConfigError(f"{argv[0]}: {exc.strerror}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise OSConfigError(f"{argv[0]} did not finish within {timeout:.0f}s")

    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip()
        detail = detail.splitlines()[-1] if detail else f"exit {proc.returncode}"
        # The polkit failure is the one an operator will actually hit on an
        # image built without the rule, so name the cause rather than echoing
        # systemd's wording.
        if "Interactive authentication required" in detail:
            raise OSConfigError(
                "Not permitted — the polkit rule for this image is missing")
        raise OSConfigError(detail)
    return (out or b"").decode("utf-8", "replace")


def _kv(text: str) -> dict[str, str]:
    """Parse `key=value` output (timedatectl show)."""
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ── reading ──────────────────────────────────────────────────────────
async def read_state() -> dict:
    """Everything the Settings screen shows, read fresh from the system.

    A failure in one area must not blank the others: a board without
    raspi-config still has a working clock, and the screen should say so
    rather than showing nothing. So each read is independently guarded and
    contributes `None` on failure, which the UI renders as "--".
    """
    state = {
        "timezone": None, "ntp": None, "ntp_synced": None, "local_rtc": None,
        "time": None, "locale": None, "keymap": None, "wifi_region": None,
        "errors": {},
    }

    try:
        show = _kv(await _run("timedatectl", "show"))
        state["timezone"] = show.get("Timezone") or None
        state["ntp"] = show.get("NTP") == "yes"
        state["ntp_synced"] = show.get("NTPSynchronized") == "yes"
        state["local_rtc"] = show.get("LocalRTC") == "yes"
        # Formatted here, not in the GUI: the daemon already knows the zone,
        # and shipping a preformatted string keeps the two from disagreeing.
        state["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except OSConfigError as exc:
        state["errors"]["clock"] = str(exc)

    try:
        state.update(_parse_localectl(await _run("localectl", "status")))
    except OSConfigError as exc:
        state["errors"]["locale"] = str(exc)

    try:
        # raspi-config prints the code on stdout; on a board that has never had
        # one set it exits non-zero, which _run turns into an exception.
        region = (await _run("sudo", "-n", "raspi-config", "nonint",
                             "get_wifi_country")).strip()
        state["wifi_region"] = region or None
    except OSConfigError as exc:
        # Not an error worth showing on every poll — plenty of boards have no
        # region set yet, and that is what the row is for.
        state["errors"]["wifi_region"] = str(exc)

    return state


def _parse_localectl(text: str) -> dict:
    """Pull LANG and the console keymap out of `localectl status`.

    Parsed rather than read from a file because /etc/default/locale and
    /etc/vconsole.conf are not both present on every image, whereas localectl
    reports whatever the running system actually resolved.
    """
    out = {"locale": None, "keymap": None}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("System Locale:"):
            rest = line.split(":", 1)[1].strip()
            for part in rest.split():
                if part.startswith("LANG="):
                    out["locale"] = part[5:]
        elif line.startswith("LANG="):
            # Continuation line when several locale variables are set.
            out["locale"] = out["locale"] or line[5:]
        elif line.startswith("VC Keymap:"):
            km = line.split(":", 1)[1].strip()
            out["keymap"] = None if km in ("n/a", "") else km
    return out


async def list_timezones() -> list[str]:
    return [z for z in (await _run("timedatectl", "list-timezones")).split() if z]


async def list_locales() -> list[str]:
    """Locales this image can offer, generated ones first.

    /usr/share/i18n/SUPPORTED is every locale the system COULD generate;
    `locale -a` is the much shorter list already generated. Both matter: the
    operator needs to be able to pick one that is not generated yet (that is
    what locale-gen is for), but the ones that work right now should be at the
    top rather than buried 400 entries down.
    """
    generated = set()
    try:
        for name in (await _run("locale", "-a")).split():
            generated.add(_normalise_locale(name))
    except OSConfigError:
        pass

    supported = []
    try:
        with open("/usr/share/i18n/SUPPORTED") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # "en_US.UTF-8 UTF-8" -> "en_US.UTF-8"
                name = line.split()[0]
                if name.upper().endswith("UTF-8"):
                    supported.append(name)
    except OSError:
        pass

    if not supported:
        # No SUPPORTED file (a minimal image). Offer what exists.
        return sorted(generated)

    ranked = sorted(set(supported),
                    key=lambda n: (_normalise_locale(n) not in generated, n))
    return ranked


def _normalise_locale(name: str) -> str:
    """`locale -a` says en_US.utf8; SUPPORTED says en_US.UTF-8. Same locale.

    Only the hyphen and the case differ between the two spellings, so
    stripping both is enough to compare them. The underscore separating
    language from territory is significant and is left alone.
    """
    return name.replace("-", "").lower()


async def list_keymaps() -> list[str]:
    return [k for k in (await _run("localectl", "list-keymaps")).split() if k]


# ── writing ──────────────────────────────────────────────────────────
async def set_timezone(tz: str) -> None:
    if not tz or "/" not in tz and tz != "UTC":
        raise OSConfigError(f"{tz!r} is not a time zone name")
    if tz not in await list_timezones():
        raise OSConfigError(f"{tz} is not a time zone this system knows")
    await _run("timedatectl", "set-timezone", tz)


async def set_ntp(enabled: bool) -> None:
    await _run("timedatectl", "set-ntp", "true" if enabled else "false")


async def set_time(text: str) -> None:
    """Set the wall clock. Only meaningful with NTP off.

    Refused rather than silently ignored when NTP is on: systemd rejects the
    call anyway, and "I typed the time and it snapped back" is a worse
    experience than being told why.
    """
    stamp = None
    for fmt in TIME_FORMATS:
        try:
            stamp = datetime.strptime(text.strip(), fmt)
            break
        except ValueError:
            continue
    if stamp is None:
        raise OSConfigError(f"Use {TIME_EXAMPLE} — 24-hour clock")

    show = _kv(await _run("timedatectl", "show"))
    if show.get("NTP") == "yes":
        raise OSConfigError("Turn Automatic time off before setting the clock")
    await _run("timedatectl", "set-time", stamp.strftime("%Y-%m-%d %H:%M:%S"))


# The image installs this. It is NOT plain locale-gen: Debian's locale-gen
# takes no locale argument, regenerates only what is already uncommented in
# /etc/locale.gen, and exits 0 either way — so calling it directly looks like
# it worked and generates nothing. See image/layer/files/tracer-locale-gen.
LOCALE_GEN = "/usr/local/sbin/tracer-locale-gen"


async def set_locale(name: str) -> None:
    """Select a system locale, generating it first if it does not exist.

    Generation is the whole reason a sudoers file exists. localectl accepts a
    LANG that was never generated without complaint, and the system then falls
    back to C at the next boot with nothing shown anywhere — the operator sees
    the locale they chose in Settings and a device ignoring it. So this
    verifies the locale really exists rather than trusting any exit code.
    """
    if not name or "." not in name:
        raise OSConfigError(f"{name!r} is not a locale name")

    generated = {_normalise_locale(n) for n in (await _run("locale", "-a")).split()}
    if _normalise_locale(name) not in generated:
        log.info("generating locale %s", name)
        # Checked explicitly so a missing helper does not surface as sudo's
        # "command not found", which reads as a problem with the locale.
        if not os.path.exists(LOCALE_GEN):
            raise OSConfigError(
                f"{name} is not generated on this system, and the helper that "
                f"generates locales ({LOCALE_GEN}) is not installed on this "
                f"image. Only locales built at image time can be selected.")
        try:
            await _run("sudo", "-n", LOCALE_GEN, name, timeout=LOCALE_GEN_TIMEOUT)
        except OSConfigError as exc:
            # Name the missing piece. "could not be generated" sent an
            # operator looking at the locale when the real answer was that the
            # image never installed the helper.
            raise OSConfigError(f"{name}: {exc}") from exc

        after = {_normalise_locale(n) for n in (await _run("locale", "-a")).split()}
        if _normalise_locale(name) not in after:
            raise OSConfigError(
                f"{name} was not generated — the system would fall back to C "
                f"at next boot, so it has not been selected")

    await _run("localectl", "set-locale", f"LANG={name}")


async def set_keymap(name: str) -> None:
    if name not in await list_keymaps():
        raise OSConfigError(f"{name} is not a keymap this system knows")
    await _run("localectl", "set-keyboard", name)


async def set_wifi_region(code: str) -> None:
    """Set the WiFi regulatory domain.

    Not cosmetic: the wrong region removes legal channels, so an AP on
    channel 12 or 13 simply never appears in a scan — which reads as a broken
    radio rather than a wrong setting.
    """
    code = (code or "").strip().upper()
    if not _REGION_RE.match(code):
        raise OSConfigError("Use a two-letter country code, e.g. US or GB")
    await _run("sudo", "-n", "raspi-config", "nonint", "do_wifi_country", code)
