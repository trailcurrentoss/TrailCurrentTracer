"""The twelve app modules.

Scope note: this milestone delivers the launcher, so each module implements
enough to report an honest tile status. Full payloads land per-app in the
order given in the build plan.

The rule followed throughout: detect for real where the standard library can,
and report `unavailable` with a plain-English reason where it cannot. Nothing
here fabricates a value on hardware — invented data in a diagnostic tool is
worse than no data. Mock values appear only under --mock.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path
import shutil
import socket
import time

from .. import sshcopy
from .base import Degraded, Module, Unavailable
from .capture import CaptureModule
from .discovery import DiscoveryModule
from .firmware import FirmwareModule
from .gnss import GnssModule
from .simulate import SimulateModule
from .terminal import TerminalModule
from .logs import LogsModule
from .mqtt import MqttModule
from .net import NetModule
from .power import PowerModule
from .moduledebug import ModuleDebugModule
from .settings import SettingsModule
from .system import SystemModule


class _App(Module):
    """Shared helper for the app modules."""

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock


async def _run(*cmd, timeout=4.0) -> tuple[int, str]:
    """Run a command, return (rc, stdout). Never raises on non-zero."""
    if not shutil.which(cmd[0]):
        raise Unavailable(f"{cmd[0]} not installed")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Unavailable(f"{cmd[0]} timed out")
    return proc.returncode, out.decode(errors="replace")


# ── CAN ──────────────────────────────────────────────────────────────
class CanModule(_App):
    name = "can"
    interval = 2.0

    async def poll(self):
        """Strictly passive. NEVER transmits to probe — see docs/api.md."""
        if self.mock:
            return {"iface": "can0", "bitrate": 500000, "state": "error-active",
                    "err": 0, "ids": 14, "rate": 62.0}

        ifaces = [n for n in os.listdir("/sys/class/net") if n.startswith("can")]
        if not ifaces:
            raise Unavailable("no can0 interface")
        iface = ifaces[0]
        try:
            with open(f"/sys/class/net/{iface}/operstate") as fh:
                oper = fh.read().strip()
        except OSError:
            oper = "unknown"
        if oper not in ("up", "unknown"):
            raise Unavailable(f"{iface} is {oper}")
        # Frames seen so far. Zero is genuinely ambiguous — a quiet bus and a
        # disconnected one are indistinguishable without transmitting, and
        # transmitting is forbidden. Report, do not diagnose.
        rx = 0
        try:
            with open(f"/sys/class/net/{iface}/statistics/rx_packets") as fh:
                rx = int(fh.read().strip())
        except OSError:
            pass
        if rx == 0:
            raise Degraded(f"{iface} up, no frames seen",
                           {"iface": iface, "rx": 0})
        return {"iface": iface, "rx": rx}

    def tile_status(self):
        if self.state == "ok":
            return (f"{self.data.get('bitrate', 500000)//1000} kbit/s", "#74FE00")
        if self.state == "degraded":
            return ("no frames", "#FFC107")
        return ("--", "#666")


# ── Headwaters ───────────────────────────────────────────────────────
def _age(started: str, now_epoch) -> float | None:
    """Seconds since an RFC3339 StartedAt, measured on the REMOTE clock.

    Returns None rather than a wrong number when either end is missing: a
    container age that is silently computed against the wrong clock is the
    kind of number an operator would act on.
    """
    if not started or now_epoch is None:
        return None
    txt = started.strip().replace("Z", "+00:00")
    # Docker emits nanoseconds; datetime accepts at most microseconds.
    if "." in txt:
        head, _, tail = txt.partition(".")
        frac = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(frac):] if len(tail) > len(frac) else ""
        offset = offset.lstrip("0123456789")
        txt = f"{head}.{frac or '0'}{offset}"
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return None
    age = now_epoch - dt.timestamp()
    # A negative age means the container started "in the future" according to
    # the rig's own clock — i.e. the clock moved backwards after the container
    # came up. Clamping that to zero hides the fault and makes a healthy stack
    # look like it just restarted. Report None and let the skew warning speak.
    return age if age >= 0 else None


def _human_skew(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 60:.0f} minutes"


def _core_index(name: str) -> int:
    """Sort cpu0, cpu1 ... numerically; "cpu10" must not sort before "cpu2"."""
    tail = name[3:]
    return int(tail) if tail.isdigit() else -1


class HeadwatersModule(_App):
    name = "headwaters"
    # btop-like cadence. Multiplexed SSH makes each poll a few milliseconds
    # after the first, so this is not the round-trip cost it would otherwise be.
    interval = 3.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub, mock=mock)
        # Previous /proc/stat sample. CPU percentage is meaningless without it.
        self._prev_cpu: dict = {}

    def _host(self):
        return (self._settings().get("headwaters_host")
                or "headwaters.local").strip()

    def _settings(self):
        st = self.hub.modules.get("settings")
        return getattr(st, "values", {}) if st else {}

    def _credentials(self):
        """Headwaters admin credentials, falling back to the MQTT pair.

        The backend's login is the ADMIN_PASSWORD account, which is not
        necessarily the broker account — so it gets its own field. But on most
        rigs they are the same, and making someone type a second identical
        password on a 3.5" screen for no reason is its own kind of bug. Try
        the dedicated pair first, fall back to the MQTT one.
        """
        v = self._settings()
        pairs = []
        if v.get("headwaters_password"):
            pairs.append((v.get("headwaters_user") or "trailcurrent",
                          v["headwaters_password"]))
        if v.get("mqtt_password"):
            pairs.append((v.get("mqtt_username") or "trailcurrent",
                          v["mqtt_password"]))
        return pairs

    # One round trip per poll. Each section is delimited so a tool that is
    # missing on a given rig (no docker, no thermal zone) leaves an empty
    # section instead of shifting every field after it — the failure mode that
    # makes remote parsing silently wrong rather than loudly broken.
    #
    # Everything here READS. Nothing on Headwaters is modified, and nothing is
    # installed: this is deliberately /proc, df, ps and docker ps, all present
    # on a stock rig. See docs/api.md C0 — Tracer reads the substrate so it can
    # still see a Headwaters whose own API has stopped answering.
    _PROBE = (
        "echo @stat; grep '^cpu' /proc/stat; "
        "echo @mem; grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|"
        "SwapTotal|SwapFree):' /proc/meminfo; "
        "echo @load; cat /proc/loadavg; "
        "echo @up; cut -d' ' -f1 /proc/uptime; "
        # The rig's own clock. Container age is computed against THIS, not
        # against Tracer's clock, which may differ — and not from docker's
        # {{.RunningFor}}, which reports "Less than a second ago" for
        # containers that have in fact been up for days.
        "echo @now; date -u +%s; "
        "echo @temp; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; "
        "echo @disk; df -PB1 2>/dev/null | "
        r"awk 'NR>1 && $1 ~ /^\/dev\// {print $6, $2, $3}'; "
        "echo @proc; ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu 2>/dev/null | "
        "sed -n '2,17p'; "
        # RunningFor and RestartCount are what distinguish a healthy rig from
        # a crash loop. `Status` alone reads "Up Less than a second" in both
        # cases, so a container view built on it would show green while the
        # stack restarted continuously underneath.
        "echo @docker; docker ps --format '{{.Names}}|{{.Status}}|{{.RunningFor}}' "
        "2>/dev/null; "
        "echo @restart; docker inspect "
        "--format '{{.Name}}|{{.RestartCount}}|{{.State.StartedAt}}' "
        "$(docker ps -q) 2>/dev/null; "
        "echo @end"
    )

    async def poll(self):
        if self.mock:
            return {"host": "10.42.0.14", "tier": "ssh", "healthy": 6, "total": 6,
                    "cpu": 18.4, "mem": 41.2, "disk": 63, "temp": 52}

        host = self._host()
        reachable = None
        for h in (host, "headwaters.local"):
            try:
                fut = asyncio.open_connection(h, 443)
                _r, w = await asyncio.wait_for(fut, timeout=2.0)
                w.close()
                reachable = h
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if reachable is None:
            raise Unavailable("headwaters not reachable")

        # Metrics need SSH. Without credentials the app still works as a
        # reachability view rather than showing nothing at all, and says why.
        key = self._key_path() if Path(self._key_path()).exists() else None
        creds = self._credentials()
        if not key and not creds:
            return {"host": reachable, "tier": "probe", "reachable": True,
                    "metrics": None,
                    "note": "Set Headwaters Access in Settings to see system stats"}

        user = (self._settings().get("headwaters_user") or "trailcurrent")
        try:
            if key:
                out = await sshcopy.run(reachable, user, self._PROBE,
                                        key=key, timeout=20)
            else:
                u, pw = creds[0]
                out = await sshcopy.run(reachable, u, self._PROBE,
                                        password=pw, timeout=20)
        except sshcopy.SSHError as exc:
            return {"host": reachable, "tier": "probe", "reachable": True,
                    "metrics": None, "note": f"SSH failed: {exc}"}

        m = self._parse(out)
        return {"host": reachable, "tier": "ssh", "reachable": True,
                "metrics": m,
                "cpu": m.get("cpu_total"), "mem": m.get("mem_percent"),
                "disk": m.get("disk_percent"), "temp": m.get("temp_c")}

    # ── parsing ──────────────────────────────────────────────────────
    def _parse(self, out: str) -> dict:
        sec, cur = {}, None
        for line in out.splitlines():
            if line.startswith("@"):
                cur = line[1:].strip()
                sec[cur] = []
            elif cur:
                sec[cur].append(line)

        res: dict = {}

        # CPU is a RATE, so it only exists relative to the previous sample.
        # The first poll after connecting has nothing to compare against and
        # reports None rather than a fabricated 0% — a monitor that opens on
        # "0% idle-looking" is actively misleading.
        cores, total = {}, None
        for line in sec.get("stat", []):
            f = line.split()
            if len(f) < 5 or not f[0].startswith("cpu"):
                continue
            vals = [int(x) for x in f[1:] if x.isdigit()]
            if len(vals) < 4:
                continue
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            cores[f[0]] = (sum(vals), idle)
        prev = self._prev_cpu
        self._prev_cpu = cores
        if prev:
            per = {}
            for name, (t, i) in cores.items():
                if name not in prev:
                    continue
                dt, di = t - prev[name][0], i - prev[name][1]
                if dt > 0:
                    per[name] = round(100.0 * (dt - di) / dt, 1)
            total = per.pop("cpu", None)
            res["cpu_cores"] = [per[k] for k in sorted(per, key=_core_index)]
        res["cpu_total"] = total

        mem = {}
        for line in sec.get("mem", []):
            k, _, v = line.partition(":")
            digits = v.strip().split()
            if digits and digits[0].isdigit():
                mem[k.strip()] = int(digits[0]) * 1024
        mt, ma = mem.get("MemTotal"), mem.get("MemAvailable")
        if mt:
            res["mem_total"] = mt
            res["mem_used"] = (mt - ma) if ma is not None else None
            res["mem_percent"] = (round(100.0 * (mt - ma) / mt, 1)
                                  if ma is not None else None)
            res["mem_cached"] = mem.get("Cached")
        st, sf = mem.get("SwapTotal"), mem.get("SwapFree")
        if st:
            res["swap_total"], res["swap_used"] = st, st - (sf or 0)

        if sec.get("load"):
            f = sec["load"][0].split()
            if len(f) >= 3:
                res["load"] = [float(x) for x in f[:3]]
        remote_now = None
        if sec.get("now"):
            raw = sec["now"][0].strip()
            if raw.isdigit():
                remote_now = int(raw)
        res["remote_now"] = remote_now
        # Clock skew between Tracer and the rig. This is a FINDING, not a
        # detail: a Headwaters whose clock is wrong will fail TLS handshakes,
        # write log lines that cannot be correlated with anything, and expire
        # or fail to expire database records at the wrong moment. It is also
        # invisible from the PWA, which renders timestamps the backend sends
        # without ever comparing them to the viewer's clock.
        #
        # Observed in the field: a rig reporting 2026-07-27 while its own
        # containers recorded a start time of 2026-08-31 and /proc/uptime
        # agreed with the later date — the clock had jumped ~36 days back.
        if remote_now is not None:
            skew = time.time() - remote_now
            res["clock_skew"] = round(skew, 1)
            res["clock_warning"] = (
                f"Headwaters clock is {_human_skew(abs(skew))} "
                f"{'behind' if skew > 0 else 'ahead of'} Tracer"
                if abs(skew) > 120 else None
            )
        else:
            res["clock_skew"] = None
            res["clock_warning"] = None

        if sec.get("up"):
            try:
                res["uptime"] = float(sec["up"][0].strip())
            except ValueError:
                pass
        if sec.get("temp"):
            raw = sec["temp"][0].strip()
            if raw.isdigit():
                res["temp_c"] = round(int(raw) / 1000.0, 1)

        disks, biggest = [], 0.0
        for line in sec.get("disk", []):
            f = line.split()
            if len(f) != 3 or not f[1].isdigit() or not f[2].isdigit():
                continue
            tot, used = int(f[1]), int(f[2])
            if tot <= 0:
                continue
            pct = round(100.0 * used / tot, 1)
            disks.append({"mount": f[0], "total": tot, "used": used, "percent": pct})
            if f[0] == "/":
                biggest = pct
        res["disks"] = disks
        res["disk_percent"] = biggest or (disks[0]["percent"] if disks else None)

        procs = []
        for line in sec.get("proc", []):
            f = line.split(None, 4)
            if len(f) < 5:
                continue
            try:
                procs.append({"pid": int(f[0]), "cpu": float(f[1]),
                              "mem": float(f[2]), "rss": int(f[3]) * 1024,
                              "name": f[4].strip()})
            except ValueError:
                continue
        res["procs"] = procs

        restarts = {}
        for line in sec.get("restart", []):
            f = line.split("|")
            if len(f) >= 2:
                nm = f[0].strip().lstrip("/")
                try:
                    restarts[nm] = {"count": int(f[1].strip()),
                                    "started": f[2].strip() if len(f) > 2 else ""}
                except ValueError:
                    pass

        containers = []
        for line in sec.get("docker", []):
            f = line.split("|")
            name = f[0].strip()
            if not name:
                continue
            status = f[1].strip() if len(f) > 1 else ""
            r = restarts.get(name, {})
            containers.append({
                "name": name,
                "status": status,
                "running_for": f[2].strip() if len(f) > 2 else "",
                "restarts": r.get("count"),
                "started": r.get("started", ""),
                "up_seconds": _age(r.get("started", ""), remote_now),
                "up": status.lower().startswith("up"),
            })
        res["containers"] = containers
        res["healthy"] = sum(1 for c in containers if c["up"])
        res["total"] = len(containers)
        return res

    # ── CA fetch, over SSH ───────────────────────────────────────────
    async def handle(self, op, args):
        if op == "refresh":
            # Force a poll now instead of waiting out the interval.
            await self.refresh()
            return {"ok": True}
        if op == "fetch_ca":
            return await self._fetch_ca()
        if op == "enrol_key":
            return await self._enrol_key()
        if op == "clear_ca":
            st = self.hub.modules.get("settings")
            if st:
                await st.handle("set", {"key": "ca_cert", "value": ""})
            return {"cleared": True}
        raise Unavailable(f"headwaters has no operation {op!r}")

    # The CA lives beside the other generated keys in the Headwaters deploy
    # directory (see Headwaters deploy.sh, which copies data/keys/ca.pem).
    CA_REMOTE_PATHS = (
        "~/data/keys/ca.pem",
        "~/local_code/ca.pem",
        "/home/trailcurrent/data/keys/ca.pem",
    )

    def _key_path(self):
        return str(Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
                   / "ssh" / "id_ed25519")

    async def _enrol_key(self):
        """Install our public key on Headwaters so future copies need no
        password. The password is used exactly once, here."""
        v = self._settings()
        host = self._host()
        user = (v.get("headwaters_user") or "trailcurrent").strip()
        pw = v.get("headwaters_password") or v.get("mqtt_password") or ""
        if not pw:
            raise Unavailable("set the Headwaters password first")
        key = self._key_path()
        try:
            pub = await sshcopy.ensure_keypair(key)
            await sshcopy.install_key(host, user, pub, pw)
        except sshcopy.SSHError as exc:
            raise Unavailable(str(exc))
        return {"enrolled": True, "host": host, "user": user}

    async def _fetch_ca(self):
        """Copy the CA off Headwaters and install it as the MQTT trust root.

        Uses the credentials already stored for Headwaters access, so nobody
        hand-copies a PEM onto a device with a 3.5" screen and no file manager.
        Prefers the enrolled key and falls back to the password.
        """
        import hashlib
        import ssl

        v = self._settings()
        host = self._host()
        user = (v.get("headwaters_user") or "trailcurrent").strip()
        pw = v.get("headwaters_password") or v.get("mqtt_password") or ""
        key = self._key_path()
        have_key = os.path.isfile(key)
        if not have_key and not pw:
            raise Unavailable("set the Headwaters password first")

        self.set_busy(True, "copying the CA from Headwaters")
        raw, last = None, None
        for use_key in ([True, False] if have_key else [False]):
            if not use_key and not pw:
                continue
            for remote in self.CA_REMOTE_PATHS:
                try:
                    raw = await sshcopy.fetch_file(
                        host, user, remote,
                        key=key if use_key else None,
                        password=None if use_key else pw)
                    break
                except sshcopy.SSHError as exc:
                    last = str(exc)
                    # A missing path is worth trying the next candidate for;
                    # an auth failure is not — fail fast rather than locking
                    # the account out with repeated bad passwords.
                    if "not found" not in last:
                        break
            if raw is not None:
                break

        self.set_busy(False)
        if raw is None:
            raise Unavailable(last or "could not copy the CA")

        pem = raw.decode("utf-8", errors="replace")
        if "BEGIN CERTIFICATE" not in pem:
            raise Unavailable("that file is not a PEM certificate")
        # Parse before writing, so a bad file never lands on disk and quietly
        # breaks every future broker connection.
        try:
            der = ssl.PEM_cert_to_DER_cert(pem)
        except Exception:
            raise Unavailable("certificate could not be parsed")
        fingerprint = ":".join(
            hashlib.sha256(der).hexdigest()[i:i + 2].upper() for i in range(0, 16, 2))

        state_dir = Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
        dest = state_dir / "ca.pem"

        def _write():
            state_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".pem.tmp")
            tmp.write_text(pem)
            os.chmod(tmp, 0o644)      # a CA is public; only the key is secret
            os.replace(tmp, dest)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise Unavailable(f"could not save: {exc}")

        st = self.hub.modules.get("settings")
        if st:
            await st.handle("set", {"key": "ca_cert", "value": str(dest)})
        # Drop the unverified session so it reconnects with verification.
        mq = self.hub.modules.get("mqtt")
        if mq:
            try:
                await mq.handle("reconnect", {})
            except Exception:
                pass
        return {"saved": str(dest), "fingerprint": fingerprint,
                "via": "key" if have_key else "password", "host": host}

    def tile_status(self):
        if self.state != "ok":
            return ("--", "#666")
        # Read defensively. These counts live under `metrics` and are absent
        # whenever SSH is unavailable or docker is not installed on the rig.
        # Indexing them directly raised KeyError inside the render path, which
        # does not fail one tile — it kills the WebSocket handler and takes the
        # whole GUI's data stream with it.
        m = (self.data or {}).get("metrics") or {}
        total = m.get("total")
        if self.data.get("tier") == "ssh" and total:
            return (f"{m.get('healthy', 0)}/{total} up", "#74FE00")
        if self.data.get("tier") == "ssh":
            return ("no containers", "#FFC107")
        return ("reachable", "#FFC107")


# ── GNSS ─────────────────────────────────────────────────────────────
ALL = [
    MqttModule, DiscoveryModule, CaptureModule, FirmwareModule, NetModule,
    TerminalModule, LogsModule, CanModule, HeadwatersModule, GnssModule,
    SimulateModule, SettingsModule, SystemModule, ModuleDebugModule,
    PowerModule,
]
