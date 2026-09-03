"""Minimal HTTP + WebSocket server on asyncio, standard library only.

WHY NO DEPENDENCY
-----------------
Neither the build host nor the device has aiohttp, and the Tracer image is
built offline for a field tool. A loopback-only server with one client does
not justify pulling a dependency into the image, so this implements the
subset of RFC 6455 that the daemon actually uses.

DELIBERATE SUBSET — this is not a general-purpose WebSocket library:
  * text frames only (the protocol is JSON; no binary frames are ever sent)
  * server never masks (per spec), client frames are always unmasked
  * continuation frames ARE handled — a large `snap` can be fragmented by
    an intermediary, and silently dropping those would be a real bug
  * ping/pong and close handled; no extensions, no compression, no subprotocols

Bound to 127.0.0.1 only. That binding IS the authorization model — see
docs/api.md section 1.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from typing import Awaitable, Callable

log = logging.getLogger("tracerd.ws")

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes
_OP_CONT, _OP_TEXT, _OP_BIN = 0x0, 0x1, 0x2
_OP_CLOSE, _OP_PING, _OP_PONG = 0x8, 0x9, 0xA

# A single frame larger than this is refused rather than buffered. The daemon
# only ever receives small control messages from the GUI; anything huge is a
# bug or an attack, and unbounded buffering on a device with a read-only
# rootfs and limited RAM is not an acceptable failure mode.
MAX_FRAME = 1 << 20  # 1 MiB


class WSClient:
    """One connected GUI. Writes are serialized behind a lock."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._r = reader
        self._w = writer
        self._lock = asyncio.Lock()
        self.alive = True

    async def send_json(self, obj) -> None:
        await self.send_text(json.dumps(obj, separators=(",", ":")))

    async def send_text(self, text: str) -> None:
        if not self.alive:
            return
        payload = text.encode("utf-8")
        async with self._lock:
            try:
                self._w.write(_build_frame(_OP_TEXT, payload))
                await self._w.drain()
            except (ConnectionResetError, BrokenPipeError):
                # Normal when the GUI restarts; the read loop will clean up.
                self.alive = False

    async def _send_raw(self, opcode: int, payload: bytes) -> None:
        if not self.alive:
            return
        async with self._lock:
            try:
                self._w.write(_build_frame(opcode, payload))
                await self._w.drain()
            except (ConnectionResetError, BrokenPipeError):
                self.alive = False

    async def close(self) -> None:
        if self.alive:
            self.alive = False
            try:
                await self._send_raw(_OP_CLOSE, b"")
                self._w.close()
            except Exception:
                pass


def _build_frame(opcode: int, payload: bytes) -> bytes:
    """Server->client frame. Never masked, never fragmented by us."""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < (1 << 16):
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


async def _read_exact(r: asyncio.StreamReader, n: int) -> bytes:
    return await r.readexactly(n)


async def _read_message(r: asyncio.StreamReader, client: WSClient) -> str | None:
    """Read one complete (possibly fragmented) text message. None on close."""
    chunks: list[bytes] = []
    msg_op: int | None = None

    while True:
        b0, b1 = await _read_exact(r, 2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F

        if ln == 126:
            (ln,) = struct.unpack("!H", await _read_exact(r, 2))
        elif ln == 127:
            (ln,) = struct.unpack("!Q", await _read_exact(r, 8))

        if ln > MAX_FRAME:
            log.warning("ws: frame of %d bytes exceeds limit, closing", ln)
            return None

        mask = await _read_exact(r, 4) if masked else None
        data = await _read_exact(r, ln) if ln else b""
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))

        # Control frames may be interleaved mid-fragmentation and are never
        # themselves fragmented.
        if opcode == _OP_CLOSE:
            return None
        if opcode == _OP_PING:
            await client._send_raw(_OP_PONG, data)
            continue
        if opcode == _OP_PONG:
            continue

        if opcode in (_OP_TEXT, _OP_BIN):
            msg_op = opcode
            chunks = [data]
        elif opcode == _OP_CONT:
            chunks.append(data)
        else:
            log.warning("ws: unknown opcode 0x%x, closing", opcode)
            return None

        if fin:
            if msg_op == _OP_BIN:
                # Not part of our protocol. Ignore rather than kill the link.
                chunks, msg_op = [], None
                continue
            return b"".join(chunks).decode("utf-8", errors="replace")


class Server:
    """HTTP + WebSocket on one port.

    routes    : dict[(method, path)] -> async handler(body: bytes) -> (status, ctype, bytes)
    on_ws     : async handler(client, recv_iter)
    static_dir: optional directory served for GET paths not otherwise routed
    """

    def __init__(self, host="127.0.0.1", port=8710):
        self.host, self.port = host, port
        self.routes: dict[tuple[str, str], Callable[[bytes], Awaitable]] = {}
        # Prefix handlers get (path, request_headers) and return
        # (status, response_headers, body). Used for the map proxy, which
        # needs Range in and Content-Range out — neither fits the simple
        # exact-match route signature.
        self.prefix_routes: list[tuple[str, Callable]] = []
        self.on_ws: Callable[[WSClient], Awaitable] | None = None
        self.static_dir = None
        self._srv = None

    def prefix(self, path_prefix: str):
        def deco(fn):
            self.prefix_routes.append((path_prefix, fn))
            return fn
        return deco

    def route(self, method: str, path: str):
        def deco(fn):
            self.routes[(method.upper(), path)] = fn
            return fn
        return deco

    async def start(self):
        self._srv = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("listening on http://%s:%d", self.host, self.port)

    async def serve_forever(self):
        assert self._srv
        async with self._srv:
            await self._srv.serve_forever()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(r.readline(), timeout=15)
            if not request_line:
                w.close()
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                w.close()
                return
            method, target = parts[0].upper(), parts[1]
            path = target.split("?", 1)[0]

            headers = {}
            while True:
                line = await asyncio.wait_for(r.readline(), timeout=15)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()

            if headers.get("upgrade", "").lower() == "websocket":
                await self._do_ws(r, w, headers, path)
                return

            body = b""
            if (n := int(headers.get("content-length", 0) or 0)) > 0:
                if n > MAX_FRAME:
                    await self._respond(w, 413, "text/plain", b"too large")
                    return
                body = await _read_exact(r, n)

            handler = self.routes.get((method, path))
            pre = next((fn for p, fn in self.prefix_routes
                        if path.startswith(p)), None)
            if handler:
                status, ctype, payload = await handler(body)
                await self._respond(w, status, ctype, payload)
            elif pre is not None and method == "GET":
                status, hdrs, payload = await pre(path, headers)
                await self._respond_raw(w, status, hdrs, payload)
            elif method == "GET" and self.static_dir:
                await self._serve_static(w, path)
            else:
                await self._respond(w, 404, "text/plain", b"not found")
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            pass
        except Exception:
            log.exception("http handler failed")
        finally:
            if not w.is_closing():
                try:
                    w.close()
                except Exception:
                    pass

    async def _do_ws(self, r, w, headers, path):
        key = headers.get("sec-websocket-key")
        if not key or self.on_ws is None:
            await self._respond(w, 400, "text/plain", b"bad websocket request")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("latin-1")).digest()
        ).decode()
        w.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        await w.drain()

        client = WSClient(r, w)
        try:
            await self.on_ws(client, lambda: _read_message(r, client))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception:
            log.exception("ws handler failed")
        finally:
            client.alive = False

    async def _serve_static(self, w, path):
        import mimetypes
        import os

        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(self.static_dir, rel))
        # Containment check — a GUI bug must not be able to read the rootfs.
        if not full.startswith(os.path.abspath(self.static_dir)):
            await self._respond(w, 403, "text/plain", b"forbidden")
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            # SPA fallback so deep links work.
            full = os.path.join(self.static_dir, "index.html")
            if not os.path.isfile(full):
                await self._respond(w, 404, "text/plain", b"not found")
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            await self._respond(w, 200, ctype, fh.read())

    async def _respond_raw(self, w, status: int, hdrs: dict, payload: bytes):
        reason = {200: "OK", 206: "Partial Content", 302: "Found",
                  400: "Bad Request", 404: "Not Found",
                  500: "Internal Server Error", 502: "Bad Gateway"}.get(status, "OK")
        head = [f"HTTP/1.1 {status} {reason}"]
        for k, v in (hdrs or {}).items():
            head.append(f"{k}: {v}")
        head.append(f"Content-Length: {len(payload)}")
        head.append("Connection: close")
        w.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await w.drain()

    async def _respond(self, w, status: int, ctype: str, payload: bytes):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                  404: "Not Found", 413: "Payload Too Large",
                  500: "Internal Server Error"}.get(status, "OK")
        w.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n".encode("latin-1") + payload
        )
        await w.drain()
