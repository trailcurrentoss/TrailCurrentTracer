"""mqtt — subscribe to the rig broker and maintain the topic tree.

Read-only by default. The subscription is `#` minus an ignore list, and the
module never publishes on its own initiative — see docs/api.md C2.

Payloads stay STRINGS. The daemon reports whether a payload parsed as JSON but
does not pre-parse it: the Inspector has to be able to show a malformed
payload verbatim, which is exactly when the tool earns its keep.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..mqttclient import MQTTClient
from .base import Degraded, Module, Unavailable

log = logging.getLogger("tracerd.mqtt")

MAX_MESSAGES = 200      # rolling log shown in the right-hand pane
MAX_TOPICS = 500        # guard against a runaway publisher on a small device
EWMA_ALPHA = 0.3


def _split_hostport(s: str, default_port: int = 8883):
    s = (s or "").strip()
    if s.startswith("mqtts://"):
        s = s[8:]
    elif s.startswith("mqtt://"):
        s = s[7:]
    if ":" in s:
        host, _, port = s.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return s, default_port
    return s, default_port


class TopicNode:
    __slots__ = ("path", "count", "rate", "last_ts", "payload", "is_json",
                 "retained", "qos", "_window_count")

    def __init__(self, path):
        self.path = path
        self.count = 0
        self.rate = 0.0
        self.last_ts = 0.0
        self.payload = ""
        self.is_json = False
        self.retained = False
        self.qos = 0
        self._window_count = 0

    def sample(self, dt: float) -> None:
        """Recompute rate from messages counted since the last sample.

        NOT instantaneous 1/dt. Subscribing to `#` delivers every retained
        message in one burst with dt near zero, which makes 1/dt enormous —
        `can/inbound` read 8904/s against a real 142 msg/s total. Counting
        over a fixed window is immune to that, and is what a technician
        actually wants: messages per second, averaged.
        """
        if dt > 0:
            inst = self._window_count / dt
            self.rate = (EWMA_ALPHA * inst + (1 - EWMA_ALPHA) * self.rate
                         if self.rate else inst)
        self._window_count = 0

    def hit(self, payload: str, qos: int, retain: bool, now: float):
        self.count += 1
        self._window_count += 1
        self.last_ts = now
        self.payload = payload
        self.qos = qos
        self.retained = retain
        self.is_json = False
        p = payload.lstrip()
        if p[:1] in ("{", "["):
            try:
                json.loads(payload)
                self.is_json = True
            except ValueError:
                pass

    def as_dict(self):
        return {
            "path": self.path, "count": self.count,
            "rate": round(self.rate, 2), "retained": self.retained,
            "qos": self.qos, "json": self.is_json,
            "last": {"ts": round(self.last_ts, 3), "payload": self.payload},
        }


class MqttModule(Module):
    name = "mqtt"
    interval = 1.0
    backoff_interval = 5.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.topics: dict[str, TopicNode] = {}
        self.messages: list[dict] = []
        self.paused = False
        self.total = 0
        self._rate = 0.0
        self._last_total = 0
        self._last_rate_ts = time.monotonic()
        self._client: MQTTClient | None = None
        self._task: asyncio.Task | None = None
        self._status = "not configured"
        self._connected = False
        self._verified = False
        self._dropped = 0
        # Other modules ride the one broker connection rather than opening
        # their own — a second client id would fight the first for the
        # session (mosquitto closes the older one).
        self._subscribers: list = []

    # ── shared connection ────────────────────────────────────────────
    def add_subscriber(self, prefix: str, callback) -> None:
        """Call `callback(topic, payload_str)` for topics under `prefix`."""
        self._subscribers.append((prefix, callback))

    async def publish_json(self, topic: str, obj) -> None:
        """Publish for another module. Operator-initiated paths only."""
        await self.publish_raw(topic, json.dumps(obj).encode())

    async def publish_raw(self, topic: str, payload: bytes) -> None:
        """Some Headwaters topics carry a bare token, not JSON — e.g.
        local/discovery/trigger is the single byte '*'."""
        if not self._client or not self._connected:
            raise Unavailable("not connected to the broker")
        await self._client.publish(topic, payload)

    # ── config ───────────────────────────────────────────────────────
    def _config(self):
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        host, port = _split_hostport(v.get("broker", ""))
        return {
            "host": host, "port": port,
            "username": v.get("mqtt_username") or None,
            "password": v.get("mqtt_password") or None,
            "client_id": v.get("client_id") or "tracer",
            "ca_cert": (v.get("ca_cert") or "").strip(),
        }

    async def setup(self):
        if not self.mock:
            self._task = asyncio.create_task(self._run_client(), name="mqtt:client")

    # ── the connection loop ──────────────────────────────────────────
    async def _run_client(self):
        backoff = 2.0
        while True:
            cfg = self._config()
            if not cfg["host"]:
                self._status = "no broker configured"
                await asyncio.sleep(5)
                continue
            if not cfg["username"]:
                self._status = "no credentials — set them in Settings > MQTT"
                await asyncio.sleep(5)
                continue

            ca = cfg["ca_cert"] if cfg["ca_cert"] and os.path.isfile(cfg["ca_cert"]) else None
            client = MQTTClient(
                cfg["host"], cfg["port"], cfg["client_id"],
                username=cfg["username"], password=cfg["password"],
                use_tls=True, ca_cert=ca,
                # No CA on the device yet: observe unverified rather than not
                # at all, and say so in the UI. Never silently imply trust.
                insecure=ca is None,
            )
            try:
                await client.connect()
                await client.subscribe("#", 0)
                self._client = client
                self._connected = True
                self._verified = client.tls_verified
                self._status = "connected"
                backoff = 2.0
                log.info("connected to %s:%d as %s%s", cfg["host"], cfg["port"],
                         cfg["username"], "" if client.tls_verified else " (TLS unverified)")
                async for topic, payload, qos, retain in client.messages():
                    if self.paused:
                        continue
                    self._ingest(topic, payload, qos, retain)
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception as exc:
                self._status = str(exc) or exc.__class__.__name__
            finally:
                self._connected = False
                self._client = None
                try:
                    await client.close()
                except Exception:
                    pass

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _ingest(self, topic, payload: bytes, qos: int, retain: bool):
        now = time.time()
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            # Binary payloads are real (firmware chunks). Show hex rather
            # than mangling them — the Inspector must never lie about bytes.
            text = "0x" + payload[:64].hex()

        node = self.topics.get(topic)
        if node is None:
            if len(self.topics) >= MAX_TOPICS:
                self._dropped += 1
                return
            node = self.topics[topic] = TopicNode(topic)
        node.hit(text, qos, retain, now)
        self.total += 1

        for prefix, cb in self._subscribers:
            if topic.startswith(prefix):
                try:
                    cb(topic, text)
                except Exception:
                    log.exception("subscriber for %s failed", prefix)

        self.messages.append({
            "ts": round(now, 3), "topic": topic,
            "payload": text, "qos": qos, "retain": retain,
        })
        if len(self.messages) > MAX_MESSAGES:
            del self.messages[:-MAX_MESSAGES]

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        if self.mock:
            return _mock_state()

        now = time.monotonic()
        dt = now - self._last_rate_ts
        if dt >= 1.0:
            self._rate = (self.total - self._last_total) / dt
            self._last_total, self._last_rate_ts = self.total, now
            # Sample every topic over the same window, so per-topic rates and
            # the headline rate are consistent by construction.
            for node in self.topics.values():
                node.sample(dt)

        data = {
            "connected": self._connected,
            "status": self._status,
            "tls_verified": self._verified,
            "paused": self.paused,
            "rate": round(self._rate, 1),
            "total": self.total,
            "dropped": self._dropped,
            "topics": [n.as_dict() for n in
                       sorted(self.topics.values(), key=lambda n: n.path)],
            "messages": list(reversed(self.messages[-60:])),
        }
        if not self._connected:
            raise Degraded(self._status, data)
        return data

    async def handle(self, op, args):
        if op == "pause":
            self.paused = not self.paused
            await self.refresh()
            return {"paused": self.paused}
        if op == "clear":
            self.topics.clear()
            self.messages.clear()
            self.total = 0
            self._dropped = 0
            await self.refresh()
            return {"cleared": True}
        if op == "reconnect":
            if self._client:
                await self._client.close()
            return {"reconnecting": True}
        if op == "publish":
            # Operator-initiated only. Never called by a polling loop.
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            if not self._client or not self._connected:
                raise Unavailable("not connected")
            await self._client.publish(
                args["topic"], str(args.get("payload", "")).encode(),
                retain=bool(args.get("retain")))
            return {"published": args["topic"]}
        raise Unavailable(f"mqtt has no operation {op!r}")

    def tile_status(self):
        if self.state == "ok":
            return (f"{self.data['rate']:.0f} msg/s", "#74FE00")
        if self.state == "degraded" and self.data:
            return (self.data.get("status", "--")[:18], "#FFC107")
        return ("--", "#666")


def _mock_state():
    now = time.time()
    topics = [
        ("local/energy/status", 2.0, '{"batteryVoltage":13.4,"solarInput":850,"stateOfCharge":92}'),
        ("local/water/status", 0.2, '{"fresh":72,"grey":38,"black":12}'),
        ("local/gps/latlon", 1.0, '{"latitude":44.42711,"longitude":-110.58839}'),
        ("local/level/tilt", 1.0, '{"pitch":0.8,"roll":-2.4,"status":"Tilted right"}'),
        ("local/airquality/status", 0.5, '{"co2":1250,"voc":88,"aqi":"Moderate"}'),
        ("local/spoor/0/inputs", 4.0, '{"inputs":[1,0,0,1,0,0,0,0],"instance":0}'),
        ("can/inbound", 46.0, '{"identifier":"0x004","data_length_code":8,"data":[[0,0,0,1,0,1,0,0]]}'),
        ("can/outbound", 0.0, ""),
        ("discovery/browse/found", 0.0, '{"hostname":"bearing-3f2a","type":"bearing"}'),
    ]
    return {
        "connected": True, "status": "connected", "tls_verified": True,
        "paused": False, "rate": 46.0, "total": 1841, "dropped": 0,
        "topics": [{"path": p, "count": 100, "rate": r, "retained": False,
                    "qos": 0, "json": pl.startswith("{"),
                    "last": {"ts": now, "payload": pl}} for p, r, pl in topics],
        "messages": [{"ts": now - i * 0.2, "topic": p, "payload": pl,
                      "qos": 0, "retain": False}
                     for i, (p, r, pl) in enumerate(topics) if pl],
    }
