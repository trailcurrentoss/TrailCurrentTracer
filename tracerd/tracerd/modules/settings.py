"""settings — the single JSON store, schema-validated, atomically written.

Grouped and searchable, mirroring the Headwaters PWA's settings pattern
(`containers/frontend/public/js/pages/settings/groups/*.js`): each group
carries `meta` plus a `searchIndex` of {label, kw, anchor}, and the UI
flattens every group's index into one search. Matching that shape means a
technician who knows the PWA already knows this screen.

Atomic write matters here specifically: /var/lib/tracer is the only writable
partition on a read-only rootfs, and acceptance criterion 4 is ten hard power
cuts mid-use. Write-temp-fsync-rename leaves either the old file or the new
one, never a truncated one.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

from . import osconfig
from .base import Module, Unavailable

STORE = Path(os.environ.get("TRACER_STATE", "/var/lib/tracer")) / "settings.json"

DEFAULTS = {
    "theme": "dark",              # dark | light
    "broker": "headwaters.local:8883",
    "mqtt_username": "",
    "mqtt_password": "",
    "client_id": "tracer-0f21",
    "ca_cert": "ca.pem",
    "brightness": 70,
    "capture_dir": "/media/usb0",
    "headwaters_host": "headwaters.local",
    "headwaters_user": "trailcurrent",
    "headwaters_password": "",
}

# Never leaves the daemon in plaintext. The GUI is told only whether a value is
# SET, never what it is — a broker password rendered on a 3.5" screen in a
# vehicle bay is a credential leak, and the GUI has no need for the value: it
# only ever asks the daemon to connect.
SECRETS = {"mqtt_password", "headwaters_password"}

# Mirrors the PWA's group/searchIndex contract. `anchor` is the row id the UI
# scrolls to when a search result is chosen.
GROUPS = [
    {
        "meta": {"id": "general", "title": "General", "icon": "settings",
                 "sub": "Appearance, brightness"},
        "searchIndex": [
            {"label": "Theme", "kw": "theme appearance dark light display mode",
             "anchor": "theme", "key": "theme", "type": "choice",
             "choices": ["dark", "light"],
             "help": "Light theme is easier to read outdoors in daylight."},
            {"label": "Brightness", "kw": "brightness backlight screen dim glare night",
             "anchor": "brightness", "key": "brightness", "type": "slider",
             "help": "This panel has no backlight control, so this dims what is drawn. It reduces glare but not power draw."},
        ],
    },
    # Owned by the OS, not by this store. Every row here carries
    # `source: "os"`, which tells the UI to read its value from `data.system`
    # and to route edits through the `set_system` op. See osconfig.py for why
    # none of it is persisted here.
    #
    # This group is the ONLY way to set these once an image is flashed: the
    # device boots straight into the kiosk, with no desktop, terminal or login
    # prompt behind it.
    {
        "meta": {"id": "system", "title": "Date & Time", "icon": "time",
                 "sub": "Clock, time zone, locale, region"},
        "searchIndex": [
            {"label": "Time zone", "kw": "timezone time zone tz region clock utc local area city",
             "anchor": "timezone", "key": "timezone", "type": "picker",
             "source": "os", "options": "timezones",
             "help": "Sets the clock's local time. Type to filter — there are several hundred."},
            {"label": "Automatic time", "kw": "ntp automatic time sync clock internet timesyncd network",
             "anchor": "ntp", "key": "ntp", "type": "choice",
             "source": "os", "choices": ["on", "off"],
             "help": "Syncs the clock over the network. Needs WiFi. Turn it off to set the time by hand."},
            {"label": "Date and time", "kw": "date time clock set manual hour minute day",
             "anchor": "time", "key": "time", "type": "text",
             "source": "os", "example": osconfig.TIME_EXAMPLE,
             "help": "Only settable with Automatic time off. A wrong clock breaks the broker connection — the certificate reads as not yet valid."},
            {"label": "Locale", "kw": "locale language lang region format utf8 english",
             "anchor": "locale", "key": "locale", "type": "picker",
             "source": "os", "options": "locales",
             "help": "Language and number/date formats. Generating a new one takes a few seconds."},
            {"label": "Keyboard layout", "kw": "keyboard layout keymap console vc language",
             "anchor": "keymap", "key": "keymap", "type": "picker",
             "source": "os", "options": "keymaps",
             "help": "Layout for the attached keyboard. Change this if punctuation keys type the wrong character."},
            {"label": "WiFi region", "kw": "wifi region country regulatory domain channel radio legal",
             "anchor": "wifi_region", "key": "wifi_region", "type": "text",
             "source": "os", "example": "US",
             "help": "Two-letter country code. The wrong region hides legal channels, so some networks never appear in a scan."},
        ],
    },
    {
        "meta": {"id": "network", "title": "Network", "icon": "wifi",
                 "sub": "WiFi connection"},
        "searchIndex": [
            {"label": "WiFi network", "kw": "wifi network ssid connect password wireless join",
             "anchor": "wifi", "key": "wifi", "type": "action",
             "help": "Scan for networks and join one. Needed before anything else works."},
        ],
    },
    {
        "meta": {"id": "broker", "title": "MQTT", "icon": "server",
                 "sub": "Broker, credentials, certificate"},
        "searchIndex": [
            {"label": "MQTT broker", "kw": "mqtt broker host port mosquitto 8883",
             "anchor": "broker", "key": "broker", "type": "text",
             "help": "Host and port of the rig broker.",
             "example": "headwaters.local:8883"},
            {"label": "Username", "kw": "mqtt username user login credential broker",
             "anchor": "mqtt_username", "key": "mqtt_username", "type": "text",
             "help": "The broker login. Usually the same account you use for Headwaters.",
             "example": "trailcurrent"},
            {"label": "Password", "kw": "mqtt password secret credential broker auth",
             "anchor": "mqtt_password", "key": "mqtt_password", "type": "secret",
             "help": "Password for the broker login above. Stored on this device only and never shown again."},
            {"label": "Client ID", "kw": "client id mqtt identity",
             "anchor": "client_id", "key": "client_id", "type": "text",
             "help": "How this Tracer identifies itself to the broker. Must be unique on the rig.",
             "example": "tracer-0f21"},
            {"label": "CA certificate", "kw": "ca certificate tls ssl pem trust",
             "anchor": "ca_cert", "key": "ca_cert", "type": "text",
             "help": "Path to the trust root for the broker. Use Headwaters Access > Fetch CA certificate rather than typing this."},
        ],
    },
    {
        "meta": {"id": "headwaters", "title": "Headwaters Access", "icon": "server",
                 "sub": "SSH host, key enrolment"},
        "searchIndex": [
            {"label": "Headwaters host", "kw": "headwaters host ssh address hostname",
             "anchor": "headwaters_host", "key": "headwaters_host", "type": "text",
             "help": "Hostname or IP of the Headwaters box on this rig.",
             "example": "headwaters.local"},
            {"label": "Headwaters user", "kw": "headwaters user ssh login account",
             "anchor": "headwaters_user", "key": "headwaters_user", "type": "text",
             "help": "SSH login on the Headwaters box.",
             "example": "trailcurrent"},
            # Pulls the CA off Headwaters and installs it as the MQTT trust
            # root, so nobody hand-copies a PEM onto a 3.5" screen.
            {"label": "Headwaters password", "kw": "headwaters password admin login "
             "credential secret api",
             "anchor": "headwaters_password", "key": "headwaters_password",
             "type": "secret", "optional": True,
             "help": "Only needed if the Headwaters SSH password differs from the MQTT password. Leave blank to reuse the MQTT one."},
            {"label": "Fetch CA certificate", "kw": "ca certificate tls ssl trust "
             "fetch download pem mqtt verify headwaters",
             "anchor": "fetch_ca", "key": "fetch_ca", "type": "action",
             "help": "Copies the CA off Headwaters over SSH and installs it, so the broker connection can be verified. Nothing to type."},
        ],
    },
    {
        "meta": {"id": "capture", "title": "Capture", "icon": "recording",
                 "sub": "Where sessions are written"},
        "searchIndex": [
            {"label": "Capture folder", "kw": "capture folder usb path directory save",
             "anchor": "capture_dir", "key": "capture_dir", "type": "text",
             "help": "Where capture sessions are written.",
             "example": "/media/usb0"},
        ],
    },
    {
        "meta": {"id": "about", "title": "About", "icon": "checkbox",
                 "sub": "Version, hostname"},
        "searchIndex": [
            {"label": "Version", "kw": "about version tracer build",
             "anchor": "version", "key": "version", "type": "readonly"},
            {"label": "Hostname", "kw": "hostname mdns name network identity",
             "anchor": "hostname", "key": "hostname", "type": "readonly"},
        ],
    },
]


def _load() -> dict:
    values = dict(DEFAULTS)
    try:
        with open(STORE) as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            # Only accept known keys — a corrupt or hand-edited file must not
            # inject arbitrary state into the daemon.
            for k, v in stored.items():
                if k in DEFAULTS:
                    values[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return values


def redact(values: dict) -> dict:
    """Wire-safe copy: secrets become a boolean 'is it set'."""
    out = dict(values)
    for k in SECRETS:
        out[k] = bool(out.get(k))
    return out


def _save(values: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: temp file in the same directory, fsync, then rename. A power cut
    # mid-write leaves either the old file or the new one.
    fd, tmp = tempfile.mkstemp(dir=str(STORE.parent), prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(values, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        # 0600 before the rename — the file holds the broker password, and it
        # must never briefly exist world-readable.
        os.chmod(tmp, 0o600)
        os.replace(tmp, STORE)
        # fsync the directory too, or the rename itself can be lost.
        dfd = os.open(str(STORE.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _validate(key: str, value):
    if key == "theme":
        if value not in ("dark", "light"):
            raise Unavailable("theme must be dark or light")
        return value
    if key == "brightness":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise Unavailable("brightness must be a number")
        return max(5, min(100, n))
    if key in DEFAULTS:
        return str(value)
    raise Unavailable(f"unknown setting {key!r}")


def _clear_secret_ok(key: str, value) -> bool:
    """Allow clearing a secret with an empty string, but never overwrite a
    stored secret with an accidental empty from a re-render."""
    return key not in SECRETS or value != ""


class SettingsModule(Module):
    name = "settings"
    interval = 30.0

    # Persistence is debounced: holding L/R repeats at 8 Hz, and each save is
    # two fsyncs. On SD that is a stall storm. The in-memory value is applied
    # and broadcast instantly; the disk catches up once input settles.
    SAVE_DEBOUNCE = 0.6

    def __init__(self, hub, mock: bool = False, version: str = "0.4.1"):
        super().__init__(hub)
        self.mock = mock
        self.version = version
        self.values = _load()
        self._save_task: asyncio.Task | None = None
        self.save_error: str | None = None

    def _schedule_save(self) -> None:
        if self.mock:
            return
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = asyncio.create_task(self._save_soon())

    async def _save_soon(self) -> None:
        try:
            await asyncio.sleep(self.SAVE_DEBOUNCE)
            # to_thread: _save() fsyncs the file AND its directory. Run inline
            # on the event loop and every other module, plus the WebSocket,
            # stalls for the duration of the write.
            await asyncio.to_thread(_save, dict(self.values))
            if self.save_error:
                self.save_error = None
                await self.refresh()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            self.save_error = str(exc)
            log_msg = f"could not persist settings: {exc}"
            self.reason = log_msg
            await self.refresh()

    # Mock runs on a workstation. Reading the clock is harmless, but this
    # daemon must never reach for the developer's timezone or locale, and
    # set_system is refused outright below.
    MOCK_SYSTEM = {
        "timezone": "UTC", "ntp": True, "ntp_synced": True, "local_rtc": False,
        "time": "2026-09-01 12:00:00", "locale": "en_US.UTF-8",
        "keymap": "us", "wifi_region": "US", "errors": {}, "mock": True,
    }

    async def poll(self):
        return {
            "values": redact(self.values),
            "groups": GROUPS,
            # Read fresh every poll, never stored. The OS owns these, and a
            # cached copy would drift the moment an NTP sync moved the clock.
            "system": (dict(self.MOCK_SYSTEM) if self.mock
                       else await osconfig.read_state()),
            "readonly": {
                "version": self.version,
                "hostname": socket.gethostname(),
                # Whether a CA is actually installed and readable — not merely
                # configured. A path pointing at a missing file must not read
                # as "verified".
                "ca_installed": bool(self.values.get("ca_cert")
                                     and os.path.isfile(self.values["ca_cert"])),
            },
            "writable": not self.mock,
            # Surfaced so the UI can say a change did not stick, rather than
            # showing a value that will vanish on reboot.
            "save_error": self.save_error,
        }

    async def handle(self, op: str, args: dict):
        if op == "get":
            return {"values": redact(self.values), "groups": GROUPS}
        if op == "set":
            key = args.get("key")
            value = _validate(key, args.get("value"))
            if key in SECRETS and value == "" and not args.get("clear"):
                raise Unavailable("empty password ignored; pass clear:true to remove")
            self.values[key] = value
            self._schedule_save()
            # refresh(), not publish() — see Module.refresh. publish() alone
            # would send the last poll's data and the change would not appear
            # for up to `interval` seconds.
            await self.refresh()
            return {"key": key, "value": value}
        if op == "options":
            # The picker lists are big (several hundred time zones) and never
            # change, so they are fetched on demand rather than shipped in
            # every settings snapshot.
            return {"options": await self._options(args.get("key", ""))}
        if op == "set_system":
            if self.mock:
                raise Unavailable("mock mode does not change the host system")
            key, value = args.get("key"), args.get("value")
            try:
                await self._set_system(key, value)
            except osconfig.OSConfigError as exc:
                # Surfaced as the operation's error, not swallowed. The whole
                # failure mode this guards against is a setting that silently
                # does nothing — see osconfig's module docstring.
                raise Unavailable(str(exc))
            await self.refresh()
            return {"key": key, "value": value}
        if op == "reset":
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            self.values = dict(DEFAULTS)
            self._schedule_save()
            await self.refresh()
            return {"reset": True}
        raise Unavailable(f"settings has no operation {op!r}")

    # ── OS-owned settings ────────────────────────────────────────────
    async def _options(self, key: str) -> list[str]:
        if self.mock:
            return {"timezones": ["UTC", "America/Denver", "Europe/London"],
                    "locales": ["en_US.UTF-8", "en_GB.UTF-8"],
                    "keymaps": ["us", "gb"]}.get(key, [])
        try:
            if key == "timezones":
                return await osconfig.list_timezones()
            if key == "locales":
                return await osconfig.list_locales()
            if key == "keymaps":
                return await osconfig.list_keymaps()
        except osconfig.OSConfigError as exc:
            raise Unavailable(str(exc))
        raise Unavailable(f"no option list named {key!r}")

    async def _set_system(self, key: str, value) -> None:
        if key == "timezone":
            await osconfig.set_timezone(str(value))
        elif key == "ntp":
            # The UI cycles a choice row through "on"/"off"; accept a real
            # boolean too so an RPC caller does not have to know that.
            await osconfig.set_ntp(value is True or str(value).lower() == "on")
        elif key == "time":
            await osconfig.set_time(str(value))
        elif key == "locale":
            await osconfig.set_locale(str(value))
        elif key == "keymap":
            await osconfig.set_keymap(str(value))
        elif key == "wifi_region":
            await osconfig.set_wifi_region(str(value))
        else:
            raise Unavailable(f"unknown system setting {key!r}")

    def tile_status(self):
        return (f"Tracer {self.version}", "#666")
