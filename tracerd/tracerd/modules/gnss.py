"""gnss — position from the rig, and a direct check of the map tile source.

Both halves deliberately avoid the Headwaters API (docs/api.md C0):

  position  arrives on MQTT as local/gps/* — published by the CAN bridge from
            Bearing's GNSS frames. Nothing HTTP involved.

  tiles     are a static PMTiles file served by nginx at /maps/tiles.pmtiles
            (frontend nginx.conf:82 aliases it to data/maps/current). A Range
            request for the first 127 bytes returns the PMTiles header, which
            names the zoom range, bounds and tile count. That proves the map
            data is intact and reachable even when the PWA will not load —
            which is the case this app exists for.

There is no tileserver process: MapLibre reads the file directly over HTTP
Range. So "is the tileserver up" really means "does nginx still serve that
file, and is it a valid PMTiles archive".
"""

from __future__ import annotations

import asyncio
import json
import ssl
import struct
import time
import urllib.request

from .base import Degraded, Module, Unavailable

TILE_PATH = "/maps/tiles.pmtiles"
TILE_CHECK_INTERVAL = 60.0
PMTILES_HEADER = 127


def _parse_pmtiles_header(b: bytes) -> dict:
    """PMTiles v3 header. Layout from the spec; offsets are fixed."""
    if len(b) < PMTILES_HEADER or b[:7] != b"PMTiles":
        raise ValueError("not a PMTiles archive")
    version = b[7]
    if version != 3:
        raise ValueError(f"unsupported PMTiles version {version}")
    u64 = lambda o: struct.unpack_from("<Q", b, o)[0]      # noqa: E731
    i32 = lambda o: struct.unpack_from("<i", b, o)[0]      # noqa: E731
    return {
        "version": version,
        "addressed_tiles": u64(72),
        "tile_entries": u64(80),
        "tile_contents": u64(88),
        "min_zoom": b[100],
        "max_zoom": b[101],
        "bounds": [i32(102) / 1e7, i32(106) / 1e7,
                   i32(110) / 1e7, i32(114) / 1e7],
        "center_zoom": b[118],
        "center": [i32(119) / 1e7, i32(123) / 1e7],
    }


class GnssModule(Module):
    name = "gnss"
    interval = 1.0
    backoff_interval = 5.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.fix: dict = {}
        self.updated = 0.0
        self.tiles: dict | None = None
        self.tiles_error: str | None = None
        self._tiles_at = 0.0
        self._hooked = False

    async def setup(self):
        self._hook()

    def _hook(self):
        if self._hooked or self.mock:
            return
        mq = self.hub.modules.get("mqtt")
        if mq and hasattr(mq, "add_subscriber"):
            mq.add_subscriber("local/gps/", self._on_gps)
            self._hooked = True

    def _on_gps(self, topic: str, payload: str) -> None:
        try:
            d = json.loads(payload)
        except ValueError:
            return
        leaf = topic.rsplit("/", 1)[-1]
        # Field names are Headwaters' own — see can-bridge.js 0x006-0x009.
        if leaf == "latlon":
            self.fix["lat"] = d.get("latitude")
            self.fix["lon"] = d.get("longitude")
        elif leaf == "alt":
            self.fix["alt_m"] = d.get("altitudeInMeters")
            self.fix["alt_ft"] = d.get("altitudeFeet")
        elif leaf == "details":
            self.fix["sats"] = d.get("numberOfSatellites")
            self.fix["speed"] = d.get("speedOverGround")
            self.fix["course"] = d.get("courseOverGround")
            # gnssMode, not mode — the field name is Headwaters'
            # (can-bridge.js 0x007). Guessing it cost a permanently blank
            # "Fix" row that looked like a missing signal rather than a typo.
            self.fix["mode"] = d.get("gnssMode")
        elif leaf == "time":
            self.fix["utc"] = d
        self.updated = time.time()

    # ── tile source ──────────────────────────────────────────────────
    async def _check_tiles(self):
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        host = (v.get("headwaters_host") or "headwaters.local").strip()
        url = f"https://{host}{TILE_PATH}"

        def _get():
            ctx = ssl.create_default_context()
            ca = (v.get("ca_cert") or "").strip()
            import os
            if ca and os.path.isfile(ca):
                ctx.load_verify_locations(ca)
                verified = True
            else:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                verified = False
            req = urllib.request.Request(url)
            # Range, not a full GET: the archive is tens of gigabytes.
            req.add_header("Range", f"bytes=0-{PMTILES_HEADER - 1}")
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                body = r.read(PMTILES_HEADER)
                status = r.status
            return status, body, int((time.monotonic() - t0) * 1000), verified

        self.set_busy(True, "checking map tiles")
        try:
            status, body, ms, verified = await asyncio.to_thread(_get)
            if status not in (200, 206):
                raise ValueError(f"HTTP {status}")
            hdr = _parse_pmtiles_header(body)
            hdr.update({"ms": ms, "status": status, "url": url,
                        "range_supported": status == 206,
                        "tls_verified": verified})
            self.tiles = hdr
            self.tiles_error = None
        except Exception as exc:
            self.tiles_error = str(exc)
        finally:
            self.set_busy(False)
        self._tiles_at = time.time()

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        if self.mock:
            return _mock_state()
        self._hook()

        now = time.time()
        if now - self._tiles_at > TILE_CHECK_INTERVAL:
            self._tiles_at = now
            asyncio.create_task(self._check_tiles())

        age = int(now - self.updated) if self.updated else None
        has_fix = self.fix.get("lat") is not None and self.fix.get("lon") is not None
        data = {
            **self.fix,
            "age_s": age,
            "has_fix": has_fix,
            # Bearing reports mode; 3 == 3D, 2 == 2D. Absent means unknown, and
            # unknown must render as `--` rather than being guessed at.
            # NMEA fix mode from Bearing: 1 no fix, 2 = 2D, 3 = 3D. Anything
            # else stays None so the UI shows `--` rather than inventing one.
            "fix_type": {3: "3D", 2: "2D", 1: "No fix"}.get(self.fix.get("mode")),
            "mode_raw": self.fix.get("mode"),
            "tiles": self.tiles,
            "tiles_error": self.tiles_error,
        }
        if not has_fix:
            raise Degraded("no position from the rig yet", data)
        return data

    async def handle(self, op, args):
        if op == "check_tiles":
            self._tiles_at = 0.0
            await self._check_tiles()
            await self.refresh()
            return {"tiles": self.tiles, "error": self.tiles_error}
        raise Unavailable(f"gnss has no operation {op!r}")

    def tile_status(self):
        d = self.data or {}
        if d.get("has_fix"):
            sats = d.get("sats")
            return (f"{sats} sats · {d.get('fix_type') or '--'}"
                    if sats is not None else "fix", "#74FE00")
        if self.state == "degraded":
            return ("no fix", "#FFC107")
        return ("--", "#666")


def _mock_state():
    return {
        "lat": 44.42711, "lon": -110.58839, "alt_m": 2371, "alt_ft": 7779,
        "sats": 9, "speed": 0.0, "course": 118, "mode": 3,
        "age_s": 1, "has_fix": True, "fix_type": "3D",
        "tiles": {"version": 3, "min_zoom": 0, "max_zoom": 14,
                  "addressed_tiles": 18_400_221, "tile_entries": 9_120_004,
                  "bounds": [-111.2, 44.1, -109.9, 45.1], "center_zoom": 8,
                  "center": [-110.5, 44.6], "ms": 12, "status": 206,
                  "range_supported": True, "tls_verified": True,
                  "url": "https://headwaters.local/maps/tiles.pmtiles"},
        "tiles_error": None,
    }
