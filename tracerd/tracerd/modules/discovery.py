"""discovery — a window onto Headwaters' device discovery.

Tracer does NOT browse for devices itself, and does not keep its own registry.
Headwaters is the system of record for what is on a rig; a device discovered
and onboarded by Tracer alone would be registered nowhere that matters, and
the two would disagree the moment either restarted. A split brain inside a
diagnostic tool is the worst place to have one.

So every operation here is a message to Headwaters over topics that already
exist (Headwaters `local_code/discovery-mdns.py`), and Headwaters does the
work:

    discovery/browse/start     -> ask Headwaters to sweep (35 s window)
    discovery/browse/found     <- what Headwaters found
    discovery/confirm/request  -> {"hostname"}  MCU onboarding
    discovery/confirm/response <- {"hostname", "success"}
    discovery/claim/request    -> {"hostname", "creds"}  Playbill onboarding
    discovery/claim/response   <- {"hostname", "success"}
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

from .. import sshcopy
from .base import Degraded, Module, Unavailable

BROWSE_WINDOW_S = 35        # matches Headwaters' own BROWSE_TIMEOUT_S
REGISTRY_INTERVAL_S = 30    # how often to re-read Headwaters' saved modules

# Mirrors Headwaters containers/backend/src/routes/modules.js MCU_MODULES.
# Duplicated deliberately: Tracer exists to check Headwaters, so it must be
# able to compute what the record SHOULD look like independently. If the two
# ever disagree, that disagreement is the finding.
MODULE_DISPLAY_NAMES = {
    "fireside": "Fireside", "spotter": "Spotter", "milepost": "Milepost",
    "solstice": "Solstice", "ampline": "Ampline", "torrent": "Torrent",
    "tapper": "Tapper", "reservoir": "Reservoir", "borealis": "Borealis",
    "aftline": "Aftline", "plateau": "Plateau", "picket": "Picket",
    "bearing": "Bearing", "therma": "Therma", "switchback": "Switchback",
    "playbill": "Playbill",
}
WIRELESS_MODULE_IDS = {"fireside", "playbill"}


def _generate_module_name(mtype, addr, existing):
    """Port of generateModuleName (routes/discovery.js). Kept literal."""
    display = MODULE_DISPLAY_NAMES.get(mtype, mtype)
    same = [m for m in existing if m.get("type") == mtype]
    addr_num = addr if addr is not None else 0
    if same or addr_num > 1:
        return f"{display} {str(addr_num).zfill(2)}"
    return display


def _build_module_record(found, existing):
    """Port of the record the confirm route writes (routes/discovery.js:309-338)."""
    mtype = found.get("type")
    rec = {
        "type": mtype,
        "name": _generate_module_name(mtype, found.get("addr"), existing),
        "hostname": found.get("hostname"),
        "addr": found.get("addr"),
        "canid": found.get("canid"),
        "fw": found.get("fw"),
        "enabled": True,
        "config": {},
    }
    if found.get("target"):
        rec["target"] = found["target"]
    if mtype in WIRELESS_MODULE_IDS:
        rec["wireless"] = True
    return rec


def _apply_rename_rule(modules, mtype):
    """When a SECOND module of a type appears, the first gains its addr suffix
    (routes/discovery.js:325-338). Without this Tracer's write and the API's
    write would differ on the second module of any type."""
    same = [m for m in modules if m.get("type") == mtype]
    if len(same) == 2:
        first = same[0]
        display = MODULE_DISPLAY_NAMES.get(first.get("type"), first.get("type"))
        addr = first.get("addr") if first.get("addr") is not None else 0
        if first.get("name") == display:
            first["name"] = f"{display} {str(addr).zfill(2)}"
    return modules
DEVICE_TTL_S = 900          # forget a device not seen for 15 minutes


def _can_discovery_frame() -> dict:
    """CAN 0x02 discovery broadcast, byte-identical to what Headwaters sends.

    Shape from Headwaters containers/backend/src/mqtt.js:1082-1112
    (publishCanMessage): data is eight 8-bit arrays, MSB first, zero-padded,
    and `identifier` is `0x` + toString(16) — so 0x02 renders as "0x2", not
    "0x002". can-to-mqtt.py parses it with int(identifier, 16), so the exact
    spelling does not matter to the bus, but matching Headwaters keeps the
    two indistinguishable on the wire.
    """
    return {
        "identifier": "0x2",
        "data_length_code": 0,
        "data": [[0] * 8 for _ in range(8)],
        "extd": 0, "rtr": 0, "ss": 0, "self": 0,
    }


class DiscoveryModule(Module):
    name = "discovery"
    interval = 1.0
    backoff_interval = 5.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.devices: dict[str, dict] = {}
        self.browsing_until = 0.0
        self.last_result: dict | None = None
        self.last_browse_ended = 0.0
        self._hooked = False
        # What Headwaters has actually SAVED. Discovery finding a device and
        # Headwaters registering it are different things, and the difference
        # is the whole point of the screen — a module that answered a browse
        # but was never accepted is not part of the rig.
        self.registered: list[dict] = []
        self.registry_error: str | None = None
        self._registry_at = 0.0

    async def setup(self):
        self._hook()

    def _hook(self):
        """Ride the mqtt module's connection instead of opening a second one."""
        if self._hooked or self.mock:
            return
        mq = self.hub.modules.get("mqtt")
        if mq and hasattr(mq, "add_subscriber"):
            mq.add_subscriber("discovery/", self._on_message)
            self._hooked = True

    # ── inbound ──────────────────────────────────────────────────────
    def _on_message(self, topic: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        now = time.time()

        if topic == "discovery/browse/found":
            host = data.get("hostname")
            if not host:
                return
            existing = self.devices.get(host, {})
            # Preserve onboarding state across re-discovery; a fresh browse
            # must not make an already-onboarded module look pending again.
            self.devices[host] = {
                **data,
                "first_seen": existing.get("first_seen", now),
                "last_seen": now,
                "state": existing.get("state", "found"),
                "detail": existing.get("detail", ""),
            }
        elif topic in ("discovery/confirm/response", "discovery/claim/response"):
            host = data.get("hostname")
            dev = self.devices.get(host)
            if dev is not None:
                ok = bool(data.get("success"))
                dev["state"] = "onboarded" if ok else "failed"
                dev["detail"] = "" if ok else (data.get("error") or "did not respond")
            self.last_result = {"hostname": host, "success": bool(data.get("success")),
                                "ts": now}

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        if self.mock:
            return _mock_state()
        self._hook()

        mq = self.hub.modules.get("mqtt")
        connected = bool(mq and getattr(mq, "_connected", False))

        now = time.time()
        for host, d in list(self.devices.items()):
            if now - d["last_seen"] > DEVICE_TTL_S and d["state"] != "onboarded":
                del self.devices[host]

        if now - self._registry_at > REGISTRY_INTERVAL_S:
            self._registry_at = now
            asyncio.create_task(self._read_registry())

        remaining = max(0, int(self.browsing_until - now))
        if self.browsing_until and remaining == 0:
            # Record that a sweep completed, so the empty state can say
            # "Headwaters scanned and found nothing" rather than the far less
            # useful "nothing here yet". Those are different faults: one means
            # no modules are powered, the other means nobody has looked.
            self.last_browse_ended = self.browsing_until
            self.browsing_until = 0.0

        # A device Headwaters has saved is onboarded, whatever the live browse
        # thinks. The registry is the source of truth; the browse is a probe.
        saved = {r.get("hostname") for r in self.registered if r.get("hostname")}
        merged = dict(self.devices)
        for r in self.registered:
            h = r.get("hostname")
            if not h:
                continue
            existing = merged.get(h, {})
            merged[h] = {**existing, **{k: v for k, v in r.items() if v not in (None, "")},
                         "hostname": h, "state": "onboarded",
                         "registered": True,
                         "first_seen": existing.get("first_seen", now),
                         "last_seen": existing.get("last_seen", now),
                         "detail": existing.get("detail", "")}
        for h, d in merged.items():
            d.setdefault("registered", h in saved)

        data = {
            "devices": sorted(merged.values(),
                              key=lambda d: (not d.get("registered"),
                                             d.get("type", ""), d.get("hostname", ""))),
            "registered_count": len(self.registered),
            "registry_error": self.registry_error,
            "browsing": remaining > 0,
            "browse_remaining": remaining,
            "source": "headwaters",
            "last_result": self.last_result,
            "scanned": bool(self.last_browse_ended),
            "scan_age_s": int(now - self.last_browse_ended) if self.last_browse_ended else None,
        }
        if not connected:
            # Discovery is entirely mediated by Headwaters, so no broker means
            # no discovery — say that rather than showing a stale list as live.
            raise Degraded("waiting for the broker", data)
        return data

    # ── operations ───────────────────────────────────────────────────
    async def handle(self, op, args):
        mq = self.hub.modules.get("mqtt")
        if op == "browse":
            if self.mock:
                self.browsing_until = time.time() + BROWSE_WINDOW_S
                await self.refresh()
                return {"browsing": True}
            if not mq:
                raise Unavailable("mqtt module not loaded")
            # A discovery sweep is THREE broadcasts, not one. Publishing only
            # the mDNS browse finds nothing, because modules that have never
            # been onboarded are not advertising yet — the CAN frame is what
            # puts them into discovery mode.
            #
            # Mirrors Headwaters routes/discovery.js:180-182:
            #   publishDiscoveryTrigger()          -> CAN 0x02, DLC 0
            #   publishWirelessDiscoveryTrigger()  -> local/discovery/trigger
            #   publishDiscoveryBrowseStart()      -> discovery/browse/start
            await mq.publish_json("can/outbound", _can_discovery_frame())
            await mq.publish_raw("local/discovery/trigger", b"*")
            await mq.publish_json("discovery/browse/start", {})
            self.browsing_until = time.time() + BROWSE_WINDOW_S
            await self.refresh()
            return {"browsing": True, "window_s": BROWSE_WINDOW_S,
                    "sent": ["can 0x02", "local/discovery/trigger",
                             "discovery/browse/start"]}

        if op == "stop":
            if mq and not self.mock:
                await mq.publish_json("discovery/browse/stop", {})
            self.browsing_until = 0.0
            await self.refresh()
            return {"browsing": False}

        if op == "onboard":
            host = args.get("hostname")
            dev = self.devices.get(host)
            if not dev:
                raise Unavailable(f"unknown device {host!r}")
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            if self.mock:
                self.devices[host]["state"] = "onboarded"
                await self.refresh()
                return {"onboarded": host, "via": "mock"}
            if not mq:
                raise Unavailable("mqtt module not loaded")

            # Deliberately NOT POST /api/discovery/confirm.
            #
            # Tracer exists to find bugs IN Headwaters. Going through the
            # backend API would only ever show what the PWA already shows —
            # if the API is the broken layer, an API-based tool cannot see it.
            # So both halves of onboarding are done against the substrate:
            #   1. the confirm marker over MQTT (same topic the host proxy
            #      listens on), and
            #   2. the mcu_modules write straight into Mongo over SSH.
            #
            # This is a debugging tool writing ground truth on purpose. It
            # replicates the backend's record-building exactly (see
            # _build_module_record); where the two ever diverge, that
            # divergence is the finding.
            is_claim = dev.get("onboard") == "claim"
            topic = ("discovery/claim/request" if is_claim
                     else "discovery/confirm/request")
            payload = ({"hostname": host, "creds": self._broker_creds()}
                       if is_claim else {"hostname": host})
            await mq.publish_json(topic, payload)

            dev["state"] = "pending"
            dev["detail"] = "confirming on the device…"
            await self.refresh()

            # Wait for the host-side proxy to report the marker landed.
            deadline = time.time() + 25
            while time.time() < deadline:
                await asyncio.sleep(0.5)
                r = self.last_result
                if r and r.get("hostname") == host:
                    if not r.get("success"):
                        dev["state"] = "failed"
                        dev["detail"] = "device did not answer the confirm marker"
                        await self.refresh()
                        raise Unavailable("device did not answer the confirm marker")
                    break
            else:
                dev["state"] = "failed"
                dev["detail"] = "timed out waiting for the device"
                await self.refresh()
                raise Unavailable("timed out waiting for the device")

            # Re-read, mutate, write back — same read-modify-write the backend
            # does, so a concurrent PWA onboard cannot be silently clobbered.
            await self._read_registry()
            modules = list(self.registered)
            if any(m.get("hostname") == host for m in modules):
                dev["state"] = "onboarded"
                await self.refresh()
                return {"onboarded": host, "via": "already present"}

            rec = _build_module_record(dev, modules)
            modules.append(rec)
            _apply_rename_rule(modules, rec["type"])
            await self._write_registry(modules)
            await self._read_registry()

            dev["state"] = "onboarded"
            dev["detail"] = ""
            await self.refresh()
            return {"onboarded": host, "name": rec["name"],
                    "via": "mqtt + direct mongo write"}

        raise Unavailable(f"discovery has no operation {op!r}")

    async def _ssh(self, command: str, timeout=40) -> str:
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        host = (v.get("headwaters_host") or "headwaters.local").strip()
        user = (v.get("headwaters_user") or "trailcurrent").strip()
        pw = v.get("headwaters_password") or v.get("mqtt_password") or ""
        key = str(Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
                  / "ssh" / "id_ed25519")
        have_key = os.path.isfile(key)
        if not have_key and not pw:
            raise Unavailable("no Headwaters credentials")
        return await sshcopy.run(host, user, command,
                                 key=key if have_key else None,
                                 password=None if have_key else pw,
                                 timeout=timeout)

    async def _write_registry(self, modules: list) -> None:
        """Write mcu_modules straight into Mongo.

        Mongo is deliberately NOT exposed on the network (no ports mapping in
        Headwaters' docker-compose.yml), so this goes through
        `docker exec ... mongosh` over the SSH channel.

        The payload is base64'd rather than interpolated into the --eval
        string: module names and hostnames would otherwise need escaping
        through ssh, the shell, AND the JS parser, and one stray quote would
        corrupt the rig's module list.
        """
        blob = base64.b64encode(json.dumps(modules).encode()).decode()
        js = ("const m = JSON.parse(Buffer.from('%s','base64').toString());"
              "db.system_config.updateOne({_id:'main'},"
              "{$set:{mcu_modules:m, updated_at:new Date()}});"
              "print('ok');" % blob)
        out = await self._ssh(
            "docker exec -i $(docker ps -qf name=mongodb) mongosh trailcurrent "
            "--quiet --eval \"%s\"" % js, timeout=45)
        if "ok" not in out:
            raise Unavailable(f"mongo write did not confirm: {out.strip()[:120]}")

    # ── Headwaters' saved-module registry ────────────────────────────
    async def _read_registry(self) -> None:
        """Read db.modules straight out of Headwaters' MongoDB over SSH.

        Headwaters exposes the registry only through an authenticated HTTP
        API, and the login is a separate admin account. Reading Mongo over the
        SSH channel we already have avoids needing a second credential and
        needs nothing added to Headwaters — see docs/api.md C1.

        Strictly read-only: a find(), nothing else.
        """
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        host = (v.get("headwaters_host") or "headwaters.local").strip()
        user = (v.get("headwaters_user") or "trailcurrent").strip()
        pw = v.get("headwaters_password") or v.get("mqtt_password") or ""
        key = str(Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
                  / "ssh" / "id_ed25519")
        have_key = os.path.isfile(key)
        if not have_key and not pw:
            self.registry_error = "no Headwaters credentials"
            return

        # Modules are persisted as system_config.mcu_modules (a field on the
        # single _id:"main" document), NOT a `modules` collection. Confirmed
        # in Headwaters containers/backend/src/routes/discovery.js:253,270 —
        # the /discovery/confirm route reads and $sets that field.
        self.set_busy(True, "reading Headwaters registry")
        cmd = ("docker exec $(docker ps -qf name=mongodb) mongosh trailcurrent "
               "--quiet --eval 'JSON.stringify((db.system_config.findOne("
               "{_id:\"main\"})||{}).mcu_modules||[])'")
        try:
            out = await sshcopy.run(host, user, cmd,
                                    key=key if have_key else None,
                                    password=None if have_key else pw,
                                    timeout=35)
            self.registered = json.loads(out.strip() or "[]")
            self.registry_error = None
        except (sshcopy.SSHError, ValueError) as exc:
            self.registry_error = str(exc)
        finally:
            self.set_busy(False)

    def _broker_creds(self) -> dict:
        """Credentials handed to a Playbill so it can reach the rig broker.

        These are the rig's own broker credentials, which Headwaters already
        holds — Tracer is relaying, not minting anything.
        """
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        return {
            "broker": v.get("broker", ""),
            "username": v.get("mqtt_username", ""),
            "password": v.get("mqtt_password", ""),
        }

    def tile_status(self):
        d = self.data or {}
        n = len(d.get("devices", []))
        if self.state == "ok":
            if d.get("browsing"):
                return (f"scanning · {n}", "#48E6FE")
            return (f"{n} found", "#48E6FE") if n else ("none found", "#666")
        return ("--", "#666")


def _mock_state():
    now = time.time()
    devs = [
        {"hostname": "bearing-3f2a", "type": "bearing", "fw": "1.4.2", "addr": 12,
         "canid": "0x150", "onboard": "confirm", "state": "found"},
        {"hostname": "solstice-08c1", "type": "solstice", "fw": "1.3.0", "addr": 4,
         "canid": "0x120", "onboard": "confirm", "state": "found"},
        {"hostname": "tapper-b71d", "type": "tapper", "fw": "2.0.1",
         "canid": "0x025", "onboard": "confirm", "state": "onboarded"},
        {"hostname": "playbill-galley", "type": "playbill", "deviceId": "galley",
         "onboard": "claim", "state": "found"},
        {"hostname": "spoor-4a90", "type": "spoor", "fw": "1.1.7", "addr": 9,
         "canid": "0x018", "onboard": "confirm", "state": "found"},
    ]
    for d in devs:
        d["first_seen"] = now - 60
        d["last_seen"] = now
        d.setdefault("detail", "")
    return {"devices": devs, "browsing": False, "browse_remaining": 0,
            "source": "headwaters", "last_result": None}
