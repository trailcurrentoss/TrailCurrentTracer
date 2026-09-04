"""tracerd — the Tracer system daemon.

    python3 -m tracerd                 # on device
    python3 -m tracerd --mock          # laptop, synthetic data

Keymap capture is a standalone tool, not a flag here — see
tracerd/tools/capture_keymap.py.

Serves the WebSocket + REST API on 127.0.0.1:8710 and, when --ui-dir is given,
the built GUI from the same origin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .hub import Hub
from .modules.apps import ALL
from .modules.base import Unavailable as ModuleUnavailable
from .modules.inputmod import InputModule
from .mapproxy import PASSTHROUGH as MAP_PASSTHROUGH, PREFIX as MAP_PREFIX, MapProxy
from .wsserver import Server

VERSION = "0.4.1"
log = logging.getLogger("tracerd")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracerd", description="Tracer system daemon")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 — the authz model)")
    p.add_argument("--port", type=int, default=8710)
    p.add_argument("--mock", action="store_true",
                   help="synthesize traffic; no hardware needed")
    p.add_argument("--ui-dir", default=None,
                   help="serve a built tracer-ui bundle from this directory")
    p.add_argument("--input-device", default=None,
                   help="override the evdev node (default: auto-detect)")
    p.add_argument("--force-unavailable", default="", metavar="MOD[,MOD...]",
                   help="force modules into the unavailable state. Acceptance "
                        "criterion 3 requires proving each app degrades cleanly "
                        "when its dependency vanishes; this injects that without "
                        "physically pulling hardware.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def amain(args) -> int:
    hub = Hub(version=VERSION, mock=args.mock)

    # input first — it is the only input path and the UI waits on it
    try:
        hub.add(InputModule(hub, mock=args.mock, device=args.input_device))
    except FileNotFoundError as exc:
        log.error("cannot start input module: %s", exc)
        return 2

    forced = {m.strip() for m in args.force_unavailable.split(",") if m.strip()}

    for cls in ALL:
        hub.add(cls(hub, mock=args.mock))

    if forced:
        unknown = forced - set(hub.modules)
        if unknown:
            log.error("--force-unavailable: unknown module(s): %s",
                      ", ".join(sorted(unknown)))
            return 2
        for name in forced:
            _force_unavailable(hub.modules[name])
        log.warning("FAULT INJECTION: %s forced unavailable",
                    ", ".join(sorted(forced)))

    srv = Server(host=args.host, port=args.port)

    if args.ui_dir:
        ui = Path(args.ui_dir).resolve()
        if not ui.is_dir():
            log.error("--ui-dir %s does not exist", ui)
            return 2
        srv.static_dir = str(ui)
        log.info("serving UI from %s", ui)

    @srv.route("GET", "/health")
    async def _health(_body):
        return 200, "application/json", json.dumps({
            "ok": True, "daemon": VERSION, "mock": args.mock,
            "modules": {n: m.state for n, m in hub.modules.items()},
        }).encode()

    @srv.route("POST", "/rpc")
    async def _rpc(body):
        return await hub.rpc(body)

    # Static map assets from Headwaters, proxied over loopback so the page
    # never has to trust the rig's private CA. See mapproxy.py.
    proxy = MapProxy(hub)

    @srv.prefix(MAP_PREFIX)
    async def _map(path, headers):
        return await proxy.handle(path, headers)

    # The map style references its assets root-relative, so those paths must
    # resolve on tracerd too — see mapproxy.PASSTHROUGH.
    for _p in MAP_PASSTHROUGH:
        srv.prefix(_p)(_map)

    async def on_ws(client, recv):
        hub.clients.add(client)
        log.info("gui connected (%d client(s))", len(hub.clients))
        try:
            await client.send_json(hub.hello())
            for frame in hub.full_snapshot():
                await client.send_json(frame)
            while client.alive:
                msg = await recv()
                if msg is None:
                    break
                await _on_client_msg(hub, client, msg)
        finally:
            hub.clients.discard(client)
            log.info("gui disconnected (%d client(s))", len(hub.clients))
            inp = hub.modules.get("input")
            if inp is not None and hasattr(inp, "on_clients_changed"):
                inp.on_clients_changed(len(hub.clients))

    srv.on_ws = on_ws

    await srv.start()
    hub.start_all()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # systemd Type=notify — tell it we are actually accepting connections.
    _sd_notify("READY=1")

    log.info("tracerd %s ready on %s:%d%s",
             VERSION, args.host, args.port, " [mock]" if args.mock else "")

    serve = asyncio.create_task(srv.serve_forever())
    await stop.wait()
    log.info("shutting down")
    serve.cancel()
    await hub.stop_all()
    return 0


def _force_unavailable(module) -> None:
    """Replace poll() so the module reports a dependency-absent state.

    Deliberately leaves the supervisor loop untouched: the point is to prove
    the REAL degradation path runs, not to bypass it.
    """
    async def _poll():
        raise ModuleUnavailable(f"{module.name} forced unavailable (fault injection)")
    module.poll = _poll


async def _on_client_msg(hub: Hub, client, msg: str) -> None:
    try:
        req = json.loads(msg)
    except json.JSONDecodeError:
        return
    t = req.get("t")
    if t == "resync":
        name = req.get("m")
        mod = hub.modules.get(name)
        if mod:
            await client.send_json(
                {"t": "snap", "m": name, "seq": mod.seq, "d": mod.snapshot()}
            )
        else:
            for frame in hub.full_snapshot():
                await client.send_json(frame)
    elif t == "ping":
        await client.send_json({"t": "pong"})


def _sd_notify(state: str) -> None:
    """Minimal sd_notify. No-op when not run under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    import socket as _s
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
