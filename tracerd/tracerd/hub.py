"""Hub — owns the modules, the clients, and the frame fan-out.

Implements the framing and rate discipline from docs/api.md §2:
  * per-module `seq`, so a stall in one module cannot corrupt another's view
  * coalescing into at most one flush per tick
  * a module with nothing to say sends NOTHING — no empty heartbeats
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("tracerd.hub")

PROTOCOL_VERSION = 1
TICK = 0.1  # 100 ms — see docs/api.md "Rate discipline"


class Hub:
    def __init__(self, version: str = "0.4.1", mock: bool = False):
        self.version = version
        self.mock = mock
        self.modules: dict[str, "object"] = {}
        self.clients: set = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._pending_snaps: dict[str, tuple[int, dict]] = {}
        self._pending_evs: list[tuple[str, dict]] = []
        self._last_tiles: list | None = None
        self._flush_task: asyncio.Task | None = None

    # ── registration ─────────────────────────────────────────────────
    def add(self, module) -> None:
        self.modules[module.name] = module

    def start_all(self) -> None:
        self.loop = asyncio.get_running_loop()
        for m in self.modules.values():
            m.start()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="hub:flush")

    # Every module is individually bounded by Module.STOP_TIMEOUT, so this is
    # only the outer backstop for a module that overrides stop() badly. It must
    # stay comfortably under tracerd.service's TimeoutStopSec.
    STOP_ALL_TIMEOUT = 5.0

    async def stop_all(self) -> None:
        """Stop every module concurrently, and always return.

        This used to await each module in turn. One module that never finished
        cancelling therefore blocked every module behind it AND the caller, and
        because nothing was bounded the daemon simply never exited: on hardware
        it logged "shutting down" and then sat until systemd SIGKILLed it 90 s
        later. Concurrent, so a slow module delays only itself; bounded, so a
        stuck one cannot hold the shutdown open.
        """
        if self._flush_task:
            self._flush_task.cancel()
        if not self.modules:
            return
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(m.stop() for m in self.modules.values()),
                    return_exceptions=True,
                ),
                timeout=self.STOP_ALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("stop_all exceeded %.1fs; exiting anyway",
                        self.STOP_ALL_TIMEOUT)
            return
        for m, res in zip(self.modules.values(), results):
            if isinstance(res, BaseException):
                log.warning("module %s failed to stop cleanly: %r", m.name, res)

    # ── outbound ─────────────────────────────────────────────────────
    def broadcast_snap(self, name: str, seq: int, snap: dict) -> None:
        # Coalesce: only the newest snapshot per module survives a tick.
        self._pending_snaps[name] = (seq, snap)

    def broadcast_ev(self, name: str, data: dict) -> None:
        # Input events are NOT coalesced — dropping a button press would make
        # the device feel broken. They flush on the next tick, in order.
        self._pending_evs.append((name, data))

    def broadcast_now(self, name: str, data: dict) -> None:
        """Send immediately, bypassing the 100 ms coalescing tick.

        The tick is right for module snapshots — it stops a 700 msg/s broker
        turning into 700 frames/s. It is wrong for terminal echo: waiting up
        to 100 ms to see the character you just typed makes the shell feel
        broken, and that is exactly how it felt.

        Safe to skip the queue here because the terminal always sends its
        whole visible buffer rather than deltas, so a dropped or reordered
        frame cannot corrupt the view.
        """
        if not self.clients or self.loop is None:
            return
        frame = {"t": "ev", "m": name, "d": data}
        self.loop.create_task(self._send_all([frame]))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            if not self.clients:
                # No GUI attached: drop the backlog rather than growing it.
                self._pending_snaps.clear()
                self._pending_evs.clear()
                continue
            frames = []
            for name, (seq, snap) in self._pending_snaps.items():
                frames.append({"t": "snap", "m": name, "seq": seq, "d": snap})

            # Tile statuses are derived from module state, so they have to be
            # re-sent whenever module state moves. Sending them only in the
            # `hello` frame froze the launcher at whatever the modules happened
            # to be reporting the instant the GUI connected — a GUI that
            # attached during startup showed "not configured" for MQTT forever,
            # while the app itself worked fine.
            if self._pending_snaps:
                tiles = self.launcher_tiles()
                if tiles != self._last_tiles:
                    self._last_tiles = tiles
                    frames.append({"t": "apps", "d": tiles})

            self._pending_snaps.clear()
            for name, data in self._pending_evs:
                frames.append({"t": "ev", "m": name, "d": data})
            self._pending_evs.clear()
            if frames:
                await self._send_all(frames)

    async def _send_all(self, frames: list[dict]) -> None:
        dead = []
        for c in list(self.clients):
            if not c.alive:
                dead.append(c)
                continue
            for f in frames:
                await c.send_json(f)
        for c in dead:
            self.clients.discard(c)

    async def send_toast(self, text: str, level: str = "info") -> None:
        await self._send_all([{"t": "toast", "level": level, "text": text}])

    # ── client lifecycle ─────────────────────────────────────────────
    def hello(self) -> dict:
        return {
            "t": "hello",
            "v": PROTOCOL_VERSION,
            "daemon": self.version,
            "mock": self.mock,
            "ts": time.time(),
            # Which modules can ever be `ok` on this unit. The UI uses this to
            # hide controls that could never work, rather than showing inert
            # ones — see docs/api.md §3.
            "caps": {name: True for name in self.modules},
            "apps": self.launcher_tiles(),
        }

    def full_snapshot(self) -> list[dict]:
        return [
            {"t": "snap", "m": name, "seq": m.seq, "d": m.snapshot()}
            for name, m in self.modules.items()
        ]

    # ── launcher ─────────────────────────────────────────────────────
    # Order and identity come from the design mock (v2.dc.html:509-522).
    APPS = [
        ("mqtt",       "MQTT Inspector", "pulse",          "#52A441", "#000"),
        ("discovery",  "Discovery",      "scan",           "#48E6FE", "#000"),
        ("capture",    "Capture",        "recording",      "#FF5453", "#000"),
        ("firmware",   "Firmware",       "cloud-upload",   "#7BC96A", "#000"),
        ("net",        "Network",        "wifi",           "#3D7B31", "#fff"),
        ("terminal",   "Terminal",       "terminal",       "#2a2a2a", "#7BC96A"),
        ("logs",       "Logs",           "document-text",  "#4a4a4a", "#fff"),
        ("can",        "CAN Sniffer",    "swap-vertical",  "#FFC107", "#000"),
        ("headwaters", "Headwaters",     "server",         "#52A441", "#000"),
        ("gnss",       "GNSS & Map",     "location",       "#48E6FE", "#000"),
        ("simulate",   "Simulate",       "swap-vertical",  "#7BC96A", "#000"),
        ("moduledebug","Module Debug",   "terminal",       "#48E6FE", "#000"),
        ("settings",   "Settings",       "settings",       "#4a4a4a", "#fff"),
    ]

    def launcher_tiles(self) -> list[dict]:
        out = []
        for mid, short, icon, tint, glyph in self.APPS:
            mod = self.modules.get(mid)
            if mod is not None:
                status, colour = mod.tile_status()
                state = mod.state
            else:
                status, colour, state = "--", "#666", "unavailable"
            out.append({
                "id": mid, "short": short, "icon": icon,
                "tint": tint, "glyph": glyph,
                "status": status, "statusColor": colour, "state": state,
            })
        return out

    # ── commands ─────────────────────────────────────────────────────
    async def rpc(self, payload: bytes) -> tuple[int, str, bytes]:
        def reply(obj):
            return 200, "application/json", json.dumps(obj).encode()

        try:
            req = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return reply({"ok": False, "err": {"code": "bad_json",
                                               "msg": "malformed request"}})

        cid = req.get("id")
        mname = req.get("m")
        op = req.get("op")
        args = req.get("args") or {}

        mod = self.modules.get(mname)
        if mod is None:
            return reply({"id": cid, "ok": False,
                          "err": {"code": "no_module",
                                  "msg": f"unknown module {mname!r}"}})
        try:
            result = await mod.handle(op, args)
            return reply({"id": cid, "ok": True, "d": result})
        except Exception as exc:
            # A command failure is always HTTP 200 with ok:false, so the UI
            # never confuses "the daemon is gone" with "that didn't work".
            return reply({"id": cid, "ok": False,
                          "err": {"code": "failed", "msg": str(exc)}})
