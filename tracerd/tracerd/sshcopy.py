"""Password- and key-driven ssh/scp helpers, no extra packages.

`sshpass` is not installed and is not worth adding to an offline image build.
OpenSSH has supported a non-interactive password path since 8.4 via
SSH_ASKPASS + SSH_ASKPASS_REQUIRE=force; the board runs OpenSSH 10, so that
is the mechanism used here.

Two requirements that are easy to get wrong:

  * The process must have NO controlling terminal, or ssh reads the password
    from the tty and ignores the askpass helper entirely. `start_new_session`
    gives us that (the equivalent of setsid).
  * The askpass helper is a file on disk holding the password. It is written
    0700 in a private temp dir and unlinked in a finally block, so the secret
    exists for the life of one call and never lands in the state directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

# Control sockets must live on a writable filesystem: the image ships a
# read-only rootfs, so $HOME may not be writable. This is the same state
# directory the settings store and SSH keys use.
_CONTROL_DIR = os.path.join(
    os.environ.get("TRACER_STATE", "/var/lib/tracer"), "ssh")
try:
    os.makedirs(_CONTROL_DIR, mode=0o700, exist_ok=True)
except OSError:
    # Fall back to the temp dir rather than refusing to talk to Headwaters at
    # all; multiplexing is an optimisation, not a requirement.
    _CONTROL_DIR = tempfile.gettempdir()

COMMON_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    # A workstation with several keys in ssh-agent offers all of them, which
    # exhausts the server's MaxAuthTries before auth can succeed. Scoping
    # identities is not optional when driving ssh programmatically.
    "-o", "IdentitiesOnly=yes",
    "-o", "NumberOfPasswordPrompts=1",
    # Connection multiplexing. The Headwaters monitor polls every few seconds,
    # and without this every poll pays a fresh TCP handshake plus a full key
    # exchange and auth — on a busy rig that is most of the poll interval, so
    # the display lags reality and the box gets pointless auth churn. With a
    # persistent master the first call sets up the session and later ones ride
    # it for a few milliseconds.
    #
    # The socket lives under the one writable partition (the rootfs is
    # read-only) and %C hashes host/port/user into a fixed-length name — the
    # literal form easily exceeds the ~104-byte sockaddr_un limit and fails
    # with a confusing "unix_listener: too long".
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_CONTROL_DIR}/cm-%C",
    "-o", "ControlPersist=60",
]


class SSHError(Exception):
    pass


async def _run(argv, password=None, timeout=45):
    """Run ssh/scp, optionally feeding a password via SSH_ASKPASS."""
    env = dict(os.environ)
    tmpdir = askpass = None
    try:
        if password is not None:
            tmpdir = tempfile.mkdtemp(prefix="tracer-ap-")
            os.chmod(tmpdir, 0o700)
            askpass = os.path.join(tmpdir, "askpass")
            with open(askpass, "w") as fh:
                # Nothing but the echo — no shell expansion of the secret.
                fh.write("#!/bin/sh\ncat <<'TRACER_EOF'\n%s\nTRACER_EOF\n" % password)
            os.chmod(askpass, 0o700)
            env["SSH_ASKPASS"] = askpass
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
        env["LC_ALL"] = "C"

        proc = await asyncio.create_subprocess_exec(
            *argv, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # No controlling terminal, or ssh prompts on the tty and the
            # askpass helper is never consulted.
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise SSHError("timed out")
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def friendly(stderr: str) -> str:
    """OpenSSH diagnostics are not written for a technician in a vehicle bay."""
    low = stderr.lower()
    if "permission denied" in low:
        return "wrong password or user"
    if "no route to host" in low or "network is unreachable" in low:
        return "no route to host"
    if "connection refused" in low:
        return "ssh refused — is sshd running on Headwaters?"
    if "could not resolve" in low or "name or service not known" in low:
        return "hostname does not resolve"
    if "timed out" in low or "timeout" in low:
        return "connection timed out"
    if "host key verification failed" in low:
        return "host key changed — refusing to connect"
    for line in stderr.strip().splitlines()[::-1]:
        line = line.strip()
        if line and not line.startswith("Warning: Permanently added"):
            return line
    return "ssh failed"


async def fetch_file(host, user, remote_path, *, key=None, password=None,
                     timeout=45) -> bytes:
    """scp a single file from the remote host and return its bytes."""
    tmp = tempfile.NamedTemporaryFile(prefix="tracer-scp-", delete=False)
    tmp.close()
    try:
        argv = ["scp", *COMMON_OPTS]
        if key:
            argv += ["-i", key]
        if password is not None and not key:
            argv += ["-o", "PreferredAuthentications=password",
                     "-o", "PubkeyAuthentication=no"]
        argv += [f"{user}@{host}:{remote_path}", tmp.name]

        rc, out, err = await _run(argv, password=password if not key else None,
                                  timeout=timeout)
        if rc != 0:
            if "No such file" in err or "not a regular file" in err:
                raise SSHError(f"{remote_path} not found on {host}")
            raise SSHError(friendly(err))
        with open(tmp.name, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


async def run(host, user, command: str, *, key=None, password=None,
              timeout=45) -> str:
    """Run one command on the remote host and return stdout.

    Read-only use only — this is how Tracer reads Headwaters' state without
    Headwaters growing an API for it.
    """
    argv = ["ssh", *COMMON_OPTS]
    if key:
        argv += ["-i", key]
    else:
        argv += ["-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no"]
    argv += [f"{user}@{host}", command]
    rc, out, err = await _run(argv, password=None if key else password, timeout=timeout)
    if rc != 0:
        raise SSHError(friendly(err))
    return out


async def put_file(host, user, local_path, remote_path, *, key=None,
                   password=None, timeout=600) -> None:
    """scp a file TO the remote host. Long timeout — deployment zips are big."""
    argv = ["scp", *COMMON_OPTS]
    if key:
        argv += ["-i", key]
    else:
        argv += ["-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no"]
    argv += [str(local_path), f"{user}@{host}:{remote_path}"]
    rc, out, err = await _run(argv, password=None if key else password, timeout=timeout)
    if rc != 0:
        raise SSHError(friendly(err))


async def run_sudo(host, user, command: str, password: str, *, key=None,
                   timeout=900):
    """Run a command that will call sudo internally.

    Headwaters' deploy.sh shells out to `sudo cp`, `sudo systemctl` and so on.
    sudo on that box wants a password, and there is no tty here, so prime the
    credential cache with `sudo -A -v` using an askpass helper first. Without
    this deploy.sh stalls forever on an invisible prompt.
    """
    helper = "$HOME/.tracer-askpass"
    setup = (
        "umask 077; "
        f"printf '#!/bin/sh\ncat <<\'E\'\n%s\nE\n' > {helper}; "
        f"chmod 700 {helper}; "
        f"SUDO_ASKPASS={helper} sudo -A -v; "
    ) % password
    cleanup = f"; rc=$?; rm -f {helper}; exit $rc"
    full = setup + f"SUDO_ASKPASS={helper} " + command + cleanup
    argv = ["ssh", *COMMON_OPTS]
    if key:
        argv += ["-i", key]
    else:
        argv += ["-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no"]
    argv += [f"{user}@{host}", full]
    rc, out, err = await _run(argv, password=None if key else password, timeout=timeout)
    return rc, out, err


async def install_key(host, user, pubkey: str, password: str, timeout=45) -> None:
    """Append our public key to the remote authorized_keys.

    Idempotent — greps first, so re-running repairs a broken file without
    accumulating duplicate entries.
    """
    remote = (
        "set -e; "
        "mkdir -p ~/.ssh; chmod 700 ~/.ssh; "
        "touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
        "grep -qF '%s' ~/.ssh/authorized_keys || echo '%s' >> ~/.ssh/authorized_keys"
        % (pubkey.strip(), pubkey.strip())
    )
    argv = ["ssh", *COMMON_OPTS,
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            f"{user}@{host}", remote]
    rc, out, err = await _run(argv, password=password, timeout=timeout)
    if rc != 0:
        raise SSHError(friendly(err))


async def ensure_keypair(path) -> str:
    """Create an ed25519 keypair if absent; return the public key."""
    pub = f"{path}.pub"
    if not os.path.isfile(path) or not os.path.isfile(pub):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for p in (path, pub):
            try:
                os.unlink(p)
            except OSError:
                pass
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-t", "ed25519", "-N", "", "-C", "tracer", "-f", path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise SSHError(f"ssh-keygen failed: {err.decode(errors='replace')}")
        os.chmod(path, 0o600)
    with open(pub) as fh:
        return fh.read().strip()
