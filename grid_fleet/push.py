"""ssh / local delivery of the snapshot to the hub inbox, and the `push` command.

Owner: Group 3 unit (grid-backend-dev).
Entry point used by cli.py: push_main(config, args) -> int.

What `push` does (spec: push-transport; design.md "Hub"):

  1. `hub` must be set, else exit EXIT_CONFIG naming the key. Nothing is
     collected, nothing is sent. A hub alias that could be read by ssh as an
     option (leading "-") or that contains whitespace/control characters is
     refused the same way (option injection).
  2. Get a FRESH snapshot: the stored snapshot.json if its collected_at is at
     most interval_s old, otherwise re-collect first. A fresh inbox mtime must
     never carry old data (design.md "Staleness").
  3. Deliver the snapshot bytes:
       hub == "local" -> atomic write into the local inbox, no ssh.
       otherwise      -> `ssh -o BatchMode=yes -o ConnectTimeout=10
                         -o ClearAllForwardings=yes -- <alias>
                         "sh -c '<upload script>'"` with the bytes on stdin.
  4. Failure (ssh non-zero, timeout, cannot spawn): exit EXIT_FAILED, keep the
     local snapshot, ONE short stderr line, no retry loop. The next timer tick
     is the retry.

Credentials: this module never reads ~/.ssh or any key/agent state. ssh finds
the user's own config, keys and agent by itself; we only pass an alias.

Public API:

    push_main(config, args) -> int
    deliver_local(config, data) -> None        atomic write into the local inbox
    deliver_ssh(config, data) -> None          raises PushError on any failure
    remote_command(host) -> str                the single `sh -c '...'` argument
    validate_hub(hub) -> Optional[str]         None if usable, else a reason
    PushError                                  user-facing failure (message is one line)
    EXIT_CONFIG, EXIT_FAILED, SSH_TIMEOUT_S
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

from grid_fleet import config as cfg
from grid_fleet import hub, snapshot

# Same value as cli.EXIT_CONFIG (missing/bad `hub` is a config problem). Not
# imported from cli.py: cli lazily imports this module, so importing it back
# would be a needless cycle.
EXIT_CONFIG = 3
# Snapshot could not be produced, or delivery failed.
EXIT_FAILED = 1

# Backstop for the whole ssh run. ConnectTimeout=10 only bounds the TCP/ssh
# handshake; a transfer that stalls afterwards would otherwise block a timer
# tick forever. Module-level so tests can shrink it.
SSH_TIMEOUT_S = 60

# `hub` value meaning "this machine is the hub" (design.md Decided).
LOCAL_HUB = "local"

# The design.md upload script, with the host as the placeholder @HOST@. A plain
# "H" placeholder (as written in the design) cannot be substituted blindly
# because "H" also occurs in HOME / XDG_STATE_HOME. The script contains no
# single quote, so it can sit inside one single-quoted `sh -c` argument and the
# hub's login shell is irrelevant.
#
#   umask 077         -> files are 0600
#   t=.H.json.$$.tmp  -> the in-flight upload lives in a dot-file readers ignore. The
#                        name carries the shell PID ($$) so it is UNIQUE per push: a
#                        manual push overlapping a timer push used to share one fixed
#                        tmp, the second `cat >` truncated it while the first kept
#                        writing at its old offset, and the rename published a file
#                        that was part NUL bytes yet still ended in a newline.
#   trap c EXIT       -> removes the tmp on every exit path (failed check, cat error,
#                        completed mv: then it is already gone and `rm -f` is a no-op).
#                        A function, not `trap 'rm ...'`, because the script must hold
#                        no single quote (it sits inside one single-quoted argument).
#                        SIGKILL / a killed session skips the trap and leaves the dot-
#                        file behind; readers ignore it.
#   [ ... -eq 1 ]     -> completeness check: only rename if the tmp file ends in
#                        "\n". Snapshots are always written as JSON + "\n"
#                        (sources_claude.atomic_write_json). If the client drops
#                        mid-stream, sshd closes the remote stdin, `cat` sees a
#                        clean EOF and exits 0, so without this check `mv` would
#                        replace the good <host>.json with a TRUNCATED file.
#                        `tail -c 1 | wc -l` is 1 only when the last byte is a
#                        newline (0 for an empty file). `wc -l` pads its number
#                        with spaces on macOS/BSD; `[ -eq ]` compares as integers
#                        so the padding is harmless. Known residual limit: a
#                        stream cut exactly after an inner newline passes. The
#                        check catches truncation, not every short stream.
#   mv                -> atomic rename over the previous <host>.json
UPLOAD_SCRIPT = (
    'umask 077; d="${XDG_STATE_HOME:-$HOME/.local/state}/grid-fleet/inbox"; '
    't="$d/.@HOST@.json.$$.tmp"; c() { rm -f "$t"; }; trap c EXIT; '
    'mkdir -p "$d" && cat > "$t" && '
    '[ "$(tail -c 1 "$t" | wc -l)" -eq 1 ] && '
    'mv "$t" "$d/@HOST@.json"'
)

# The conservative alias charset: letters, digits and . _ @ : - . That covers a config
# alias, `user@host`, a dotted host name, an IPv4 address and an unbracketed IPv6 one;
# it excludes whitespace, control characters, quotes, `%`, `/`, `$`, `;` and every
# other shell or ssh-token metacharacter (ProxyCommand-style `%h` expansion, etc.).
# Matched with fullmatch: a `$` anchor would let a trailing "\n" through.
_ALIAS_CHARS = re.compile(r"[A-Za-z0-9._@:-]+")


class PushError(Exception):
    """Delivery failed. str(exc) is a single line (reason class + exit code only), safe to print."""


def validate_hub(hub_value: object) -> Optional[str]:
    """Return None if `hub_value` is a usable ssh alias (or "local"), else the reason.

    The alias is passed to ssh as a bare argv word. A leading "-" would be parsed by
    ssh as an option (e.g. -oProxyCommand=...); deliver_ssh also puts `--` before it,
    but the check stays so the failure is a clear config error. The rest of the value
    is held to a conservative charset (_ALIAS_CHARS) so no metacharacter can reach ssh
    or a user's `%h`-expanding ssh_config entry.
    """
    if not isinstance(hub_value, str) or not hub_value:
        return "must be a non-empty ssh host alias or \"local\""
    if hub_value.startswith("-"):
        return "must not start with \"-\" (ssh would read it as an option)"
    if not _ALIAS_CHARS.fullmatch(hub_value):
        return "may only contain letters, digits and . _ @ : -"
    return None


def remote_command(host: str) -> str:
    """The ONE argument handed to ssh as the remote command.

    `host` is forced through the sanitiser again here, so even a caller that
    forgot cannot put a quote, space, `;` or `/` into the command. The safe
    charset (A-Za-z0-9._-, no leading dot) is what makes inline substitution
    into the script acceptable.
    """
    safe = cfg.sanitize_host_name(host)
    return "sh -c '" + UPLOAD_SCRIPT.replace("@HOST@", safe) + "'"


# --- snapshot freshness -----------------------------------------------------


def _read_stored(config) -> Optional[bytes]:
    """The stored snapshot's bytes if it exists, parses and is fresh; else None.

    Fresh means 0 <= age <= interval_s by the one clock. A snapshot dated in the
    future (clock jump) is not trusted either: re-collecting is cheap and safe.
    """
    try:
        with open(snapshot.snapshot_path(config), "rb") as fh:
            data = fh.read()
        snap = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):  # missing, unreadable, not JSON/UTF-8
        return None
    if not isinstance(snap, dict):
        return None
    collected = snap.get("collected_at")
    if isinstance(collected, bool) or not isinstance(collected, int):
        return None
    age = cfg.now() - collected
    if age < 0 or age > config.interval_s:
        return None
    # write_snapshot always ends the file with "\n"; the hub-side completeness
    # check relies on that. A file without it (hand-edited, truncated) is not
    # trusted: re-collect rather than send bytes the hub would refuse.
    if not data.endswith(b"\n"):
        return None
    return data


def fresh_snapshot_bytes(config) -> bytes:
    """Bytes of a snapshot no older than interval_s, collecting one if needed.

    Raises OSError if a re-collected snapshot cannot be written.
    """
    stored = _read_stored(config)
    if stored is not None:
        return stored
    snapshot.collect_and_write(config)
    # Deliver exactly what is on disk, so hub copy == local snapshot.json.
    with open(snapshot.snapshot_path(config), "rb") as fh:
        return fh.read()


# --- delivery ---------------------------------------------------------------


def deliver_local(config, data: bytes) -> None:
    """Write `data` as <state_dir>/inbox/<host>.json: unique tmp dot-file + atomic rename.

    Mirrors the remote script (owner-only, dot-prefixed tmp, rename) so a reader never
    sees a partial file. The tmp name is unique per call (mkstemp: O_EXCL, so it never
    follows a pre-planted symlink, and mode 0600), so two overlapping deliveries cannot
    truncate each other's file. The tmp is removed on any failure. Raises OSError.
    """
    host = cfg.sanitize_host_name(config.host_name)
    final = hub.inbox_path(config.state_dir, host)
    inbox = os.path.dirname(final)
    os.makedirs(inbox, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + host + ".json.", suffix=".tmp", dir=inbox)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def deliver_ssh(config, data: bytes) -> None:
    """Run ssh once with `data` on stdin. Raises PushError; never retries."""
    argv = [
        "ssh",  # resolved via PATH; tests put a fake first
        "-o", "BatchMode=yes",  # never prompt (passphrase, host key, password)
        "-o", "ConnectTimeout=10",  # never hang on an unreachable hub
        # One-shot upload: never open port/agent/X11 forwards a user's ssh config asks for.
        "-o", "ClearAllForwardings=yes",
        "--",  # end of options: the alias can never be parsed as one, whatever it holds
        config.hub,
        remote_command(config.host_name),
    ]
    try:
        proc = subprocess.run(
            argv,  # list + no shell: the alias and command are single argv words
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=SSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PushError("push failed (timeout after %ds); local snapshot kept" % SSH_TIMEOUT_S)
    except OSError as exc:
        # e.g. ssh not on PATH. strerror only: str(exc) can carry paths.
        raise PushError("push failed (cannot run ssh: %s); local snapshot kept" % (exc.strerror or "error"))
    if proc.returncode != 0:
        # Reason class + exit code only. ssh's own stderr is deliberately NOT
        # echoed: it can carry host names, addresses and key paths, and this
        # line lands in timer logs.
        raise PushError("push failed (ssh exit %d); local snapshot kept" % proc.returncode)


# --- command ----------------------------------------------------------------


def _fail(message: str, code: int) -> int:
    sys.stderr.write("grid-fleet push: %s\n" % message)
    return code


def push_main(config, args) -> int:
    """`grid-fleet push`. 0 delivered; EXIT_CONFIG bad/missing hub; EXIT_FAILED otherwise."""
    # Config checks come first: with no usable hub we must send AND collect nothing.
    if config.hub is None:
        return _fail("config key 'hub' is missing (set it to an ssh host alias, or \"local\")", EXIT_CONFIG)
    problem = validate_hub(config.hub)
    if problem is not None:
        return _fail("config key 'hub' %s" % problem, EXIT_CONFIG)

    try:
        data = fresh_snapshot_bytes(config)
    except OSError as exc:
        return _fail("cannot prepare snapshot (%s)" % (exc.strerror or "error"), EXIT_FAILED)

    try:
        if config.hub == LOCAL_HUB:
            deliver_local(config, data)
        else:
            deliver_ssh(config, data)
    except PushError as exc:
        return _fail(str(exc), EXIT_FAILED)
    except OSError as exc:
        return _fail("cannot write local inbox (%s)" % (exc.strerror or "error"), EXIT_FAILED)

    sys.stdout.write("push: delivered (%s)\n" % ("local" if config.hub == LOCAL_HUB else "ssh"))
    return 0
