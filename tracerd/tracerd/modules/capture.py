"""capture — record MQTT traffic to disk and play it back.

Recording rides the existing broker connection (mqtt.add_subscriber) rather
than opening a second client: a second client id would fight the first for the
session, and mosquitto closes the older one.

PLAYBACK NEVER REPUBLISHES. It replays into the GUI only. Pushing recorded
frames back onto a live rig would inject stale energy readings, GPS fixes and
CAN frames into a vehicle that is running — the tool would become the fault it
is meant to diagnose. Playback is a view, not a transmitter.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from .base import Degraded, Module, Unavailable

MAX_BUFFER = 50_000          # messages held in RAM while recording
PREVIEW = 60                 # rows sent to the GUI

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _capture_dir() -> Path:
    return Path(os.environ.get("TRACER_STATE", "/var/lib/tracer")) / "captures"


class CaptureModule(Module):
    name = "capture"
    interval = 1.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.recording = False
        self.rec_name = ""
        self.rec_started = 0.0
        self.buffer: list[dict] = []
        self.dropped = 0
        self._hooked = False

        # playback
        self.play_file = ""
        self.playing = False
        self.play_pos = 0
        self.play_rows: list[dict] = []
        self.play_started = 0.0
        self.play_offset = 0.0
        self.loop = False
        self.loops = 0
        self._play_task: asyncio.Task | None = None

    async def setup(self):
        _capture_dir().mkdir(parents=True, exist_ok=True)
        self._hook()

    def _hook(self):
        if self._hooked or self.mock:
            return
        mq = self.hub.modules.get("mqtt")
        if mq and hasattr(mq, "add_subscriber"):
            mq.add_subscriber("", self._on_message)   # "" == every topic
            self._hooked = True

    def _on_message(self, topic: str, payload: str) -> None:
        if not self.recording:
            return
        if len(self.buffer) >= MAX_BUFFER:
            self.dropped += 1
            return
        self.buffer.append({"ts": round(time.time(), 3),
                            "topic": topic, "payload": payload})

    # ── files ────────────────────────────────────────────────────────
    def _sessions(self) -> list[dict]:
        out = []
        try:
            for p in sorted(_capture_dir().glob("*.jsonl"), reverse=True):
                st = p.stat()
                out.append({"name": p.stem, "file": p.name,
                            "bytes": st.st_size, "mtime": round(st.st_mtime, 0)})
        except OSError:
            pass
        return out

    async def _write(self, name: str) -> dict:
        safe = SAFE_NAME.sub("-", name).strip("-") or time.strftime("capture-%m%d-%H%M")
        path = _capture_dir() / f"{safe}.jsonl"
        rows = self.buffer
        started = self.rec_started

        def _do():
            _capture_dir().mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w") as fh:
                # A header line makes a capture self-describing — you can tell
                # what it is months later without opening the whole file.
                fh.write(json.dumps({"_capture": safe, "started": started,
                                     "ended": time.time(),
                                     "messages": len(rows)}) + "\n")
                for r in rows:
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            os.replace(tmp, path)
            return path.stat().st_size

        self.set_busy(True, "writing capture")
        try:
            size = await asyncio.to_thread(_do)
        finally:
            self.set_busy(False)
        return {"name": safe, "file": path.name, "bytes": size,
                "messages": len(rows)}

    async def _load(self, file: str) -> list[dict]:
        path = _capture_dir() / file
        if not path.is_file():
            raise Unavailable(f"{file} not found")

        def _do():
            rows = []
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if "_capture" in o:      # header
                        continue
                    rows.append(o)
            return rows
        self.set_busy(True, "loading capture")
        try:
            return await asyncio.to_thread(_do)
        finally:
            self.set_busy(False)

    # ── playback ─────────────────────────────────────────────────────
    LOOP_GAP = 0.6      # visible pause at the seam, so a restart reads as one

    async def _play_loop(self):
        """Advance the cursor in the recording's own timing.

        Playback only moves an index; nothing is published. See the module
        docstring — replaying onto a live rig would be actively harmful.
        """
        try:
            while self.playing:
                if self.play_pos >= len(self.play_rows):
                    if not self.loop or not self.play_rows:
                        break
                    # Wrap. A short pause at the seam matters: without it the
                    # jump back to the start is indistinguishable from the
                    # capture glitching, which is the opposite of useful when
                    # you are watching for a glitch.
                    self.loops += 1
                    self.play_pos = 0
                    await self.refresh()
                    await asyncio.sleep(self.LOOP_GAP)
                    continue

                cur = self.play_rows[self.play_pos]
                nxt = (self.play_rows[self.play_pos + 1]
                       if self.play_pos + 1 < len(self.play_rows) else None)
                self.play_pos += 1
                await self.refresh()
                if nxt is None:
                    continue          # let the wrap check above decide
                gap = max(0.0, min(2.0, nxt["ts"] - cur["ts"]))
                await asyncio.sleep(gap)
            self.playing = False
            await self.refresh()
        except asyncio.CancelledError:
            raise

    # ── state ────────────────────────────────────────────────────────
    async def poll(self):
        self._hook()
        if self.mock:
            return _mock_state()

        elapsed = (time.time() - self.rec_started) if self.recording else 0.0
        approx = sum(len(r["topic"]) + len(r["payload"]) + 40 for r in self.buffer[-200:])
        avg = (approx / max(1, len(self.buffer[-200:]))) if self.buffer else 0

        data = {
            "recording": self.recording,
            "name": self.rec_name,
            "elapsed": int(elapsed),
            "count": len(self.buffer),
            "bytes": int(avg * len(self.buffer)),
            "dropped": self.dropped,
            "sessions": self._sessions(),
            "dir": str(_capture_dir()),
            "playback": {
                "file": self.play_file,
                "playing": self.playing,
                "loop": self.loop,
                "loops": self.loops,
                "pos": self.play_pos,
                "total": len(self.play_rows),
                "rows": self.play_rows[max(0, self.play_pos - PREVIEW):self.play_pos][::-1],
            },
        }
        mq = self.hub.modules.get("mqtt")
        if not (mq and getattr(mq, "_connected", False)) and not self.play_rows:
            raise Degraded("broker not connected — cannot record", data)
        return data

    async def handle(self, op, args):
        if op == "start":
            if self.recording:
                raise Unavailable("already recording")
            self.buffer = []
            self.dropped = 0
            self.rec_name = args.get("name", "")
            self.rec_started = time.time()
            self.recording = True
            await self.refresh()
            return {"recording": True}

        if op == "stop":
            if not self.recording:
                raise Unavailable("not recording")
            self.recording = False
            if not self.buffer:
                await self.refresh()
                raise Unavailable("nothing captured")
            res = await self._write(args.get("name") or self.rec_name)
            self.buffer = []
            await self.refresh()
            return res

        if op == "rename":
            # Naming after the fact is the common case: you record first and
            # only then know what you caught.
            old = args.get("file", "")
            new = SAFE_NAME.sub("-", args.get("name", "")).strip("-")
            if not new:
                raise Unavailable("name cannot be empty")
            src = _capture_dir() / old
            dst = _capture_dir() / f"{new}.jsonl"
            if not src.is_file():
                raise Unavailable(f"{old} not found")
            if dst.exists():
                raise Unavailable(f"{new} already exists")
            await asyncio.to_thread(os.replace, src, dst)
            if self.play_file == old:
                self.play_file = dst.name
            await self.refresh()
            return {"renamed": dst.name}

        if op == "delete":
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            p = _capture_dir() / args.get("file", "")
            if p.is_file():
                await asyncio.to_thread(os.unlink, p)
            await self.refresh()
            return {"deleted": args.get("file")}

        if op == "load":
            self.play_rows = await self._load(args.get("file", ""))
            self.play_file = args.get("file", "")
            self.play_pos = 0
            self.playing = False
            self.loops = 0
            await self.refresh()
            return {"loaded": self.play_file, "messages": len(self.play_rows)}

        if op == "play":
            if not self.play_rows:
                raise Unavailable("no capture loaded")
            if self.play_pos >= len(self.play_rows):
                self.play_pos = 0
            self.playing = True
            if self._play_task and not self._play_task.done():
                self._play_task.cancel()
            self._play_task = asyncio.create_task(self._play_loop())
            await self.refresh()
            return {"playing": True}

        if op == "pause":
            self.playing = False
            if self._play_task and not self._play_task.done():
                self._play_task.cancel()
            await self.refresh()
            return {"playing": False}

        if op == "loop":
            self.loop = bool(args["on"]) if "on" in args else not self.loop
            await self.refresh()
            return {"loop": self.loop}

        if op == "seek":
            self.play_pos = max(0, min(len(self.play_rows), int(args.get("pos", 0))))
            await self.refresh()
            return {"pos": self.play_pos}

        raise Unavailable(f"capture has no operation {op!r}")

    def tile_status(self):
        d = self.data or {}
        if self.recording:
            return (f"rec {d.get('count', 0)}", "#FF5453")
        pb = d.get("playback", {})
        if pb.get("playing"):
            return ("looping" if pb.get("loop") else "playing", "#48E6FE")
        n = len(d.get("sessions", []))
        return (f"{n} saved", "#aaa") if n else ("idle", "#666")


def _mock_state():
    now = time.time()
    return {
        "recording": False, "name": "", "elapsed": 0, "count": 0, "bytes": 0,
        "dropped": 0, "dir": "/var/lib/tracer/captures",
        "sessions": [
            {"name": "capture-0412-1358", "file": "capture-0412-1358.jsonl",
             "bytes": 2_800_000, "mtime": now - 3600},
            {"name": "can-bus-fault-0409", "file": "can-bus-fault-0409.jsonl",
             "bytes": 22_400_000, "mtime": now - 86400},
        ],
        "playback": {"file": "", "playing": False, "loop": False, "loops": 0,
                     "pos": 0, "total": 0, "rows": []},
    }
