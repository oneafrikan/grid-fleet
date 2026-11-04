"""statusLine tap (wrapper around the user's original statusLine) + limits.json read.

Entry points:
  tap_fast(argv) -> int or None   called by bin/grid-fleet BEFORE cli/argparse is imported
  tap_main(config, args) -> int   called by cli.py (same behaviour; args.original_command
                                  is the list of argv words after `tap --`, [] if none)
Reader used by `collect`: read_limits(config) -> dict or None.

What the tap does, once per statusLine refresh (so it must stay fast: the spec bound is
< 50 ms added at p95; see docs/spikes.md for measured numbers):

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

Startup cost is the dominant cost of the tap (it is a fresh interpreter every refresh),
so this module deliberately imports only json/math/os/sys, the builtin _signal and the
light grid_fleet.paths. NOT subprocess (about 6 ms), signal, typing, tempfile (pulls in
shutil, random, bz2, lzma), argparse, dataclasses or grid_fleet.config.
tests/test_limits.py asserts that none of those get loaded.
"""

import json
import math
import os
import sys

import _signal  # builtin: raw signal numbers without signal.py's enum import cost

from grid_fleet import paths

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
    state = getattr(config, "state_dir", "") or paths.state_dir()
    return os.path.join(state, LIMITS_FILE)


# --- value checks ------------------------------------------------------------


def _is_number(value: object) -> bool:
    """A finite JSON number. bool is an int subclass, so exclude it explicitly.

    An int too big for a float (about 1e308 and up) makes math.isfinite raise
    OverflowError; such a value is hostile or corrupt, so it is "not a number"
    rather than an exception that would drop the whole capture / crash collect.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _clean_window(raw: object, name_in: str, name_out: str) -> "Optional[dict]":
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


def _read_file(path: str) -> "Optional[dict]":
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


def read_limits(config) -> "Optional[dict]":
    """The `limits` snapshot section, or None when never captured / unreadable."""
    return _read_file(limits_path(config))


def _merge(previous: "Optional[dict]", new: dict, now: int) -> dict:
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
    """tmp + rename in the same directory; the temp file is created 0600 and O_EXCL.

    Deliberately not shared with sources_claude.atomic_write_json (large module) nor
    built on tempfile.mkstemp (pulls in shutil/random/bz2/lzma): the tap runs on every
    statusLine refresh, so importing either would cost startup time on the hot path.
    The name is pid + 48 random bits, so concurrent taps never share a temp file.
    Raises OSError.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = os.path.join(directory, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), os.urandom(6).hex()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
    now = paths.now()
    _atomic_write(path, _merge(_read_file(path), new, now))
    return True


# --- the tap -------------------------------------------------------------------


def _run_original(command: "List[str]", data: bytes) -> int:
    """Run the original statusLine with `data` on its stdin; return its exit code.

    No shell, no env override (full environment inherited), no timeout. Its stdout and
    stderr are INHERITED, not captured: its bytes reach our caller untouched and in order,
    which is as close to "indistinguishable from the original" as it gets (and works the
    same when our own stdout is closed).

    Spawned with os.posix_spawnp instead of subprocess: importing subprocess costs about
    6 ms of start-up on every statusLine refresh (see the module docstring). Behaviour
    kept equal to subprocess.run(command, input=data):
      - SIGPIPE/SIGXFSZ are reset to default in the child (Python ignores SIGPIPE, and an
        ignored signal would be inherited across exec, changing how pipelines in the
        original behave).
      - the stdin pipe is created non-inheritable, so only fd 0 of the child refers to it.
    """
    program = command[0]
    read_end, write_end = os.pipe()
    try:
        actions = []
        if read_end == 0:
            # Our own stdin was closed so the pipe landed on fd 0: dup2(0, 0) would leave
            # it close-on-exec, so just make it inheritable.
            os.set_inheritable(0, True)
        else:
            actions.append((os.POSIX_SPAWN_DUP2, read_end, 0))
        try:
            pid = os.posix_spawnp(program, command, os.environ, file_actions=actions,
                                  setsigdef=(_signal.SIGPIPE, _signal.SIGXFSZ))
        except OSError as exc:
            # Cannot even start it. Say so on stderr (strerror only: no paths) and mimic
            # the shell's exit code. Nothing on stdout. stderr may itself be closed
            # (sys.stderr is None): then there is nobody to tell.
            try:
                sys.stderr.write("grid-fleet tap: cannot run original statusLine (%s)\n" % (exc.strerror or "error"))
            except (AttributeError, OSError, ValueError):
                pass
            return EXIT_ORIGINAL_UNAVAILABLE
        os.close(read_end)
        read_end = -1
        try:
            # Feed the identical bytes. The child may exit without reading them all
            # (BrokenPipeError): that is its right, same as with subprocess.
            view = memoryview(data)
            while view:
                view = view[os.write(write_end, view):]
        except OSError:
            pass
        os.close(write_end)
        write_end = -1
        _pid, status = os.waitpid(pid, 0)
    finally:
        for fd in (read_end, write_end):
            if fd >= 0:
                os.close(fd)
    # A negative code means "killed by signal N": report it the way a shell would.
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def _tap(config, command: "List[str]") -> int:
    """The tap proper. `config` may be None (fast path): the state dir then comes from
    the environment, exactly as for a Config built from defaults."""
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

    if not command:
        return 0  # no original configured: print nothing
    return _run_original(command, data)


def tap_main(config, args) -> int:
    """`grid-fleet tap -- <original command...>`, via cli.py. Never fails because of the capture."""
    return _tap(config, list(getattr(args, "original_command", None) or []))


def tap_fast(argv: "List[str]") -> "Optional[int]":
    """Fast path for bin/grid-fleet: run the tap without importing cli/argparse/config.

    Handles exactly the forms `tap` and `tap -- <original command...>` and returns the
    exit code; for anything else (flags before `--`, `-h`, stray words) returns None so the
    caller falls through to cli.main, which reports usage errors as before.

    Why config is not loaded: the tap only ever needed the state dir, which config derives
    from the same environment (GRID_FLEET_STATE_DIR / XDG_STATE_HOME) via paths.state_dir;
    a broken config file already fell back to defaults, i.e. the same value.
    """
    if argv[:1] != ["tap"] or (len(argv) > 1 and argv[1] != "--"):
        return None
    return _tap(None, list(argv[2:]))
