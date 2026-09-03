"""firmware — deploy a Headwaters package the way a developer does by hand.

DELIBERATELY NOT the PWA uploader. Headwaters' web UI already accepts a zip
and deploys it; going through that path would only exercise the same code that
has broken before. This reproduces the documented manual flow instead, so it
still works when the uploader does not:

    scp trailcurrent-deployment-X.Y.Z.zip trailcurrent@<host>:~
    unzip -o trailcurrent-deployment-X.Y.Z.zip
    ./deploy.sh

Verbatim from Headwaters PI_DEPLOYMENT.md "Subsequent Updates" (lines 172-190).
Verification commands afterwards come from the same document (lines 234-258).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

from .. import sshcopy
from .base import Module, Unavailable

# Where to look for packages: a USB stick first (how one arrives in the field),
# then Tracer's own state directory.
SEARCH_DIRS = ["/media/usb0", "/media/usb1", "/mnt/usb"]

VERIFY = [
    ("containers", "docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | head -20"),
    ("cantomqtt", "systemctl is-active cantomqtt.service"),
    ("deployment-watcher", "systemctl is-active deployment-watcher.service"),
    ("map-watcher", "systemctl is-active map-watcher.service"),
    ("api", "curl -k -s -o /dev/null -w '%{http_code}' https://localhost/api/health"),
]


def _state_dir() -> Path:
    return Path(os.environ.get("TRACER_STATE", "/var/lib/tracer")) / "firmware"


class FirmwareModule(Module):
    name = "firmware"
    interval = 5.0

    def __init__(self, hub, mock: bool = False):
        super().__init__(hub)
        self.mock = mock
        self.busy = False
        self.stage = ""
        self.log: list[str] = []
        self.last_result: dict | None = None
        self.selected = ""
        self.browser: dict | None = None

    def _creds(self):
        st = self.hub.modules.get("settings")
        v = getattr(st, "values", {}) if st else {}
        key = str(Path(os.environ.get("TRACER_STATE", "/var/lib/tracer"))
                  / "ssh" / "id_ed25519")
        return {
            "host": (v.get("headwaters_host") or "headwaters.local").strip(),
            "user": (v.get("headwaters_user") or "trailcurrent").strip(),
            "pw": v.get("headwaters_password") or v.get("mqtt_password") or "",
            "key": key if os.path.isfile(key) else None,
        }

    # ── file browser ─────────────────────────────────────────────────
    def _places(self) -> list[dict]:
        """Starting points worth offering, and only the ones that exist.

        A package normally arrives on a USB stick, so mounted media comes
        first; home and /tmp cover a package copied over with scp.
        """
        out = []
        seen = set()
        for label, path in [
            ("USB / removable", "/media"),
            ("Mounted", "/mnt"),
            ("Home", os.path.expanduser("~")),
            ("Tracer firmware", str(_state_dir())),
            ("Downloads", os.path.expanduser("~/Downloads")),
            ("Temp", "/tmp"),
        ]:
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            out.append({"label": label, "path": path})
        return out

    def _browse(self, path: str) -> dict:
        path = os.path.abspath(os.path.expanduser(path or "/media"))
        if not os.path.isdir(path):
            raise Unavailable(f"{path} is not a directory")
        dirs, files = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=True):
                            dirs.append({"name": e.name, "dir": True,
                                         "path": os.path.join(path, e.name)})
                        elif e.name.lower().endswith(".zip"):
                            # Only .zip is selectable, so listing anything else
                            # would just be noise on a 640 px screen.
                            st = e.stat()
                            files.append({"name": e.name, "dir": False,
                                          "path": os.path.join(path, e.name),
                                          "bytes": st.st_size,
                                          "mtime": round(st.st_mtime, 0)})
                    except OSError:
                        continue
        except PermissionError:
            raise Unavailable(f"permission denied: {path}")
        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        parent = os.path.dirname(path.rstrip("/")) or "/"
        return {"path": path,
                "parent": None if path == "/" else parent,
                "entries": dirs + files,
                "zips": len(files)}

    def _packages(self) -> list[dict]:
        out, seen = [], set()
        dirs = [Path(d) for d in SEARCH_DIRS] + [_state_dir()]
        for d in dirs:
            try:
                if not d.is_dir():
                    continue
                for p in sorted(d.glob("trailcurrent-deployment-*.zip")):
                    if p.name in seen:
                        continue
                    seen.add(p.name)
                    st = p.stat()
                    out.append({"name": p.name, "path": str(p),
                                "bytes": st.st_size, "mtime": round(st.st_mtime, 0),
                                "where": str(d)})
            except OSError:
                continue
        return out

    def _note(self, text: str):
        self.log.append(text)
        del self.log[:-60]

    async def poll(self):
        if self.mock:
            return {"packages": [{"name": "trailcurrent-deployment-1.1.0.zip",
                                  "path": "/media/usb0/trailcurrent-deployment-1.1.0.zip",
                                  "bytes": 190_000_000, "mtime": time.time(),
                                  "where": "/media/usb0"}],
                    "busy": False, "stage": "", "log": [], "selected": "",
                    "last_result": None, "search_dirs": SEARCH_DIRS}
        return {"packages": self._packages(),
                "browser": self.browser,
                "places": self._places(),
                "busy": self.busy,
                "stage": self.stage, "log": self.log[-24:],
                "selected": self.selected, "last_result": self.last_result,
                "search_dirs": SEARCH_DIRS + [str(_state_dir())]}

    async def handle(self, op, args):
        if op == "browse":
            self.browser = self._browse(args.get("path") or "/media")
            await self.refresh()
            return self.browser

        if op == "browse_close":
            self.browser = None
            await self.refresh()
            return {"closed": True}

        if op == "select":
            p = args.get("path", "")
            if p and not os.path.isfile(p):
                raise Unavailable("file not found")
            self.selected = p
            self.browser = None
            await self.refresh()
            return {"selected": self.selected}
        if op == "deploy":
            if self.busy:
                raise Unavailable("a deployment is already running")
            if not args.get("confirm"):
                raise Unavailable("needs_confirmation")
            path = args.get("path") or self.selected
            if not path or not os.path.isfile(path):
                raise Unavailable("no package selected")
            asyncio.create_task(self._deploy(path))
            return {"started": os.path.basename(path)}
        if op == "verify":
            return await self._verify()
        raise Unavailable(f"firmware has no operation {op!r}")

    async def _deploy(self, path: str):
        c = self._creds()
        name = os.path.basename(path)
        self.busy = True
        self.set_busy(True, "deploying")
        self.log = []
        self.last_result = None
        try:
            if not c["key"] and not c["pw"]:
                raise Unavailable("no Headwaters credentials")

            # Checksum before and after: a zip truncated in transit would fail
            # deep inside deploy.sh with a confusing error, long after the
            # actual fault.
            self.stage = "hashing"
            await self.refresh()
            local_sha = await asyncio.to_thread(_sha256, path)
            self._note(f"local sha256 {local_sha[:16]}…")

            self.stage = "copying"
            await self.refresh()
            self._note(f"scp {name} -> {c['user']}@{c['host']}:~")
            t0 = time.monotonic()
            await sshcopy.put_file(c["host"], c["user"], path, f"~/{name}",
                                   key=c["key"], password=c["pw"], timeout=1800)
            self._note(f"copied in {int(time.monotonic() - t0)}s")

            self.stage = "verifying transfer"
            await self.refresh()
            out = await sshcopy.run(c["host"], c["user"],
                                    f"sha256sum ~/{name} | cut -d' ' -f1",
                                    key=c["key"], password=c["pw"], timeout=300)
            remote_sha = out.strip()
            if remote_sha != local_sha:
                raise Unavailable("checksum mismatch after transfer — copy again")
            self._note("checksum matches")

            self.stage = "unzipping"
            await self.refresh()
            out = await sshcopy.run(c["host"], c["user"],
                                    f"cd ~ && unzip -o {name} | tail -3",
                                    key=c["key"], password=c["pw"], timeout=600)
            for l in out.strip().splitlines():
                self._note(l.strip())

            self.stage = "running deploy.sh"
            await self.refresh()
            self._note("./deploy.sh — this takes several minutes")
            rc, out, err = await sshcopy.run_sudo(
                c["host"], c["user"], "cd ~ && ./deploy.sh 2>&1 | tail -40",
                c["pw"], key=c["key"], timeout=2400)
            for l in (out or "").strip().splitlines()[-24:]:
                self._note(l.rstrip())
            if rc != 0:
                raise Unavailable(f"deploy.sh exited {rc} — see the log above")

            self.stage = "verifying"
            await self.refresh()
            checks = await self._verify()
            self.last_result = {"ok": True, "package": name, "checks": checks,
                                "ts": time.time()}
            self.stage = "done"
            self._note("deployment complete")
        except Exception as exc:
            self.stage = "failed"
            self._note(f"FAILED: {exc}")
            self.last_result = {"ok": False, "package": name, "error": str(exc),
                                "ts": time.time()}
        finally:
            self.busy = False
            self.set_busy(False)
            await self.refresh()

    async def _verify(self) -> list[dict]:
        """Post-deploy checks, taken from PI_DEPLOYMENT.md lines 234-258."""
        c = self._creds()
        out = []
        for label, cmd in VERIFY:
            try:
                r = await sshcopy.run(c["host"], c["user"], cmd,
                                      key=c["key"], password=c["pw"], timeout=45)
                text = r.strip()
                ok = bool(text) and "inactive" not in text and "failed" not in text
                if label == "api":
                    ok = text.endswith("200")
                out.append({"name": label, "detail": text[:70] or "--", "ok": ok})
            except Exception as exc:
                out.append({"name": label, "detail": str(exc)[:70], "ok": False})
        return out

    def tile_status(self):
        d = self.data or {}
        if d.get("busy"):
            return (d.get("stage", "working")[:16], "#FFC107")
        r = d.get("last_result")
        if r:
            return ("deployed" if r.get("ok") else "failed",
                    "#74FE00" if r.get("ok") else "#FF5453")
        if d.get("selected"):
            return (os.path.basename(d["selected"])[:18], "#7BC96A")
        n = len(d.get("packages", []))
        return (f"{n} package{'' if n == 1 else 's'}", "#aaa") if n else ("none staged", "#666")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
