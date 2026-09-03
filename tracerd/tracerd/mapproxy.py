"""Proxy Headwaters' static map assets through tracerd.

WHY PROXY RATHER THAN FETCH DIRECTLY
------------------------------------
nginx already sets `Access-Control-Allow-Origin: *` on the tiles and on
/maps-static/, so CORS is not the obstacle. TLS is: Headwaters presents a
private-CA certificate, and Tracer's Chromium has no reason to trust it. A
direct fetch from the page dies on a certificate error that the operator can
do nothing about from a 3.5" screen.

tracerd already holds the CA (Settings › Headwaters Access › Fetch CA), so it
can make that request properly and hand the bytes to the browser over
loopback. From the page's point of view everything is same-origin plain HTTP.

Still no Headwaters API involved — these are static files served by nginx
(frontend nginx.conf:82 for the tiles), which is exactly the layer we want to
test when the PWA itself will not load.

Range requests are forwarded verbatim: PMTiles does random access over HTTP,
and swallowing Range would turn every tile read into a multi-gigabyte GET.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("tracerd.mapproxy")

PREFIX = "/hw/"

# Paths proxied straight through, WITHOUT the /hw/ prefix.
#
# The style JSON references its own assets root-relative:
#     "url":    "pmtiles:///maps/tiles.pmtiles"
#     "glyphs": "/maps-static/fonts/{fontstack}/{range}.pbf"
#     "sprite": "/maps-static/sprites/sprite"
#
# Those resolve against the PAGE origin, so they land on tracerd. Without
# these entries they hit the SPA fallback and MapLibre parses index.html as
# a tile archive — which is exactly the "Wrong magic number for PMTiles
# archive" failure. Serving them here lets the style work unmodified, the
# same file the PWA uses.
PASSTHROUGH = ("/maps/", "/maps-static/", "/libs/")

# Only these paths are proxied. An open relay to Headwaters would be a far
# larger surface than this feature needs.
ALLOWED = ("libs/", "maps-static/", "maps/")

MAX_BYTES = 8 << 20          # a PMTiles range read is small; cap the rest


class MapProxy:
    def __init__(self, hub):
        self.hub = hub

    def _target(self, path: str):
        rel = path[len(PREFIX):] if path.startswith(PREFIX) else path.lstrip("/")
        if not any(rel.startswith(a) or rel == a for a in ALLOWED):
            return None
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        host = (v.get("headwaters_host") or "headwaters.local").strip()
        return f"https://{host}/{rel}", v.get("ca_cert") or ""

    async def handle(self, path: str, req_headers: dict):
        t = self._target(path)
        if t is None:
            return 404, {"Content-Type": "text/plain"}, b"not proxied"
        url, ca = t
        rng = req_headers.get("range")

        def _get():
            ctx = ssl.create_default_context()
            if ca and os.path.isfile(ca):
                ctx.load_verify_locations(ca)
            else:
                # No CA installed yet: still serve the map rather than fail.
                # The GNSS screen reports tls_verified so this is visible.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url)
            if rng:
                req.add_header("Range", rng)
            try:
                with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                    body = r.read(MAX_BYTES)
                    hdrs = {
                        "Content-Type": r.headers.get("Content-Type",
                                                      "application/octet-stream"),
                        "Cache-Control": "public, max-age=86400",
                    }
                    cr = r.headers.get("Content-Range")
                    if cr:
                        hdrs["Content-Range"] = cr
                    hdrs["Accept-Ranges"] = "bytes"
                    return r.status, hdrs, body
            except urllib.error.HTTPError as exc:
                return exc.code, {"Content-Type": "text/plain"}, b"upstream error"

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            log.warning("map proxy failed for %s: %s", url, exc)
            return 502, {"Content-Type": "text/plain"}, str(exc).encode()
