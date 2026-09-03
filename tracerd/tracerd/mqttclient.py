"""Minimal MQTT 3.1.1 client on asyncio, standard library only.

WHY NO DEPENDENCY
-----------------
Neither the board nor the build host has paho or aiomqtt, the image is built
offline, and Tracer only ever needs to subscribe, receive, and occasionally
publish. MQTT 3.1.1 is a small binary protocol; implementing the subset we
use is less risk than adding a wheel to an offline image build.

DELIBERATE SUBSET — not a general-purpose client:
  * QoS 0 and 1 inbound (QoS 2 is not used anywhere in the fleet)
  * outbound publish at QoS 0
  * clean session only — Tracer is an observer, it must not accumulate
    server-side queue state for a client id that comes and goes
  * no MQTT 5, no topic aliases, no will

Reference: MQTT 3.1.1, OASIS. Packet layout in comments below.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time

log = logging.getLogger("tracerd.mqtt")

CONNECT, CONNACK = 1, 2
PUBLISH, PUBACK = 3, 4
SUBSCRIBE, SUBACK = 8, 9
PINGREQ, PINGRESP = 12, 13
DISCONNECT = 14

CONNACK_ERRORS = {
    1: "broker rejected protocol version",
    2: "client id rejected",
    3: "broker unavailable",
    4: "bad username or password",
    5: "not authorised",
}


def _encode_len(n: int) -> bytes:
    """MQTT variable-length integer (1-4 bytes, 7 bits each)."""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


class MQTTClient:
    """One connection. Reconnection is the caller's job."""

    def __init__(self, host, port, client_id, username=None, password=None,
                 use_tls=True, ca_cert=None, insecure=False, keepalive=45):
        self.host, self.port = host, port
        self.client_id = client_id
        self.username, self.password = username, password
        self.use_tls, self.ca_cert, self.insecure = use_tls, ca_cert, insecure
        self.keepalive = keepalive
        self._r = self._w = None
        self._pid = 0
        self.connected = False
        self.tls_verified = False

    # ── connection ───────────────────────────────────────────────────
    async def connect(self) -> None:
        ctx = None
        if self.use_tls:
            ctx = ssl.create_default_context()
            if self.ca_cert:
                try:
                    ctx.load_verify_locations(self.ca_cert)
                    self.tls_verified = True
                except OSError as exc:
                    raise ConnectionError(f"CA certificate unreadable: {exc}")
            elif self.insecure:
                # The rig uses a private CA. Without it we can still observe
                # traffic, but the UI must say the connection is unverified
                # rather than implying a trusted chain.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self.tls_verified = False
            else:
                raise ConnectionError("no CA certificate configured")

        self._r, self._w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ctx), timeout=12
        )

        # CONNECT: protocol name, level 4, flags, keepalive, then payload.
        flags = 0x02  # clean session
        payload = _str(self.client_id)
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        var = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        await self._send(CONNECT, 0, var + payload)

        ptype, _flags, body = await self._read_packet(timeout=12)
        if ptype != CONNACK or len(body) < 2:
            raise ConnectionError("no CONNACK from broker")
        rc = body[1]
        if rc != 0:
            raise ConnectionError(CONNACK_ERRORS.get(rc, f"connect refused ({rc})"))
        self.connected = True

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self._pid = (self._pid % 65535) + 1
        body = struct.pack("!H", self._pid) + _str(topic) + bytes([qos])
        await self._send(SUBSCRIBE, 0x02, body)

    async def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        flags = 0x01 if retain else 0x00
        await self._send(PUBLISH, flags, _str(topic) + payload)

    async def ping(self) -> None:
        await self._send(PINGREQ, 0, b"")

    async def close(self) -> None:
        self.connected = False
        try:
            if self._w and not self._w.is_closing():
                await self._send(DISCONNECT, 0, b"")
                self._w.close()
        except (OSError, ConnectionError):
            pass

    # ── framing ──────────────────────────────────────────────────────
    async def _send(self, ptype: int, flags: int, body: bytes) -> None:
        if not self._w:
            raise ConnectionError("not connected")
        self._w.write(bytes([(ptype << 4) | flags]) + _encode_len(len(body)) + body)
        await self._w.drain()

    async def _read_packet(self, timeout=None):
        head = await asyncio.wait_for(self._r.readexactly(1), timeout=timeout)
        ptype, flags = head[0] >> 4, head[0] & 0x0F
        # Variable-length remaining-length field.
        n, mult = 0, 1
        for _ in range(4):
            b = (await asyncio.wait_for(self._r.readexactly(1), timeout=timeout))[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise ConnectionError("malformed remaining-length")
        body = await asyncio.wait_for(self._r.readexactly(n), timeout=timeout) if n else b""
        return ptype, flags, body

    async def messages(self):
        """Yield (topic, payload, qos, retain) until the link drops.

        Also answers PUBACK for QoS 1 and keeps the keepalive alive, so the
        caller only has to consume messages.
        """
        last_ping = time.monotonic()
        while self.connected:
            try:
                timeout = max(1.0, self.keepalive / 2)
                ptype, flags, body = await self._read_packet(timeout=timeout)
            except asyncio.TimeoutError:
                # Quiet bus is normal; keep the session alive.
                await self.ping()
                last_ping = time.monotonic()
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                self.connected = False
                return

            if ptype == PUBLISH:
                qos = (flags >> 1) & 0x03
                retain = bool(flags & 0x01)
                (tlen,) = struct.unpack("!H", body[:2])
                topic = body[2:2 + tlen].decode("utf-8", errors="replace")
                rest = body[2 + tlen:]
                if qos > 0:
                    pid = struct.unpack("!H", rest[:2])[0]
                    rest = rest[2:]
                    await self._send(PUBACK, 0, struct.pack("!H", pid))
                yield topic, rest, qos, retain
            elif ptype == PINGRESP:
                pass

            if time.monotonic() - last_ping > self.keepalive / 2:
                await self.ping()
                last_ping = time.monotonic()
