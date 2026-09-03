"""simulate — emulate any module's CAN traffic, from the fleet DBC.

Lets an operator inject frames for a module that may not be installed, or
send deliberately wrong values to see how the rest of the system copes. Useful
before a module is fitted, and for reproducing a fault you cannot reproduce
on demand.

Everything is generated from the vendored TrailCurrent.dbc — module list,
frame list, field names, bit layout, scaling, ranges, units, enumerations. So
the simulator cannot drift from the fleet definition: add a signal to the DBC
and it appears here with no code change.

TWO DIRECTIONS, and the difference matters:

  can/inbound   Pretend a module reported something. can-to-mqtt does NOT
                echo this to the bus; Headwaters' can-bridge decodes it as if
                it had arrived from a real module. This is how you emulate
                hardware that is not present.

  can/outbound  Put the frame on the REAL CAN bus. Physical modules will see
                and act on it. This is how you command a Switchback relay or
                a Torrent channel.

The default follows the DBC's own sender: a frame a module sends is emulated
inbound; a frame Headwaters sends is a command and goes outbound.
"""

from __future__ import annotations

import time

from .. import dbc
from .base import Module, Unavailable

# Module ids from Headwaters routes/modules.js MCU_MODULES.
KNOWN = ["bearing", "solstice", "borealis", "torrent", "switchback", "picket",
         "tapper", "therma", "plateau", "aftline", "ampline", "milepost",
         "reservoir", "spotter", "fireside", "playbill"]

DISPLAY = {m: m.capitalize() for m in KNOWN}


def _owner(msg: dict) -> str:
    """Which module a frame belongs to.

    The DBC sender is authoritative for telemetry. Commands are sent by
    Headwaters or are unattributed (Vector__XXX), so fall back to the message
    name — TorrentToggle0 belongs with Torrent even though Headwaters sends it.
    """
    s = (msg["sender"] or "").lower()
    if s in KNOWN:
        return s
    name = msg["name"].lower()
    for m in KNOWN:
        if name.startswith(m):
            return m
    return "system"


class SimulateModule(Module):
    name = "simulate"
    interval = 10.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.sent: list[dict] = []
        try:
            self.messages = dbc.parse()
            self.load_error = None
        except Exception as exc:
            self.messages = {}
            self.load_error = str(exc)

    # ── catalogue ────────────────────────────────────────────────────
    def _catalog(self) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for msg in self.messages.values():
            if not msg["signals"]:
                continue          # nothing to fill in; a bare trigger frame
            groups.setdefault(_owner(msg), []).append({
                "id": msg["id"], "hex": msg["hex"], "name": msg["name"],
                "dlc": msg["dlc"], "sender": msg["sender"],
                "signals": len(msg["signals"]), "comment": msg["comment"],
                "direction": self._default_direction(msg),
            })
        out = []
        for key in sorted(groups):
            frames = sorted(groups[key], key=lambda f: f["id"])
            out.append({
                "id": key,
                "label": DISPLAY.get(key, key.capitalize()),
                "frames": frames,
                "count": len(frames),
            })
        return out

    @staticmethod
    def _default_direction(msg: dict) -> str:
        sender = (msg["sender"] or "").lower()
        return "inbound" if sender in KNOWN else "outbound"

    def _frame(self, mid: int) -> dict:
        msg = self.messages.get(mid)
        if not msg:
            raise Unavailable(f"no frame with id {mid}")
        return {
            "id": msg["id"], "hex": msg["hex"], "name": msg["name"],
            "dlc": msg["dlc"], "sender": msg["sender"],
            "comment": msg["comment"],
            "direction": self._default_direction(msg),
            "signals": [{
                "name": s["name"], "length": s["length"], "unit": s["unit"],
                "min": s["min"], "max": s["max"], "scale": s["scale"],
                "offset": s["offset"], "signed": s["signed"],
                "comment": s["comment"],
                # A signal with a value table becomes a picker rather than a
                # free-text box — far less to get wrong on a 3.5" screen.
                "choices": [{"value": k, "label": v}
                            for k, v in sorted(s["values"].items())] or None,
                "default": self._suggest(s),
            } for s in msg["signals"]],
        }

    @staticmethod
    def _suggest(sig: dict):
        if sig["values"]:
            return min(sig["values"])
        lo, hi = sig["min"], sig["max"]
        if lo is None or hi is None:
            return 0
        if lo <= 0 <= hi:
            return 0
        return lo

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        if self.load_error:
            raise Unavailable(f"could not read the CAN database: {self.load_error}")
        mq = self.hub.modules.get("mqtt")
        connected = bool(mq and getattr(mq, "_connected", False))
        return {
            "modules": self._catalog(),
            "total_frames": sum(1 for m in self.messages.values() if m["signals"]),
            "connected": connected,
            "sent": self.sent[-12:][::-1],
        }

    async def handle(self, op, args):
        if op == "frame":
            return self._frame(int(args.get("id")))

        if op == "send":
            mid = int(args.get("id"))
            msg = self.messages.get(mid)
            if not msg:
                raise Unavailable(f"no frame with id {mid}")
            direction = args.get("direction") or self._default_direction(msg)
            if direction not in ("inbound", "outbound"):
                raise Unavailable("direction must be inbound or outbound")
            # Putting a frame on a live vehicle bus is not something to do by
            # accident, and neither is convincing Headwaters a module exists.
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")

            values = args.get("values") or {}
            try:
                data = dbc.encode(msg, values)
            except dbc.EncodeError as exc:
                raise Unavailable(str(exc))

            # The identifier spelling MATTERS and differs by direction:
            #
            #   inbound   can-to-mqtt.py:206 emits "0x" + format(id, '03x'),
            #             and can-bridge.js looks the parser up by that exact
            #             string. Sending "0x1b" instead of "0x01b" means the
            #             lookup misses and the frame is silently ignored —
            #             no error, nothing decoded, nothing to debug.
            #   outbound  can-to-mqtt.py parses with int(identifier, 16), so
            #             any spelling works; match Headwaters' own
            #             mqtt.js:1105 form for consistency on the wire.
            ident = f"0x{mid:03x}" if direction == "inbound" else f"0x{mid:x}"
            payload = {
                "identifier": ident,
                "data_length_code": msg["dlc"],
                "data": data,
                "extd": 0, "rtr": 0, "ss": 0, "self": 0,
            }
            topic = "can/inbound" if direction == "inbound" else "can/outbound"

            if self.mock:
                record = {"ts": time.time(), "name": msg["name"], "hex": msg["hex"],
                          "topic": topic, "values": values, "mock": True}
                self.sent.append(record)
                await self.refresh()
                return record

            mq = self.hub.modules.get("mqtt")
            if not mq:
                raise Unavailable("mqtt module not loaded")
            await mq.publish_json(topic, payload)

            record = {
                "ts": time.time(), "name": msg["name"], "hex": msg["hex"],
                "topic": topic, "values": values,
                "bytes": " ".join(f"{int(''.join(map(str, b)), 2):02X}"
                                  for b in data[:msg["dlc"]]),
            }
            self.sent.append(record)
            del self.sent[:-50]
            await self.refresh()
            return record

        raise Unavailable(f"simulate has no operation {op!r}")

    def tile_status(self):
        d = self.data or {}
        if self.state != "ok":
            return ("--", "#666")
        if self.sent:
            return (f"{len(self.sent)} sent", "#FFC107")
        return (f"{d.get('total_frames', 0)} frames", "#aaa")
