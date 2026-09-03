#!/bin/bash
# Push tracerd + the GUI to the live board and restart it. No image rebuild.
#
# The GUI is pure JavaScript ES modules with no build step, so this is a plain
# file copy — edit, run this, refresh. Seconds, not the 30-45 minutes an image
# build takes.
#
# Usage:
#   ./scripts/dev-deploy.sh            # deploy and restart tracerd
#   ./scripts/dev-deploy.sh --mock     # ...in mock mode (no hardware needed)
#   ./scripts/dev-deploy.sh --kiosk    # ...and show it on the board's panel
#   ./scripts/dev-deploy.sh --stop     # stop the dev daemon (and kiosk)
#   ./scripts/dev-deploy.sh --logs     # tail its output
#   ./scripts/dev-deploy.sh --autostart     # also start it at boot
#   ./scripts/dev-deploy.sh --no-autostart  # stop starting it at boot
#
# The board address is asked for on first use and saved to scripts/dev.env
# (gitignored). Override per run with --device user@host, or TRACER_DEVICE.
#
# Runs tracerd in a detached session under ~/tracer-dev on the board.
# It does not install a systemd unit and does not touch the image — a dev
# deploy must never leave state behind that a later flash would inherit.

set -euo pipefail

# Board address, key and port. No default is baked in — see dev-env.sh. It
# consumes --device/--key/--port and hands the rest back in DEV_REST.
# shellcheck source=scripts/dev-env.sh
. "$(dirname "$0")/dev-env.sh" "$@"
eval set -- "$DEV_REST"
# Under $HOME, not /opt: sudo on the board requires a password, and a dev
# loop must never need one. Keeps deploys entirely unprivileged.
REMOTE_DIR="tracer-dev"   # relative to the remote $HOME

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE=""

KIOSK=""
AUTOSTART=""
case "${1:-}" in
  --mock)  MODE="--mock" ;;
  --kiosk) KIOSK="yes" ;;
  # Deploy as normal, then leave an XDG autostart entry behind so the board
  # comes up running Tracer. Deliberately opt-in and separate from --kiosk:
  # a plain `make dev` must never change what the board does at boot.
  --autostart) KIOSK="yes"; AUTOSTART="yes" ;;
  --no-autostart)
      ssh $SSH_OPTS "$DEVICE" \
        "rm -f \$HOME/.config/autostart/tracer-dev.desktop \
                \$HOME/$REMOTE_DIR/tracer-dev-autostart.sh"
      echo "autostart removed — the board will boot to the plain desktop"
      exit 0 ;;
  --stop)
      # Kill by recorded PID, never by pattern. Pattern matching here is a
      # trap: the command line also carries "tracerd.log", so even the
      # bracket trick still self-matches.
      ssh $SSH_OPTS "$DEVICE" bash -s <<'SEOF'
for pid in $(pgrep -f "python3 -m tracerd" 2>/dev/null); do
    case "$(ps -p "$pid" -o comm= 2>/dev/null)" in python3*) kill "$pid" 2>/dev/null ;; esac
done
rm -f "$HOME/tracer-dev/tracerd.pid"
pkill -x chromium 2>/dev/null && echo "kiosk closed" || true
echo stopped
SEOF
      exit 0 ;;
  --logs)
      ssh $SSH_OPTS "$DEVICE" "tail -f \$HOME/$REMOTE_DIR/tracerd.log"
      exit 0 ;;
  --shot)
      ssh $SSH_OPTS "$DEVICE" \
        "XDG_RUNTIME_DIR=/run/user/\$(id -u) WAYLAND_DISPLAY=wayland-0 \
         grim \$HOME/$REMOTE_DIR/screen.png" || {
          echo "grim failed — is the labwc session running?" >&2; exit 1; }
      scp $SSH_OPTS "$DEVICE:$REMOTE_DIR/screen.png" ./panel.png
      echo "wrote ./panel.png"
      exit 0 ;;
  "") ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

echo "→ $DEVICE:$REMOTE_DIR"

ssh $SSH_OPTS "$DEVICE" "mkdir -p $REMOTE_DIR"

if command -v rsync >/dev/null && ssh $SSH_OPTS "$DEVICE" 'command -v rsync >/dev/null'; then
    rsync -az --delete \
        -e "ssh $SSH_OPTS" \
        --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'node_modules' --exclude '.gitignore' \
        "$ROOT/tracerd/" "$DEVICE:$REMOTE_DIR/tracerd/"
    rsync -az --delete \
        -e "ssh $SSH_OPTS" \
        --exclude 'node_modules' --exclude '.gitignore' \
        "$ROOT/tracer-ui/" "$DEVICE:$REMOTE_DIR/tracer-ui/"
else
    # rsync absent on one end — fall back to tar over ssh. Slower but works
    # on a stock image with no extra packages.
    echo "  (rsync unavailable, using tar)"
    tar -C "$ROOT" -cz \
        --exclude '__pycache__' --exclude '*.pyc' --exclude 'node_modules' \
        tracerd tracer-ui \
      | ssh $SSH_OPTS "$DEVICE" "rm -rf $REMOTE_DIR/tracerd $REMOTE_DIR/tracer-ui && tar -C $REMOTE_DIR -xz"
fi

ssh $SSH_OPTS "$DEVICE" bash -s <<EOF
set -e
PIDFILE="\$HOME/$REMOTE_DIR/tracerd.pid"
[ -f "\$PIDFILE" ] && kill "\$(cat "\$PIDFILE")" 2>/dev/null || true
# Sweep strays. The pidfile only tracks the LAST start; a failed or raced
# deploy leaves an older daemon alive, and because it keeps reading
# /dev/input/event0 it goes on throwing errors from stale code while the new
# one serves the port — which reads exactly like "my fix did not deploy".
#
# Do NOT pattern-kill here. This shell's own command line contains the
# pattern, so pkill -f kills itself before reaching the daemons — and the
# [p] bracket trick does not save it, because the same line also carries the
# unbracketed form. Filter on the process NAME instead, which a shell can
# never match.
tracerd_pids() {
    for pid in \$(pgrep -f "python3 -m tracerd" 2>/dev/null); do
        [ "\$pid" = "\$\$" ] && continue
        case "\$(ps -p "\$pid" -o comm= 2>/dev/null)" in
            python3*) echo "\$pid" ;;
        esac
    done
}
for pid in \$(tracerd_pids); do kill "\$pid" 2>/dev/null || true; done
sleep 0.8
for pid in \$(tracerd_pids); do kill -9 "\$pid" 2>/dev/null || true; done
sleep 0.3
# Log path must be absolute — we cd into the package dir below, which would
# otherwise resolve the relative path against the wrong directory.
LOG="\$HOME/$REMOTE_DIR/tracerd.log"
# /var/lib/tracer only exists on a flashed image and is root-owned. A dev
# deploy is unprivileged, so point the settings store somewhere writable —
# otherwise every settings write fails with EACCES.
export TRACER_STATE="\$HOME/$REMOTE_DIR/state"
mkdir -p "\$TRACER_STATE"
cd "\$HOME/$REMOTE_DIR/tracerd"
setsid python3 -m tracerd $MODE --port $PORT --ui-dir ../tracer-ui \
    > "\$LOG" 2>&1 < /dev/null &
echo \$! > "\$PIDFILE"

# Verify by asking the daemon, not by looking for a process. A PID proves
# something started; only a health response proves it is actually serving.
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    if curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        n=\$(tracerd_pids | wc -l)
        if [ "\$n" -gt 1 ]; then
            echo "WARNING: \$n tracerd processes running — a stale daemon will" >&2
            echo "         serve old code and race for /dev/input." >&2
            exit 1
        fi
        echo "tracerd serving on port $PORT ${MODE} (\$n process)"
        exit 0
    fi
done
echo "FAILED TO START (no health response after 5s):" >&2
tail -20 "\$LOG" >&2
exit 1
EOF

echo
echo "Health:"
ssh $SSH_OPTS "$DEVICE" "curl -s http://127.0.0.1:$PORT/health" || true
echo

# If a kiosk is already showing, it is running the OLD JavaScript — the GUI
# is served from disk and the browser never re-fetches on its own. Leaving it
# stale makes a deployed fix look like it did not work, which has cost real
# debugging time. Restart it whenever it is up.
if [ -z "$KIOSK" ]; then
    if ssh $SSH_OPTS "$DEVICE" 'pgrep -x chromium >/dev/null'; then
        echo "kiosk is running — restarting it to pick up the new UI"
        KIOSK="yes"
    fi
fi

if [ -n "$KIOSK" ]; then
    echo
    echo "Launching kiosk on the board's panel..."
    # The board runs a Wayland session on seat0. Launching a GUI app over SSH
    # needs both of these exported explicitly — an ssh session inherits
    # neither, and chromium fails with a bare "cannot open display".
    #
    # --password-store=basic is not optional on a desktop-flavoured Pi OS.
    # Chromium's default store is gnome-libsecret, and with no unlocked
    # keyring it pops a MODAL "Choose password for new keyring" dialog that
    # sits on top of the kiosk. The panel then shows the desktop, the log
    # stays empty, and pgrep still reports chromium up — so it reads exactly
    # like "the UI did not deploy" when the UI is fine and merely covered.
    ssh $SSH_OPTS "$DEVICE" bash -s <<KEOF
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
export WAYLAND_DISPLAY=\${WAYLAND_DISPLAY:-wayland-0}
pkill -x chromium 2>/dev/null || true
sleep 0.5
setsid chromium --kiosk --ozone-platform=wayland \
  --noerrdialogs --disable-infobars --hide-scrollbars \
  --disable-features=Translate,TranslateUI \
  --check-for-update-interval=31536000 --force-device-scale-factor=1 \
  --password-store=basic --use-mock-keychain \
  --user-data-dir=\$HOME/$REMOTE_DIR/chrome-profile \
  "http://127.0.0.1:$PORT/" > \$HOME/$REMOTE_DIR/chromium.log 2>&1 < /dev/null &
sleep 5
pgrep -x chromium >/dev/null && echo "kiosk up on the panel" \
  || { echo "kiosk FAILED:" >&2; tail -15 \$HOME/$REMOTE_DIR/chromium.log >&2; exit 1; }
KEOF
else
    echo
    echo "Show it on the board's panel:   ./scripts/dev-deploy.sh --kiosk"
    echo
    echo "Or view from here over a tunnel:"
    echo "  ssh $SSH_OPTS -L $PORT:127.0.0.1:$PORT $DEVICE -N &"
    echo "  xdg-open http://127.0.0.1:$PORT/?dev"
fi

# ── autostart ────────────────────────────────────────────────────────
# Installed only on --autostart. XDG autostart rather than a systemd --user
# unit because that is what this board can actually honour: its user session
# has graphical-session.target inactive and linger off, so a user unit would
# be enabled, look correct in `systemctl --user is-enabled`, and never run.
# See scripts/tracer-dev-autostart.sh for the full reasoning.
if [ -n "$AUTOSTART" ]; then
    echo
    echo "Installing autostart..."
    scp -q $SSH_OPTS "$ROOT/scripts/tracer-dev-autostart.sh" \
        "$DEVICE:$REMOTE_DIR/tracer-dev-autostart.sh"
    ssh $SSH_OPTS "$DEVICE" bash -s <<AEOF
set -e
chmod +x "\$HOME/$REMOTE_DIR/tracer-dev-autostart.sh"
mkdir -p "\$HOME/.config/autostart"
cat > "\$HOME/.config/autostart/tracer-dev.desktop" <<DEOF
[Desktop Entry]
Type=Application
Name=Tracer (dev deploy)
Comment=Starts the dev-deployed tracerd and kiosk. Installed by dev-deploy.sh --autostart.
Exec=$([ -n "$PORT" ] && echo "env TRACER_PORT=$PORT ")\$HOME/$REMOTE_DIR/tracer-dev-autostart.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
DEOF
echo "installed ~/.config/autostart/tracer-dev.desktop"
AEOF
    echo "Tracer will start on the next boot. Log: ~/$REMOTE_DIR/autostart.log"
    echo "Undo with:  ./scripts/dev-deploy.sh --no-autostart"
fi

# Grab what is actually on the panel — proof, not assumption. Needs grim,
# which is present in the board's labwc session.
echo
echo "Screenshot the panel with:"
echo "  ./scripts/dev-deploy.sh --shot   (writes ./panel.png here)"
