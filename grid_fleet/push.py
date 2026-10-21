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
       otherwise      -> `ssh -o BatchMode=yes -o ConnectTimeout=10 <alias>
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
#   cat > .H.json.tmp -> the in-flight upload lives in a dot-file readers ignore
#   mv                -> atomic rename over the previous <host>.json
UPLOAD_SCRIPT = (
    'umask 077; d="${XDG_STATE_HOME:-$HOME/.local/state}/grid-fleet/inbox"; '
    'mkdir -p "$d" && cat > "$d/.@HOST@.json.tmp" && mv "$d/.@HOST@.json.tmp" "$d/@HOST@.json"'
)

# Whitespace or ASCII control chars (incl. DEL) anywhere in the alias.
_BAD_ALIAS_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
# How much of ssh's own first stderr line we echo into our one-line log.
_REASON_MAX = 80


class PushError(Exception):
    """Delivery failed. str(exc) is a single sanitised line, safe to print."""


def validate_hub(hub_value: object) -> Optional[str]:
    """Return None if `hub_value` is a usable ssh alias (or "local"), else the reason.

    The alias is passed to ssh as a bare argv word, so a leading "-" would be
    parsed by ssh as an option (e.g. -oProxyCommand=...). Whitespace/control
    characters have no place in a host alias either.
    """
    if not isinstance(hub_value, str) or not hub_value:
        return "must be a non-empty ssh host alias or \"local\""
    if hub_value.startswith("-"):
        return "must not start with \"-\" (ssh would read it as an option)"
    if _BAD_ALIAS_CHARS.search(hub_value):
        return "must not contain whitespace or control characters"
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
    """Write `data` as <state_dir>/inbox/<host>.json: tmp dot-file + atomic rename.

    Mirrors the remote script (umask 077, `.<host>.json.tmp`, mv) so a reader
    never sees a partial file. Raises OSError.
    """
    host = cfg.sanitize_host_name(config.host_name)
    final = hub.inbox_path(config.state_dir, host)
    tmp = hub.inbox_tmp_path(config.state_dir, host)
    os.makedirs(os.path.dirname(final), mode=0o700, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)  # in case the tmp pre-existed with other bits
            fh.write(data)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _short_reason(stderr: bytes) -> str:
    """First line of ssh's stderr reduced to printable ASCII, capped.

    ssh's message is the only clue why a timer-driven push failed ("Permission
    denied (publickey)"), so a short version is kept; control characters are
    stripped so it cannot forge extra log lines.
    """
    text = stderr.decode("utf-8", "replace")
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.sub(r"[^\x20-\x7e]", "?", first)
    return first[:_REASON_MAX]


def deliver_ssh(config, data: bytes) -> None:
    """Run ssh once with `data` on stdin. Raises PushError; never retries."""
    argv = [
        "ssh",  # resolved via PATH; tests put a fake first
        "-o", "BatchMode=yes",  # never prompt (passphrase, host key, password)
        "-o", "ConnectTimeout=10",  # never hang on an unreachable hub
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
        why = _short_reason(proc.stderr)
        raise PushError(
            "push failed (ssh exit %d%s); local snapshot kept"
            % (proc.returncode, ": " + why if why else "")
        )


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
