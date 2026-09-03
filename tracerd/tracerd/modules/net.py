"""net — WiFi association, scanning, and reachability. nmcli-backed.

Two responsibilities:

  1. Report the current association (the Network app's four stat tiles) and
     run the reachability checks the mock lists.
  2. Scan, connect and forget — used by the blocking WiFi setup flow. Without
     a network the device cannot reach a broker, a Headwaters box, or any
     module, so joining one is a precondition for the rest of the product
     rather than a setting buried in an app.

Everything here is a read or an action on THIS device's own radio. Nothing
touches the vehicle fleet, so it stays inside the monitoring constraint.
"""

from __future__ import annotations

import asyncio
import shutil
import time

from .base import Degraded, Module, Unavailable

RECHECK_INTERVAL = 20.0


async def _run(*cmd, timeout=15.0):
    if not shutil.which(cmd[0]):
        raise Unavailable(f"{cmd[0]} not installed")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Unavailable(f"{cmd[0]} timed out")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _unescape(field: str) -> str:
    """nmcli -t escapes ':' and '\\' inside fields."""
    return field.replace("\\:", ":").replace("\\\\", "\\")


def _split_terse(line: str, n: int) -> list[str]:
    """Split an nmcli -t line on unescaped colons."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    out.append(cur)
    while len(out) < n:
        out.append("")
    return out[:n]


def _signal_dbm() -> int | None:
    """Real dBm from /proc/net/wireless.

    `iw` is not installed on this board, and nmcli reports a 0-100 quality
    percentage, not dBm. Rather than relabel a percentage as dBm — which the
    mock displays and which a technician would read literally — take the
    driver's own level value. Returns None if unavailable, so the UI shows
    `--` instead of a made-up number.
    """
    try:
        with open("/proc/net/wireless") as fh:
            for line in fh.readlines()[2:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0].rstrip(":"):
                    return int(float(parts[3].rstrip(".")))
    except (OSError, ValueError, IndexError):
        pass
    return None


class NetModule(Module):
    name = "net"
    interval = 4.0
    backoff_interval = 4.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self._checks: list[dict] = []
        self._checks_at = 0.0
        self._check_task: asyncio.Task | None = None
        # False when NetworkManager refused the scan request (polkit). The UI
        # must say so rather than presenting a stale cache as a fresh sweep.
        self.scan_authorized = True

    # ── current association ──────────────────────────────────────────
    async def poll(self):
        if self.mock:
            self._maybe_recheck()
            return {
                "ssid": "Airstream-27", "ip": "10.42.0.87", "gateway": "10.42.0.1",
                "signal_dbm": -42, "iface": "wlan0", "up": True,
                "checks": self._checks or _MOCK_CHECKS,
            }

        rc, out, _ = await _run("nmcli", "-t", "-f",
                                "TYPE,STATE,DEVICE", "dev", "status")
        iface = None
        for line in out.splitlines():
            typ, state, dev = _split_terse(line, 3)
            if typ == "wifi" and state == "connected":
                iface = dev
                break

        # The SSID must come from the active AP, NOT from `dev status`'s
        # CONNECTION column — that is the nmcli connection-profile name, which
        # on a netplan-managed box reads "netplan-wlan0-Quigon" rather than
        # the network the technician is looking for.
        ssid = None
        if iface:
            rc, out, _ = await _run("nmcli", "-t", "-f", "ACTIVE,SSID",
                                    "dev", "wifi", "list", "ifname", iface)
            for line in out.splitlines():
                active, name = _split_terse(line, 2)
                if active.strip() == "yes":
                    ssid = _unescape(name).strip()
                    break

        if not ssid:
            # Not a crash and not an error — it is the state the blocking
            # WiFi setup flow exists to resolve.
            raise Unavailable("not connected to wifi")

        ip = gateway = None
        rc, out, _ = await _run("nmcli", "-t", "-f",
                                "IP4.ADDRESS,IP4.GATEWAY", "dev", "show", iface)
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k.startswith("IP4.ADDRESS") and "/" in v:
                ip = v.split("/")[0]
            elif k == "IP4.GATEWAY" and v:
                gateway = v

        self._maybe_recheck()
        return {
            "ssid": ssid, "ip": ip, "gateway": gateway,
            "signal_dbm": _signal_dbm(), "iface": iface, "up": True,
            "checks": self._checks,
        }

    # ── reachability ─────────────────────────────────────────────────
    def _maybe_recheck(self) -> None:
        """Reachability probes are slow; run them out of band on their own
        cadence so they never stall the 4 s association poll."""
        if self._check_task and not self._check_task.done():
            return
        if time.time() - self._checks_at < RECHECK_INTERVAL:
            return
        self._check_task = asyncio.create_task(self._recheck())

    async def _recheck(self) -> None:
        self._checks_at = time.time()
        if self.mock:
            self._checks = _MOCK_CHECKS
            return
        self.set_busy(True, "checking reachability")
        results = await asyncio.gather(
            _check_resolve("headwaters.local"),
            _check_ping("headwaters.local"),
            _check_port("headwaters.local", 8883, "MQTT TLS 8883"),
            _check_http("headwaters.local", 443, "/api/health", "Backend API 443"),
            _check_port("farwatch.trailcurrent.com", 8883, "Cloud bridge to Farwatch"),
            return_exceptions=True,
        )
        self._checks = [r for r in results if isinstance(r, dict)]
        self.set_busy(False)

    # ── operations ───────────────────────────────────────────────────
    async def handle(self, op: str, args: dict):
        if op == "scan":
            nets = await self._scan()
            return {
                "networks": nets,
                "authorized": self.scan_authorized,
                "warning": None if self.scan_authorized else
                           "NetworkManager refused the scan request; showing "
                           "cached results only",
            }
        if op == "connect":
            return await self._connect(args)
        if op == "forget":
            ssid = args.get("ssid", "")
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            await _run("nmcli", "con", "delete", ssid)
            return {"forgotten": ssid}
        if op == "recheck":
            self._checks_at = 0.0
            await self._recheck()
            return {"checks": self._checks}
        raise Unavailable(f"net has no operation {op!r}")

    async def _scan(self) -> list[dict]:
        if self.mock:
            return _MOCK_NETWORKS

        # NetworkManager gates scan REQUESTS behind polkit. As a normal user
        # this fails with "not authorized" and nmcli then serves whatever is
        # left in its cache — which is why only the already-connected SSID
        # appears. The failure is silent unless we check it, so check it.
        rc, out, err = await _run("nmcli", "dev", "wifi", "rescan", timeout=25.0)
        self.scan_authorized = rc == 0
        if rc != 0 and "not authorized" not in (err + out).lower():
            # A non-authorization failure is worth surfacing too, but a stale
            # list is still better than nothing, so carry on and report.
            self.scan_authorized = False
        if self.scan_authorized:
            # Results land asynchronously; without this the first list after a
            # rescan is still the old cache.
            await asyncio.sleep(2.5)

        rc, out, _ = await _run("nmcli", "-t", "-f",
                                "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list")
        saved = await self._saved()

        # One SSID is commonly broadcast by several APs (mesh, extenders): this
        # rig shows twelve "Quigon" rows. Collapse by SSID keeping the STRONGEST
        # signal, and OR the flags — taking the first row instead reports the
        # connected network as not-connected whenever a stronger-but-idle AP
        # sorts ahead of it.
        merged: dict[str, dict] = {}
        for line in out.splitlines():
            inuse, ssid, signal, sec = _split_terse(line, 4)
            ssid = _unescape(ssid).strip()
            if not ssid:
                continue
            sig = int(signal) if signal.isdigit() else 0
            active = inuse.strip() == "*"
            cur = merged.get(ssid)
            if cur is None:
                merged[ssid] = {
                    "ssid": ssid, "signal": sig,
                    "secure": bool(sec.strip()) and sec.strip() != "--",
                    "security": sec.strip() or "open",
                    "active": active, "saved": ssid in saved,
                    "aps": 1,
                }
            else:
                cur["signal"] = max(cur["signal"], sig)
                cur["active"] = cur["active"] or active
                cur["aps"] += 1

        nets = list(merged.values())
        nets.sort(key=lambda n: (-n["active"], -n["saved"], -n["signal"]))
        return nets

    async def _saved(self) -> set[str]:
        """SSIDs with a stored profile.

        Match on the profile's 802-11-wireless.ssid, NOT its NAME. netplan
        names profiles like "netplan-wlan0-Quigon", so comparing names marks
        every saved network as unsaved and re-prompts for a known password.
        """
        rc, out, _ = await _run("nmcli", "-t", "-f", "NAME,TYPE", "con", "show")
        names = [
            _unescape(_split_terse(l, 2)[0])
            for l in out.splitlines()
            if _split_terse(l, 2)[1].endswith("wireless")
        ]
        ssids: set[str] = set()
        for name in names:
            rc, o, _ = await _run("nmcli", "-t", "-f", "802-11-wireless.ssid",
                                  "con", "show", name, timeout=6.0)
            for line in o.splitlines():
                _, _, v = line.partition(":")
                if v.strip():
                    ssids.add(v.strip())
            # Fall back to the profile name if the SSID field was empty.
            if not o.strip():
                ssids.add(name)
        return ssids

    async def _connect(self, args: dict) -> dict:
        ssid = args.get("ssid", "").strip()
        psk = args.get("psk") or ""
        if not ssid:
            raise Unavailable("no ssid given")
        if self.mock:
            await asyncio.sleep(1.2)
            if psk and psk != "correct":
                raise Unavailable("incorrect password")
            return {"connected": True, "ssid": ssid}

        if psk:
            cmd = ("nmcli", "dev", "wifi", "connect", ssid, "password", psk)
        else:
            # Saved or open network — no credential needed.
            cmd = ("nmcli", "dev", "wifi", "connect", ssid)
        rc, out, err = await _run(*cmd, timeout=45.0)
        if rc != 0:
            msg = (err or out).strip().splitlines()
            detail = msg[-1] if msg else "connection failed"
            # nmcli's phrasing is not written for a technician in a vehicle bay.
            low = detail.lower()
            if "secrets were required" in low or "802.1x" in low or "key" in low:
                detail = "incorrect password"
            elif "not found" in low:
                detail = "network not found"
            raise Unavailable(detail)
        # Force the association poll and the probes to refresh immediately.
        self._checks_at = 0.0
        return {"connected": True, "ssid": ssid}

    def tile_status(self):
        if self.state != "ok":
            return ("--", "#666")
        return (self.data.get("ssid") or "--", "#74FE00")


# ── individual probes ────────────────────────────────────────────────
async def _check_resolve(host: str) -> dict:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=2), timeout=4.0
        )
        addr = infos[0][4][0]
        return {"name": f"{host} resolves", "detail": f"mDNS · {addr}",
                "result": "OK", "ok": True}
    except Exception:
        return {"name": f"{host} resolves", "detail": "no answer",
                "result": "Failed", "ok": False}


async def _check_ping(host: str) -> dict:
    name = f"ICMP to {host.split('.')[0].capitalize()}"
    try:
        rc, out, _ = await _run("ping", "-c", "2", "-W", "1", host, timeout=8.0)
    except Unavailable as exc:
        return {"name": name, "detail": str(exc), "result": "Failed", "ok": False}
    if rc != 0:
        return {"name": name, "detail": "no reply", "result": "Failed", "ok": False}
    avg = ""
    for line in out.splitlines():
        if "min/avg/max" in line or "rtt" in line:
            try:
                # ping reports 3 decimals; sub-millisecond precision is noise
                # on a field tool and pushes the column out of alignment.
                avg = f"{float(line.split('=')[1].strip().split('/')[1]):.0f} ms avg"
            except (IndexError, ValueError):
                pass
    return {"name": name, "detail": avg or "reply", "result": "OK", "ok": True}


async def _check_port(host: str, port: int, name: str) -> dict:
    t0 = time.monotonic()
    try:
        fut = asyncio.open_connection(host, port)
        r, w = await asyncio.wait_for(fut, timeout=4.0)
        w.close()
        ms = int((time.monotonic() - t0) * 1000)
        return {"name": name, "detail": f"connect {ms} ms", "result": "OK", "ok": True}
    except asyncio.TimeoutError:
        return {"name": name, "detail": "timed out", "result": "Offline", "ok": False}
    except OSError as exc:
        detail = "no route to host" if exc.errno in (113, 101) else "refused"
        return {"name": name, "detail": detail, "result": "Offline", "ok": False}


async def _check_http(host: str, port: int, path: str, name: str) -> dict:
    import ssl
    try:
        # The rig uses a private CA, so certificate verification would fail
        # for reasons that say nothing about reachability. This probe answers
        # "is the API answering", not "is the chain valid" — the UI labels it
        # accordingly rather than implying a verified connection.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        fut = asyncio.open_connection(host, port, ssl=ctx)
        r, w = await asyncio.wait_for(fut, timeout=5.0)
        w.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout=4.0)
        w.close()
        code = line.decode(errors="replace").split()[1] if b" " in line else "?"
        ok = code.startswith("2") or code.startswith("3")
        return {"name": name, "detail": f"HTTP {code}",
                "result": "OK" if ok else "Failed", "ok": ok}
    except asyncio.TimeoutError:
        return {"name": name, "detail": "timed out", "result": "Offline", "ok": False}
    except OSError as exc:
        detail = "no route to host" if exc.errno in (113, 101) else "refused"
        return {"name": name, "detail": detail, "result": "Offline", "ok": False}
    except Exception:
        return {"name": name, "detail": "handshake failed",
                "result": "Failed", "ok": False}


_MOCK_CHECKS = [
    {"name": "headwaters.local resolves", "detail": "mDNS · 10.42.0.14", "result": "OK", "ok": True},
    {"name": "ICMP to Headwaters", "detail": "2.1 ms avg", "result": "OK", "ok": True},
    {"name": "MQTT TLS 8883", "detail": "handshake 84 ms", "result": "OK", "ok": True},
    {"name": "Backend API 443", "detail": "HTTP 200", "result": "OK", "ok": True},
    {"name": "Cloud bridge to Farwatch", "detail": "no route to host", "result": "Offline", "ok": False},
]

_MOCK_NETWORKS = [
    {"ssid": "Airstream-27", "signal": 96, "secure": True, "security": "WPA2", "active": True, "saved": True},
    {"ssid": "Headwaters-4F2A", "signal": 74, "secure": True, "security": "WPA2", "active": False, "saved": False},
    {"ssid": "RV-Park-Guest", "signal": 61, "secure": False, "security": "open", "active": False, "saved": False},
    {"ssid": "Starlink-9931", "signal": 48, "secure": True, "security": "WPA2", "active": False, "saved": False},
    {"ssid": "TrailCurrent-Bench", "signal": 32, "secure": True, "security": "WPA3", "active": False, "saved": True},
]
