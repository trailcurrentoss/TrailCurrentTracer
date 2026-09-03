#!/usr/bin/env python3
"""Record the panel while every app in the launcher loads.

Runs ON the board. Produces an MP4 of the real 640x480 panel showing the boot
screen, the launcher, and each app in turn as it loads and populates from the
daemon.

Why it drives Chromium rather than the buttons or the touchscreen
-----------------------------------------------------------------
Buttons arrive from tracerd's input module, which reads one evdev node matched
by name out of /proc/bus/input/devices. A synthetic uinput keyboard cannot take
that node from the real one, so there is no way to press Start from a script.

Touch can open an app -- every tile carries data-idx and the delegated handler
in main.js turns a tap into the same open Start issues -- but it cannot leave
one: hintBar() in chrome/chrome.js renders the "Select Back" chip as plain
markup with no data-idx and no listener, so nothing in the hint bar is
tappable. A touch-driven walkthrough would strand itself in the first app.

What is left is the ?screen= deep link main.js already reads for the
screenshot-diff workflow. Navigating to it reloads the page, so each app is
genuinely observed loading and reconnecting to the daemon rather than being
revealed from a warm launcher. Driving that needs a URL change, which in kiosk
mode needs the DevTools protocol, which needs Chromium restarted once with a
debug port. This script restarts it, records, and restarts it again on the
original command line so the board is left exactly as it was found.

Usage:
    python3 tools/record_walkthrough.py [--out FILE] [--dwell SECONDS]
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

CDP_PORT = 9222
DEFAULT_OUT = os.path.expanduser("~/tracer-apps.mp4")
DEV_DIR = os.path.expanduser("~/tracer-dev")
AUTOSTART = os.path.join(DEV_DIR, "tracer-dev-autostart.sh")

# Mirrors the kiosk launch in tracer-dev-autostart.sh. Only used for the
# temporary debug-port run -- the restore at the end re-runs the autostart
# script itself, so the board is never left on a command line reconstructed
# here. Reading the live argv out of /proc was tried first and is a trap: the
# browser process shares its comm with several helpers, so the wrong argv gets
# picked up and relaunching with it takes the kiosk down.
KIOSK_FLAGS = [
    "--kiosk", "--ozone-platform=wayland",
    "--noerrdialogs", "--disable-infobars", "--hide-scrollbars",
    "--disable-features=Translate,TranslateUI",
    "--check-for-update-interval=31536000", "--force-device-scale-factor=1",
    "--password-store=basic", "--use-mock-keychain",
    f"--user-data-dir={DEV_DIR}/chrome-profile",
]


def env():
    e = dict(os.environ)
    e.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    e.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return e


def log(msg):
    print(f"[record] {msg}", flush=True)


# ── the app list comes from the daemon, never from a hardcoded copy ──
async def fetch_apps(port):
    import websockets
    async with websockets.connect(f"ws://127.0.0.1:{port}/stream") as ws:
        deadline = time.time() + 15
        while time.time() < deadline:
            frame = json.loads(await ws.recv())
            payload = frame.get("d") or frame
            apps = payload.get("apps") if isinstance(payload, dict) else None
            if isinstance(apps, list) and apps:
                return apps
    raise SystemExit("daemon never sent an app list")


# ── chromium ─────────────────────────────────────────────────────────
def kill_chromium():
    subprocess.run(["pkill", "-x", "chromium"], check=False)
    for _ in range(20):
        if not subprocess.run(["pgrep", "-x", "chromium"],
                              capture_output=True).stdout.strip():
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-x", "chromium"], check=False)
    time.sleep(1.0)


def start_debug_kiosk(base):
    """Temporary kiosk with a DevTools port. Torn down in the finally block."""
    kill_chromium()
    argv = (["chromium"] + KIOSK_FLAGS
            + [f"--remote-debugging-port={CDP_PORT}", base + "/"])
    with open(os.path.join(DEV_DIR, "chromium.log"), "ab") as out:
        subprocess.Popen(["setsid"] + argv, env=env(),
                         stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def restore_kiosk():
    """Put the board back exactly the way the board puts itself back at boot."""
    kill_chromium()
    if os.path.exists(AUTOSTART):
        subprocess.run(["setsid", AUTOSTART], env=env(), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        log(f"WARNING: {AUTOSTART} missing — kiosk not restored")


def tail_chromium_log(n=15):
    path = os.path.join(DEV_DIR, "chromium.log")
    if not os.path.exists(path):
        return "(no chromium.log)"
    with open(path, errors="replace") as fh:
        return "".join(fh.readlines()[-n:])


def wait_for_cdp(timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json", timeout=2).read()
            for target in json.loads(raw):
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("chromium never exposed a DevTools page target\n"
                     + tail_chromium_log())


# ── recorder ─────────────────────────────────────────────────────────
def start_recorder(out, output_name, fps):
    if os.path.exists(out):
        os.remove(out)
    cmd = ["wf-recorder", "-o", output_name, "-f", out,
           "-c", "libx264", "-x", "yuv420p", "-r", str(fps),
           "-p", "preset=veryfast", "-p", "crf=20"]
    proc = subprocess.Popen(cmd, env=env(),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            start_new_session=True)
    # wf-recorder writes the container header on the first frame; give it a
    # moment so the opening of the video is not clipped.
    time.sleep(2.5)
    if proc.poll() is not None:
        raise SystemExit("wf-recorder died: "
                         + proc.stderr.read().decode(errors="replace"))
    return proc


def stop_recorder(proc):
    # SIGINT, not SIGTERM: wf-recorder only finalises the MP4 (writes the moov
    # atom) on an interrupt. SIGTERM leaves an unplayable file.
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ── walkthrough ──────────────────────────────────────────────────────
async def walk(ws_url, base, apps, dwell):
    import websockets
    msg_id = 0

    async with websockets.connect(ws_url, max_size=None) as ws:
        async def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            await ws.send(json.dumps(
                {"id": msg_id, "method": method, "params": params or {}}))

        async def navigate(url, hold):
            await send("Page.navigate", {"url": url})
            await asyncio.sleep(hold)

        # Cold start: the real boot screen, which ?screen= deliberately skips.
        log("boot screen")
        await navigate(base + "/", dwell + 2)

        log("launcher")
        await navigate(base + "/?screen=launcher", dwell)

        for app in apps:
            log(f"{app['short']}  (?screen={app['id']})")
            await navigate(f"{base}/?screen={app['id']}", dwell)

        log("back to launcher")
        await navigate(base + "/?screen=launcher", dwell)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dwell", type=float, default=4.0,
                    help="seconds to hold on each app")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--output-name", default="HDMI-A-1")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"

    apps = asyncio.run(fetch_apps(args.port))
    log(f"{len(apps)} apps: " + ", ".join(a["id"] for a in apps))

    recorder = None
    try:
        log("restarting kiosk with a DevTools port")
        start_debug_kiosk(base)
        ws_url = wait_for_cdp()
        time.sleep(4)   # let the first paint settle before rolling

        log(f"recording to {args.out}")
        recorder = start_recorder(args.out, args.output_name, args.fps)
        asyncio.run(walk(ws_url, base, apps, args.dwell))
    finally:
        if recorder:
            stop_recorder(recorder)
            log("recording finalised")
        log("restoring the kiosk via tracer-dev-autostart.sh")
        restore_kiosk()

    size = os.path.getsize(args.out) if os.path.exists(args.out) else 0
    log(f"done: {args.out} ({size/1e6:.1f} MB)")
    return 0 if size else 1


if __name__ == "__main__":
    sys.exit(main())
