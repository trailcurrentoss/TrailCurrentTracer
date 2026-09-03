#!/bin/bash
# Start the dev-deployed Tracer when the board's desktop session comes up.
#
# THIS IS THE DEV-BOARD PATH, NOT THE PRODUCT PATH.
# ------------------------------------------------
# A flashed image starts Tracer from image/systemd/tracerd.service and
# tracer-ui.service — root-owned units, running as the device user out of
# /opt/tracer, with the kiosk under `cage`. That is the real boot story and
# this script does not replace or simulate it.
#
# A hand-provisioned dev board can use none of that:
#   * it runs a full desktop session (labwc) that already owns the screen,
#   * `cage` is not installed,
#   * the app lives unprivileged in ~/tracer-dev, not /opt/tracer,
#   * sudo needs a password, so nothing may require root,
#   * and the user session has graphical-session.target INACTIVE with
#     linger off, so a systemd --user unit would never fire at boot.
#
# XDG autostart is what that session actually honours, so this runs from
# ~/.config/autostart/tracer-dev.desktop. It is installed only when asked
# for (`dev-deploy.sh --autostart`) and removed by `--no-autostart`.
#
# Everything it touches is under $HOME. A reflash wipes the card, so none of
# this can survive into an image.

set -uo pipefail

DIR="$HOME/tracer-dev"
PORT="${TRACER_PORT:-8710}"
LOG="$DIR/autostart.log"

mkdir -p "$DIR"
exec >>"$LOG" 2>&1
echo "=== autostart $(date -Is) ==="

# The session exports these for its own children, but an autostart entry is
# launched early enough that WAYLAND_DISPLAY is not always set yet.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

# Wait for the compositor socket. Without this the kiosk races labwc and dies
# with a bare "cannot open display" on a cold boot, while working every time
# when launched by hand over SSH — the exact shape of a bug that looks
# intermittent and wastes an afternoon.
for _ in $(seq 1 30); do
    [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break
    sleep 1
done
[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] || {
    echo "no wayland socket after 30s — giving up"; exit 1; }

# ── daemon ───────────────────────────────────────────────────────────
# Filter on the process NAME, never a bare pattern: this script's own command
# line would otherwise match and it would kill itself. Same trap documented
# in dev-deploy.sh.
tracerd_pids() {
    for pid in $(pgrep -f "python3 -m tracerd" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        case "$(ps -p "$pid" -o comm= 2>/dev/null)" in python3*) echo "$pid" ;; esac
    done
}

healthy() { curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }

# Branch on HEALTH, never on "a process exists". An earlier version of this
# script asked tracerd_pids first and skipped startup when it saw anything,
# which broke the moment a daemon was still shutting down: the dying process
# matched, the script reported "already running", started nothing, and then
# sat out its whole health timeout. A PID proves something exists; only a
# health response proves it serves. Same rule as dev-deploy.sh.
if healthy; then
    echo "tracerd already serving on $PORT"
else
    # Sweep anything stale or mid-shutdown so the port is free. Filter on the
    # process NAME — a bare pattern kill would match this script's own line.
    for pid in $(tracerd_pids); do kill "$pid" 2>/dev/null; done
    sleep 1
    for pid in $(tracerd_pids); do kill -9 "$pid" 2>/dev/null; done
    sleep 0.5

    # /var/lib/tracer is root-owned and only exists on a flashed image, so the
    # unprivileged dev daemon keeps its state under $HOME or every settings
    # write fails with EACCES.
    export TRACER_STATE="$DIR/state"
    mkdir -p "$TRACER_STATE"
    cd "$DIR/tracerd" || { echo "no $DIR/tracerd — deploy first"; exit 1; }
    setsid python3 -m tracerd --port "$PORT" --ui-dir ../tracer-ui \
        >>"$DIR/tracerd.log" 2>&1 </dev/null &
    echo $! > "$DIR/tracerd.pid"
    echo "started tracerd pid $(cat "$DIR/tracerd.pid")"

    for _ in $(seq 1 20); do
        healthy && break
        sleep 0.5
    done
    healthy || { echo "tracerd never became healthy:"; tail -20 "$DIR/tracerd.log"; exit 1; }
    echo "tracerd healthy on $PORT"
fi

# ── kiosk ────────────────────────────────────────────────────────────
# --password-store=basic is required, not cosmetic. Chromium's default store
# is gnome-libsecret; with no unlocked keyring it raises a MODAL "Choose
# password for new keyring" dialog that covers the kiosk. The panel then shows
# the desktop while pgrep still reports chromium up.
pkill -x chromium 2>/dev/null
sleep 0.5
setsid chromium --kiosk --ozone-platform=wayland \
    --noerrdialogs --disable-infobars --hide-scrollbars \
    --disable-features=Translate,TranslateUI \
    --check-for-update-interval=31536000 --force-device-scale-factor=1 \
    --password-store=basic --use-mock-keychain \
    --user-data-dir="$DIR/chrome-profile" \
    "http://127.0.0.1:$PORT/" >>"$DIR/chromium.log" 2>&1 </dev/null &

sleep 5
pgrep -x chromium >/dev/null && echo "kiosk up" || {
    echo "kiosk FAILED:"; tail -15 "$DIR/chromium.log"; exit 1; }
