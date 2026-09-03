# Shared config for the dev scripts. Sourced, not executed.
#
# WHY NOTHING HERE HAS A DEFAULT
# ------------------------------
# A board's address belongs to the bench it sits on. Baking one into a tracked
# file is wrong twice over: every other clone of this repo gets a value that is
# useless to them at best and points at a stranger's machine at worst, and the
# author's network layout ships to whatever remote the repo is pushed to. The
# same habit is how credentials end up in git history — the mechanism that puts
# an IP in a script is the one that will eventually put a password there.
#
# So the address is ASKED FOR on first use and written to scripts/dev.env,
# which is gitignored. Resolution order, most explicit first:
#
#   1. --device user@host        (a script argument; also --key, --port)
#   2. an environment variable   (TRACER_DEVICE=user@host make dev)
#   3. scripts/dev.env           (gitignored, per-workstation)
#   4. an interactive prompt     (offers to save for next time)
#   5. non-interactive with none of the above: fail with instructions
#
# Steps 1 and 2 exist because a prompt is not always possible — CI, a cron
# job, a `make` invocation with stdin redirected. An explicit argument is the
# fallback in those cases; guessing never is.
#
# Secrets are never prompted for or stored here. SSH keys are referenced by
# PATH, and the board's sudo password is typed into sudo's own prompt on the
# board, never captured by these scripts.

# POSIX-clean on purpose: this file gets sourced by bash scripts, but people
# also run `sh scripts/dev-env.sh` to see what it resolves to. $BASH_SOURCE
# without a subscript yields element 0 under bash and is simply unset under
# dash, so this works in both — `${BASH_SOURCE[0]}` does NOT, because dash
# cannot even parse the array subscript ("Bad substitution").
# shellcheck disable=SC2128
_here="$(cd "$(dirname "${BASH_SOURCE:-$0}")" && pwd)"
_envfile="$_here/dev.env"

# Quote one argument for later `eval set --`. printf '%q' would be shorter but
# is a bashism; dash's printf has no %q and would emit the format verbatim.
_devq() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# ── arguments ────────────────────────────────────────────────────────
# Call as:  . "$(dirname "$0")/dev-env.sh" "$@"  then  eval set -- "$DEV_REST"
#
# The leftovers are handed back in DEV_REST rather than by rewriting "$@"
# directly: bash restores a sourced file's positional parameters when it
# returns, so a `set --` in here would be silently undone and the caller would
# still see --device in its own flag parsing.
DEV_REST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --device) [ -n "${2:-}" ] || { echo "--device needs a value" >&2; exit 1; }
                  TRACER_DEVICE="$2"; shift 2 ;;
        --device=*) TRACER_DEVICE="${1#*=}"; shift ;;
        --key)    [ -n "${2:-}" ] || { echo "--key needs a value" >&2; exit 1; }
                  TRACER_KEY="$2"; shift 2 ;;
        --key=*)  TRACER_KEY="${1#*=}"; shift ;;
        --port)   [ -n "${2:-}" ] || { echo "--port needs a value" >&2; exit 1; }
                  TRACER_PORT="$2"; shift 2 ;;
        --port=*) TRACER_PORT="${1#*=}"; shift ;;
        *)        DEV_REST="$DEV_REST $(_devq "$1")"; shift ;;
    esac
done

if [ -f "$_envfile" ]; then
    # An explicit environment variable beats the file, so save anything already
    # set and put it back afterwards.
    _pre_device="${TRACER_DEVICE:-}"
    _pre_key="${TRACER_KEY:-}"
    _pre_port="${TRACER_PORT:-}"
    # shellcheck disable=SC1090
    . "$_envfile"
    [ -n "$_pre_device" ] && TRACER_DEVICE="$_pre_device"
    [ -n "$_pre_key" ]    && TRACER_KEY="$_pre_key"
    [ -n "$_pre_port" ]   && TRACER_PORT="$_pre_port"
fi

# ── first run: ask ───────────────────────────────────────────────────
if [ -z "${TRACER_DEVICE:-}" ]; then
    if [ ! -t 0 ]; then
        cat >&2 <<EOF
No board configured, and no terminal to ask on.

Pass it explicitly:
    $(basename "${0:-dev-deploy.sh}") --device user@host

or set it in the environment:
    TRACER_DEVICE=user@host $(basename "${0:-dev-deploy.sh}")

or create scripts/dev.env from scripts/dev.env.example.
EOF
        exit 1
    fi

    echo "No board configured yet — scripts/dev.env does not exist."
    echo
    printf 'Board SSH destination (user@host): '
    read -r _in_device
    [ -n "$_in_device" ] || { echo "Nothing entered." >&2; exit 1; }

    printf 'SSH key [~/.ssh/id_ed25519]: '
    read -r _in_key
    [ -n "$_in_key" ] || _in_key="~/.ssh/id_ed25519"

    printf 'Save these to scripts/dev.env for next time? [Y/n] '
    read -r _in_save
    case "$_in_save" in
        [Nn]*) ;;
        *)
            # 0600: it holds no secret today, but it is per-user config for
            # reaching a machine and there is no reason for it to be readable
            # by anyone else on this workstation.
            (umask 077; cat > "$_envfile" <<EOF
# Local dev board config. GITIGNORED — do not commit, and do not add
# credentials here. See dev.env.example for the full list of settings.
TRACER_DEVICE=$_in_device
TRACER_KEY=$_in_key
EOF
            )
            echo "Wrote $_envfile (gitignored)."
            ;;
    esac
    echo
    TRACER_DEVICE="$_in_device"
    TRACER_KEY="$_in_key"
fi

DEVICE="$TRACER_DEVICE"
# eval so a leading ~ expands; the file is config, not a shell context.
eval KEY="${TRACER_KEY:-~/.ssh/id_ed25519}"
PORT="${TRACER_PORT:-8710}"
# A workstation with several keys in ssh-agent offers all of them, which trips
# the board's MaxAuthTries before the right key is reached. IdentitiesOnly is
# not optional here.
SSH_OPTS="-o IdentitiesOnly=yes -o ConnectTimeout=8 -i $KEY"

# Run directly (rather than sourced), report what was resolved and from where.
# This file is normally sourced, so executing it would otherwise set a few
# variables in a shell that immediately exits — no output, no error, nothing
# to say whether your --device was even understood.
#
# Keyed on $0 rather than $BASH_SOURCE so it works under dash too: when this
# file is sourced, $0 is the CALLING script's name; when it is executed, $0 is
# this file. $BASH_SOURCE would leave `sh dev-env.sh` silent.
case "$0" in
  */dev-env.sh|dev-env.sh)
    echo "device   $DEVICE"
    echo "key      $KEY"
    echo "port     $PORT"
    echo "config   $_envfile$([ -f "$_envfile" ] || echo ' (does not exist)')"
    echo
    echo "This file is meant to be sourced by the dev scripts, not run."
    echo "To use it:  make dev  |  make dev-provision  |  make dev-check"
    ;;
esac
