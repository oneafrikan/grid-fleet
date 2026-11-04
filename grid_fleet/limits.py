"""statusLine tap (wrapper around the user's original statusLine) + limits.json read.

Entry point used by cli.py: tap_main(config, args) -> int, where
args.original_command is the list of argv words after `tap --` ([] if none).
Reader used by `collect`: read_limits(config) -> dict or None.

What the tap does, once per statusLine refresh (so it must stay fast; spike 7.1
measured about 26 ms added at p95):

  1. Read stdin ONCE into memory (the statusLine payload).
  2. Capture the account's rate-limit slice into <state_dir>/limits.json. Any
     failure here is swallowed: the user's statusLine must never break because
     of us.
  3. Run the original statusLine command with the IDENTICAL stdin bytes and the
     full, unchanged environment, then pass its stdout and exit code through.

limits.json (schema 1), written 0600 via tmp + rename:

    {"schema": 1, "captured_at": <epoch s>,
     "five_hour": {"used_pct": 42.0, "resets_at": <epoch s>},
     "seven_day": {"used_pct": 18.0, "resets_at": <epoch s>}}

Privacy: only the fields above are ever copied out of the payload. The payload
also carries a working directory, session id, transcript path and model; none of
those is read into the file, so none can reach a snapshot.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from typing import List, Optional

from grid_fleet import config as cfg

LIMITS_FILE = "limits.json"
SCHEMA = 1
WINDOWS = ("five_hour", "seven_day")

# Exit code when the original command cannot be started at all (the shell's own
# "command not found / not executable" convention).
EXIT_ORIGINAL_UNAVAILABLE = 127


def limits_path(config) -> str:
    # A Config built from defaults (cli does this when the config file is
    # broken, so the statusLine survives) has no state_dir: resolve it from
    # the environment rather than writing into the current directory.
    state = getattr(config, "state_dir", "") or cfg.state_dir()
    return os.path.join(state, LIMITS_FILE)


# --- value checks ------------------------------------------------------------


def _is_number(value: object) -> bool:
    """A finite JSON number. bool is an int subclass, so exclude it explicitly."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _clean_window(raw: object, name_in: str, name_out: str) -> Optional[dict]:
    """Pick the two known fields out of one window; None if either is unusable.

    `name_in`/`name_out` differ only for used_pct (payload: used_percentage).
    Unknown keys are dropped here: this is where the slice-only rule is enforced.
    """
    if not isinstance(raw, dict):
        return None
    pct, resets = raw.get(name_in), raw.get("resets_at")
    if not _is_number(pct) or not _is_number(resets):
        return None
    return {name_out: pct, "resets_at": resets}


def _slice_from_payload(data: bytes) -> dict:
    """Extract {window: {"used_pct", "resets_at"}} from the raw payload bytes.

    Returns {} for anything unusable (not JSON, no rate_limits, empty, wrong
    types): the caller then leaves the file untouched.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return {}
    out = {}
    for name in WINDOWS:
        win = _clean_window(limits.get(name), "used_percentage", "used_pct")
        if win is not None:
            out[name] = win
    return out


# --- file read / merge / write -------------------------------------------------


def _read_file(path: str) -> Optional[dict]:
    """Parse limits.json into a clean dict, or None if absent/invalid.

    Re-validates and re-slices rather than trusting the file: a hand-edited or
    corrupt file must not put unknown fields (or junk) into a snapshot.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    # A future schema is not ours to interpret; a missing schema is tolerated.
    if "schema" in raw and raw["schema"] != SCHEMA:
        return None
    captured = raw.get("captured_at")
    if not _is_number(captured):
        return None
    out = {"captured_at": captured}
    for name in WINDOWS:
        win = _clean_window(raw.get(name), "used_pct", "used_pct")
        if win is not None:
            out[name] = win
    # A file with no window carries no information: same as "never captured".
    return out if len(out) > 1 else None


def read_limits(config) -> Optional[dict]:
    """The `limits` snapshot section, or None when never captured / unreadable."""
    return _read_file(limits_path(config))


def _merge(previous: Optional[dict], new: dict, now: int) -> dict:
    """Build the next file content (merge rule from the limits-capture spec).

    A window absent from the new payload keeps its previous stored value only
    while that value's resets_at is still in the future; otherwise it is dropped.

    Known limitation (spec has a single file-level captured_at, so this is
    accepted, not fixed here): captured_at is set to `now` on every write, so a
    carried-forward window looks as fresh as the one just captured, and the
    view's "as of <age>" for it understates its real age.
    """
    merged = {"schema": SCHEMA, "captured_at": now}
    for name in WINDOWS:
        if name in new:
            merged[name] = new[name]
        elif previous and name in previous and previous[name]["resets_at"] > now:
            merged[name] = previous[name]
    return merged


def _atomic_write(path: str, obj: dict) -> None:
    """tmp + rename in the same directory; mkstemp makes the temp file 0600.

    Deliberately not shared with sources_claude.atomic_write_json: that module is
    large and the tap runs on every statusLine refresh, so importing it would
    cost startup time on the hot path. Raises OSError.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def capture(config, data: bytes) -> bool:
    """Write the limits slice for one payload. True if the file was written.

    No usable rate_limits in the payload: the file is neither created nor
    modified. Raises OSError if the write fails (the tap catches it).

    Concurrency: the rename makes every reader see complete JSON. The
    read-merge-write is not locked, so two simultaneous taps are last-writer-wins;
    at worst a carried-forward window is dropped until the next payload brings it
    back (both windows normally arrive together, so this is rare).
    """
    new = _slice_from_payload(data)
    if not new:
        return False
    path = limits_path(config)
    now = cfg.now()
    _atomic_write(path, _merge(_read_file(path), new, now))
    return True


# --- the tap -------------------------------------------------------------------


def _run_original(command: List[str], data: bytes) -> int:
    """Run the original statusLine; pass its stdout through; return its exit code.

    No shell, no env override (full environment inherited), no timeout: the tap
    must be indistinguishable from the original. stderr is inherited, so it
    passes through untouched too.
    """
    try:
        proc = subprocess.run(command, input=data, stdout=subprocess.PIPE)
    except OSError as exc:
        # Cannot even start it. Say so on stderr (strerror only: no paths) and
        # mimic the shell's exit code. Nothing on stdout.
        sys.stderr.write("grid-fleet tap: cannot run original statusLine (%s)\n" % (exc.strerror or "error"))
        return EXIT_ORIGINAL_UNAVAILABLE
    try:
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write(proc.stdout)
            out.flush()
        else:  # pragma: no cover - stdout without a binary layer
            sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
            sys.stdout.flush()
    except OSError:
        pass  # reader went away; the exit code is still what matters
    # A negative returncode means "killed by signal N": report it the way a shell would.
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def tap_main(config, args) -> int:
    """`grid-fleet tap -- <original command...>`. Never fails because of the capture."""
    try:
        data = sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        data = b""

    # Capture first: it is a few ms of local work, and doing it before the
    # original keeps it independent of whatever the original does.
    try:
        capture(config, data)
    except Exception:
        # Any failure (unwritable dir, disk full, odd payload) is dropped on
        # purpose: the statusLine output and exit code must be unaffected.
        pass

    command = list(getattr(args, "original_command", None) or [])
    if not command:
        return 0  # no original configured: print nothing
    return _run_original(command, data)
