"""logs — Tracer's own journal, plus Headwaters' host and container logs.

Reads the SUBSTRATE, never an API (docs/api.md C0). Headwaters exposes no log
endpoint, and even if it did, a tool meant to debug Headwaters must not depend
on Headwaters' own code to tell it what went wrong.

Three sources, all pulled over the SSH channel and parsed on Tracer:

  local      journalctl on this device
  host       journalctl -u <unit> on Headwaters — where the CAN-to-MQTT
             bridge lives. It is NOT a container, so `docker logs` will never
             show it; this is the only way to see its errors.
  container  docker logs <name> on Headwaters

Unit names are the INSTALLED names, which differ from the filenames in the
Headwaters repo: local_code/can-to-mqtt.service is installed as
`cantomqtt.service` (Headwaters deploy.sh:502). Verified against the live host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .. import sshcopy
from .base import Degraded, Module, Unavailable

log = logging.getLogger("tracerd.logs")

MAX_LINES = 400
REFRESH_S = 20

# Host units, installed names. can0/docker included because the bridge
# depends on both and its failures usually start there.
HOST_UNITS = [
    "cantomqtt", "discovery-mdns", "deployment-watcher",
    "map-watcher", "os-settings", "time-from-bearing",
    "can0", "docker",
]

CONTAINERS = ["mosquitto", "backend", "frontend", "mongodb", "photon", "valhalla"]

# journald numeric priority -> our level
_PRIO = {0: "ERR", 1: "ERR", 2: "ERR", 3: "ERR", 4: "WARN",
         5: "INFO", 6: "INFO", 7: "DEBUG"}

# Leading ISO-8601 timestamp, optional fractional seconds and zone.
_RE_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*")


def _level_from_text(text: str) -> str:
    """Containers have no priority field, so infer from the line."""
    t = text.lower()
    if any(k in t for k in ("traceback", "exception", "error", "fatal", "econnrefused",
                            "failed", "refused", "cannot", "critical")):
        return "ERR"
    if any(k in t for k in ("warn", "deprecat", "retry", "retrying", "timeout")):
        return "WARN"
    return "INFO"


class LogsModule(Module):
    name = "logs"
    interval = 5.0
    backoff_interval = 10.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.source = "local"      # local | host | container
        self.unit = ""
        self.level = "ALL"         # ALL | WARN | ERR
        self.query = ""
        self.lines: list[dict] = []
        self.failed_units: list[str] = []
        self.error: str | None = None
        self.fetched_at = 0.0
        self._fetch_at = 0.0
        self._task: asyncio.Task | None = None

    # ── remote helpers ───────────────────────────────────────────────
    def _creds(self):
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        key = str(Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
                  / "ssh" / "id_ed25519")
        return {
            "host": (v.get("headwaters_host") or "headwaters.local").strip(),
            "user": (v.get("headwaters_user") or "trailcurrent").strip(),
            "pw": v.get("headwaters_password") or v.get("mqtt_password") or "",
            "key": key if os.path.isfile(key) else None,
        }

    async def _ssh(self, cmd: str, timeout=40) -> str:
        c = self._creds()
        if not c["key"] and not c["pw"]:
            raise Unavailable("no Headwaters credentials")
        return await sshcopy.run(c["host"], c["user"], cmd,
                                 key=c["key"], password=None if c["key"] else c["pw"],
                                 timeout=timeout)

    # ── parsing ──────────────────────────────────────────────────────
    @staticmethod
    def _parse_journal_json(out: str, fallback_unit: str) -> list[dict]:
        """journalctl -o json gives a real PRIORITY, so the level is the
        system's own classification rather than our guess at one."""
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            msg = j.get("MESSAGE")
            if isinstance(msg, list):     # journald returns bytes as int arrays
                try:
                    msg = bytes(msg).decode("utf-8", "replace")
                except Exception:
                    msg = str(msg)
            try:
                ts = int(j.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
            except (TypeError, ValueError):
                ts = 0.0
            try:
                prio = int(j.get("PRIORITY", 6))
            except (TypeError, ValueError):
                prio = 6
            # Prefer the systemd unit over SYSLOG_IDENTIFIER. The bridge runs
            # a venv python, so the identifier is literally "python" for every
            # line — useless when several services are being compared.
            unit = j.get("_SYSTEMD_UNIT") or ""
            if unit.endswith(".service"):
                unit = unit[:-8]
            if not unit:
                ident = j.get("SYSLOG_IDENTIFIER") or ""
                unit = fallback_unit if ident in ("python", "python3", "") else ident
            rows.append({
                "ts": round(ts, 3),
                "level": _PRIO.get(prio, "INFO"),
                "unit": unit,
                "text": (msg or "").rstrip(),
            })
        return rows

    @staticmethod
    def _parse_docker(out: str, container: str) -> list[dict]:
        rows = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            ts = 0.0
            text = line
            # --timestamps prefixes RFC3339Nano
            if len(line) > 30 and line[4] == "-" and "T" in line[:30]:
                stamp, _, rest = line.partition(" ")
                text = rest
                try:
                    import datetime
                    s = stamp.replace("Z", "+00:00")
                    if "." in s:
                        head, _, tail = s.partition(".")
                        frac = tail[:6].ljust(6, "0")
                        off = tail[len(tail.rstrip("0123456789")):] if False else ""
                        s = f"{head}.{frac}+00:00"
                    ts = datetime.datetime.fromisoformat(s).timestamp()
                except Exception:
                    ts = 0.0
            # Docker prepends its own --timestamps, and the application
            # usually logs one too, so a line arrives with TWO. We already
            # render time in its own column; a second copy eats a third of a
            # 640 px row. Strip a leading ISO-8601 stamp from the remainder.
            text = _RE_ISO.sub("", text, count=1)
            rows.append({"ts": round(ts, 3), "level": _level_from_text(text),
                         "unit": container, "text": text})
        return rows

    # ── fetching ─────────────────────────────────────────────────────
    async def _fetch(self) -> None:
        """Fetch into a LOCAL list and swap it in only on success.

        Assigning straight to self.lines meant every refresh emptied the
        screen while it worked, and a failed refresh emptied it for good —
        the log flicked to "0 of 0" and back. Last-known-good data with an
        age on it is far more useful to someone debugging than a blank pane.
        """
        rows: list[dict] | None = None
        try:
            if self.source == "local":
                if not shutil.which("journalctl"):
                    raise Unavailable("journalctl not installed")
                unit = self.unit or "tracerd"
                proc = await asyncio.create_subprocess_exec(
                    "journalctl", "-o", "json", "-n", str(MAX_LINES),
                    "--no-pager", *(["-u", unit] if self.unit else []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                rows = self._parse_journal_json(out.decode(errors="replace"), unit)
            elif self.source == "host":
                unit = self.unit or "cantomqtt"
                out = await self._ssh(
                    f"journalctl -u {unit} -o json -n {MAX_LINES} --no-pager "
                    "--output-fields=__REALTIME_TIMESTAMP,PRIORITY,MESSAGE,"
                    "SYSLOG_IDENTIFIER,_SYSTEMD_UNIT")
                rows = self._parse_journal_json(out, unit)
            else:
                name = self.unit or "backend"
                out = await self._ssh(
                    "docker logs --tail %d --timestamps "
                    "$(docker ps -qf name=%s) 2>&1" % (MAX_LINES, name))
                rows = self._parse_docker(out, name)
            self.lines = rows or []
            self.fetched_at = time.time()
            self.error = None
        except (Unavailable, sshcopy.SSHError) as exc:
            # Keep whatever is already on screen; only record why the refresh
            # failed. Wiping it would throw away the evidence someone is
            # mid-way through reading.
            self.error = str(exc)
        except Exception as exc:
            self.error = str(exc)

    async def _fetch_failed_units(self) -> None:
        """Anything systemd has given up on. This belongs at the top of the
        screen: a failed unit is usually the answer, and it is the one thing
        that will not show up in the log you happen to be reading."""
        try:
            out = await self._ssh(
                "systemctl list-units --state=failed --no-legend --no-pager "
                "| awk '{print $2}'", timeout=25)
            self.failed_units = [u for u in out.split() if u.endswith(".service")]
        except Exception:
            pass

    # ── state ────────────────────────────────────────────────────────
    def _filtered(self):
        out = self.lines
        if self.level == "WARN":
            out = [l for l in out if l["level"] in ("WARN", "ERR")]
        elif self.level == "ERR":
            out = [l for l in out if l["level"] == "ERR"]
        if self.query:
            q = self.query.lower()
            out = [l for l in out if q in l["text"].lower() or q in l["unit"].lower()]
        return list(reversed(out))        # newest first

    async def poll(self):
        if self.mock:
            return _mock_state()

        now = time.time()
        if now - self._fetch_at > REFRESH_S and (
                self._task is None or self._task.done()):
            self._fetch_at = now
            self._task = asyncio.create_task(self._refresh_all())

        rows = self._filtered()
        data = {
            "source": self.source,
            "unit": self.unit or ("tracerd" if self.source == "local"
                                  else "cantomqtt" if self.source == "host" else "backend"),
            "level": self.level,
            "query": self.query,
            "lines": rows[:120],
            "total": len(self.lines),
            "shown": len(rows),
            "warnings": sum(1 for l in self.lines if l["level"] == "WARN"),
            "errors": sum(1 for l in self.lines if l["level"] == "ERR"),
            "failed_units": self.failed_units,
            "age_s": int(time.time() - self.fetched_at) if self.fetched_at else None,
            "stale": bool(self.error and self.lines),
            "hosts": {"host_units": HOST_UNITS, "containers": CONTAINERS},
            "error": self.error,
        }
        if self.error:
            raise Degraded(self.error, data)
        return data

    async def _refresh_all(self):
        label = ("reading journal" if self.source == "local"
                 else f"reading {self.unit or 'logs'} on Headwaters")
        self.set_busy(True, label)
        try:
            await self._fetch()
            if self.source != "local":
                await self._fetch_failed_units()
        finally:
            self.set_busy(False)
        await self.refresh()

    async def handle(self, op, args):
        if op == "select":
            src = args.get("source")
            if src in ("local", "host", "container"):
                self.source = src
            if "unit" in args:
                self.unit = args.get("unit") or ""
            self._fetch_at = 0.0
            self.lines = []          # different unit: old lines are not its lines
            self.fetched_at = 0.0
            await self._refresh_all()
            return {"source": self.source, "unit": self.unit}
        if op == "level":
            order = ["ALL", "WARN", "ERR"]
            self.level = args.get("level") or order[(order.index(self.level) + 1) % 3]
            await self.refresh()
            return {"level": self.level}
        if op == "search":
            self.query = args.get("query", "")
            await self.refresh()
            return {"query": self.query}
        if op == "refresh":
            self._fetch_at = 0.0
            await self._refresh_all()
            return {"refreshed": True}
        raise Unavailable(f"logs has no operation {op!r}")

    def tile_status(self):
        d = self.data or {}
        if self.state in ("ok", "degraded"):
            if d.get("failed_units"):
                return (f"{len(d['failed_units'])} failed", "#FF5453")
            if d.get("errors"):
                return (f"{d['errors']} errors", "#FF5453")
            if d.get("warnings"):
                return (f"{d['warnings']} warnings", "#FFC107")
            return ("clean", "#74FE00")
        return ("--", "#666")


def _mock_state():
    now = time.time()
    rows = [
        (0, "INFO", "cantomqtt", "published 46 frames to can/inbound"),
        (2, "WARN", "cantomqtt", "CAN send failed (1): no buffer space available"),
        (8, "ERR", "cantomqtt", "CAN bus send failed (3): transmit buffer full"),
        (9, "INFO", "cantomqtt", "reinitialising socketcan can0 @ 500000"),
        (14, "ERR", "backend", "connect ECONNREFUSED farwatch.trailcurrent.com:8883"),
        (15, "WARN", "backend", "falling back to local-only mode, buffering 240 msgs"),
        (22, "INFO", "mosquitto", "new client connected as tracer-0f21"),
    ]
    lines = [{"ts": now - a, "level": lv, "unit": u, "text": t} for a, lv, u, t in rows]
    return {"source": "host", "unit": "cantomqtt", "level": "ALL", "query": "",
            "lines": lines, "total": len(lines), "shown": len(lines),
            "warnings": 2, "errors": 2, "failed_units": [],
            "hosts": {"host_units": HOST_UNITS, "containers": CONTAINERS},
            "error": None}
