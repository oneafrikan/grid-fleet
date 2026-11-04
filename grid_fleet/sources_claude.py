"""The ONLY module that knows Claude Code's on-disk/CLI layout.

Owner: Group 2 unit (grid-backend-dev). Covers `claude agents --json`, the
transcript scan with the offset cache, and the tested-versions warning.

Public API (snapshot.py is the main caller):

    TESTED_VERSIONS                       versions we have verified the layout on
    parse_version(text) -> Optional[str]  tolerant of "2.1.305" and "2.1.305 (Claude Code)"
    get_version(claude_bin) -> Optional[str]
    version_warning(version) -> Optional[str]
    collect_sessions(claude_bin, timeout=None) -> (counts or None, warnings)
    scan_transcripts(projects_dir, state_dir, now, opener=open) -> ScanResult
    summarize_tokens(entries, now) -> dict    last_24h / last_7d / by_day
    summarize_agents(entries, now) -> dict    {"last_7d": [{type, invocations, output}, ...]}
    window_keys(now) -> WindowKeys            hour-key bounds shared by both summaries
    atomic_write_json(path, obj)              tmp + rename, mode 0600

Privacy contract (design.md "Incremental token summing"): from a transcript
line we read ONLY `type`, `message.id`, `message.usage`, `timestamp`; from a
subagent meta file ONLY `agentType`. Nothing else is stored, logged or put in a
warning. Warnings are fixed strings with counts: never a path, name or id,
and never exception text (exception text can carry a path).

Python 3.9, stdlib only.
"""

import datetime
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

# --- versions ---------------------------------------------------------------

# Versions the transcript/CLI layout has been verified on. A run on any other
# version still collects (fail soft) but adds a warning.
TESTED_VERSIONS = ("2.1.305",)

VERSION_TIMEOUT_S = 5
# Read at call time (not bound as a default arg) so tests can shorten it.
AGENTS_TIMEOUT_S = 10

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")

# --- layout / retention constants -------------------------------------------

SCAN_DAYS = 8  # only transcripts modified this recently are scanned; hours older than this are pruned
DAY_S = 86400
HOUR_S = 3600
OFFSETS_SCHEMA = 1
OFFSETS_FILE = "offsets.json"

# Index into the per-hour sums list: [output, input, cache_creation, cache_read].
_FIELDS = ("output", "input", "cache_creation", "cache_read")
_USAGE_KEYS = (
    "output_tokens",
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

LABEL_MAIN = "main"
LABEL_UNKNOWN = "unknown"
_LABEL_BAD_CHARS = re.compile(r"[^A-Za-z0-9._:-]")
_LABEL_MAX = 64
# A real meta file is a few bytes ({"agentType": "..."}). Read at most this much so a
# hostile or corrupt multi-GB "meta" file cannot exhaust memory; larger means "unknown".
_META_MAX_BYTES = 64 * 1024

# "2026-10-21T14:03:00.000Z" -> hour key "2026-10-21T14". Anything not in this
# shape takes the slower datetime path below.
_TS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}")


def parse_version(text: object) -> Optional[str]:
    """Pull the first x.y.z out of `claude --version` output, or None."""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.search(text)
    return m.group(0) if m else None


def get_version(claude_bin: str) -> Optional[str]:
    """Run `claude --version`; None on any failure (the caller just omits it)."""
    try:
        proc = subprocess.run(
            [claude_bin, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_version(proc.stdout.decode("utf-8", "replace"))


def version_warning(version: Optional[str]) -> Optional[str]:
    """Warning string when a known version is outside the tested list, else None."""
    if version is None or version in TESTED_VERSIONS:
        return None
    return "claude %s not in tested versions" % version


# --- sessions ---------------------------------------------------------------


def _pid_alive(pid: object) -> bool:
    """True if `pid` is a running process we can name.

    pid <= 0 is rejected explicitly: os.kill(0, 0) signals the caller's whole
    process group and "succeeds", which would count a bogus row as live.
    PermissionError means the process exists but belongs to someone else: alive.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError, OSError):
        return False
    return True


def collect_sessions(
    claude_bin: str, timeout: Optional[float] = None
) -> Tuple[Optional[Dict[str, int]], List[str]]:
    """Count live interactive sessions from `claude agents --json`.

    Returns (counts, warnings). counts is None (never zeros) when the command
    is missing, fails, times out or does not produce a JSON list.
    """
    limit = AGENTS_TIMEOUT_S if timeout is None else timeout
    try:
        proc = subprocess.run(
            [claude_bin, "agents", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return None, ["claude agents timed out"]
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError / PermissionError etc: binary absent or not runnable.
        return None, ["claude agents unavailable"]
    if proc.returncode != 0:
        return None, ["claude agents failed"]
    try:
        rows = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None, ["claude agents output not valid JSON"]
    if not isinstance(rows, list):
        return None, ["claude agents output not valid JSON"]

    counts = {"busy": 0, "waiting": 0, "idle": 0, "other": 0, "total": 0}
    for row in rows:
        # Only interactive sessions with a live process count: background
        # rows and rows whose process has gone are stale noise (spec).
        if not isinstance(row, dict) or row.get("kind") != "interactive":
            continue
        if not _pid_alive(row.get("pid")):
            continue
        status = row.get("status")
        key = status if status in ("busy", "waiting", "idle") else "other"
        counts[key] += 1
        counts["total"] += 1
    return counts, []


# --- atomic file write ------------------------------------------------------


def atomic_write_json(path: str, obj: object) -> None:
    """Write JSON to `path` via tmp + rename, mode 0600, creating the dir 0700.

    mkstemp creates the temp file 0600 in the same directory, so the rename is
    atomic and no reader ever sees a half-written file. Raises OSError.
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


# --- time helpers -----------------------------------------------------------


def _hour_key(epoch: int) -> str:
    """UTC hour bucket key, e.g. "2026-10-21T14". Sorts lexicographically."""
    return time.strftime("%Y-%m-%dT%H", time.gmtime(epoch))


def _ts_to_hour_key(ts: object) -> Optional[str]:
    """Event timestamp -> UTC hour key, or None if unusable.

    Fast path: Claude writes "...Z" UTC timestamps, so the first 13 chars are
    already the key. Offset-bearing timestamps go through datetime (3.9
    fromisoformat cannot take a trailing Z, so that is rewritten).
    """
    if not isinstance(ts, str) or not _TS_Z.match(ts):
        return None
    if ts.endswith("Z"):
        return ts[:13]
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return ts[:13]  # naive: treat as UTC, same as the Z path
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H")


class WindowKeys(object):
    """Hour-key bounds for the reported windows, derived from one `now`.

    Windows are whole UTC hour buckets ending with the current (partial) hour:
    last_24h = 24 buckets, last_7d = 168 buckets. An event 25 h old therefore
    never lands in last_24h. `days` are the 7 UTC dates ending today.
    """

    def __init__(self, now: int):
        base = now - (now % HOUR_S)
        self.now_key = _hour_key(base)
        self.key_24h = _hour_key(base - 23 * HOUR_S)
        self.key_7d = _hour_key(base - (7 * 24 - 1) * HOUR_S)
        self.key_prune = _hour_key(now - SCAN_DAYS * DAY_S)
        today = now - (now % DAY_S)
        self.days = [time.strftime("%Y-%m-%d", time.gmtime(today - i * DAY_S)) for i in range(6, -1, -1)]

    def in_24h(self, key: str) -> bool:
        return self.key_24h <= key <= self.now_key

    def in_7d(self, key: str) -> bool:
        return self.key_7d <= key <= self.now_key


def window_keys(now: int) -> WindowKeys:
    return WindowKeys(now)


# --- transcript scan --------------------------------------------------------


class ScanResult(object):
    """What the scan hands to snapshot.py.

    entries       relpath -> {"offset","size","agent","bad","last","hours"}; the group 6
                  leaderboard reads `agent` and `hours` from here.
    matched_files transcripts on disk inside the 8-day mtime window (0 while sessions
                  are busy suggests an unknown layout; snapshot.py decides).
    warnings      count-only strings.
    """

    def __init__(self, entries: Dict[str, dict], matched_files: int, warnings: List[str]):
        self.entries = entries
        self.matched_files = matched_files
        self.warnings = warnings


def _sanitize_label(raw: object) -> str:
    """agentType -> a short, safe label; non-strings and empties become "unknown".

    The spec is silent on odd agent type strings, so this is the conservative rule:
    printable ASCII from [A-Za-z0-9._:-] only (everything else, control characters and
    non-ASCII included, becomes "_"), at most 64 characters. It is applied again when the
    leaderboard is built, so even a hand-edited cache cannot put an unsafe string in a snapshot.
    """
    if not isinstance(raw, str) or not raw:
        return LABEL_UNKNOWN
    return _LABEL_BAD_CHARS.sub("_", raw)[:_LABEL_MAX]


def _classify(relpath: str) -> Optional[str]:
    """Return "main", "sub" or None (unrecognised shape: ignored) for a relpath.

    Verified layout: <project>/<session>.jsonl and
    <project>/<session>/subagents/agent-<id>.jsonl. Anything else is skipped
    rather than guessed at.
    """
    parts = relpath.split(os.sep)
    if len(parts) == 2:
        return "main"
    if len(parts) == 4 and parts[2] == "subagents" and parts[3].startswith("agent-"):
        return "sub"
    return None


def _read_agent_label(path: str, opener: Callable) -> str:
    """agentType from the sibling meta file, or "unknown" if absent/unreadable."""
    meta = path[: -len(".jsonl")] + ".meta.json"
    try:
        with opener(meta, "rb") as fh:
            raw = fh.read(_META_MAX_BYTES + 1)
        if len(raw) > _META_MAX_BYTES:
            return LABEL_UNKNOWN  # not a plausible meta file
        data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return LABEL_UNKNOWN
    return _sanitize_label(data.get("agentType") if isinstance(data, dict) else None)


def _is_vec(v: object) -> bool:
    return isinstance(v, list) and len(v) == 4 and all(isinstance(x, int) for x in v)


def _is_last(v: object) -> bool:
    return (
        isinstance(v, dict)
        and isinstance(v.get("id"), str)
        and isinstance(v.get("hour"), str)
        and _is_vec(v.get("u"))
    )


def _load_offsets(state_dir: str, opener: Callable) -> Optional[Dict[str, dict]]:
    """Load the cache; None means missing/corrupt (caller rebuilds from scratch)."""
    path = os.path.join(state_dir, OFFSETS_FILE)
    try:
        with opener(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != OFFSETS_SCHEMA:
        return None
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    clean = {}  # type: Dict[str, dict]
    for rel, ent in files.items():
        if not isinstance(ent, dict):
            return None  # one bad entry means we cannot trust the file: rebuild
        ok = (
            isinstance(ent.get("offset"), int)
            and isinstance(ent.get("size"), int)
            and isinstance(ent.get("agent"), str)
            and isinstance(ent.get("hours"), dict)
            and all(_is_vec(v) for v in ent["hours"].values())
            and (ent.get("last") is None or _is_last(ent["last"]))
        )
        if not ok:
            return None
        ent.setdefault("bad", 0)
        ent.setdefault("last", None)
        clean[rel] = ent
    return clean


def _usage_vector(usage: dict) -> List[int]:
    """[output, input, cache_creation, cache_read]; non-int / negative values count as 0."""
    out = []
    for key in _USAGE_KEYS:
        v = usage.get(key)
        out.append(v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0)
    return out


def _consume(data: bytes, ent: dict) -> int:
    """Fold the complete lines of `data` into `ent`. Returns bytes consumed.

    Only complete lines are processed: we stop at the last newline, so a
    half-written trailing line is left for the next run (spec: partial last line).

    Duplicate rule (spike 2.3, decided in design.md): consecutive usage lines
    with the same message.id are one message streamed in chunks; keep the last.
    `ent["last"]` carries the previous counted message across runs so a message
    split over a run boundary still collapses (otherwise a warm run would
    double count and differ from a from-scratch recount).
    """
    end = data.rfind(b"\n")
    if end < 0:
        return 0
    hours = ent["hours"]
    last = ent["last"]  # {"id", "hour", "u"} or None
    bad = 0
    for line in data[: end + 1].split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if obj.get("type") != "assistant" or not isinstance(usage, dict):
            continue  # a normal non-usage line (user turn, tool result...): not an error
        key = _ts_to_hour_key(obj.get("timestamp"))
        if key is None:
            bad += 1  # usage with no usable timestamp cannot be bucketed
            continue
        vec = _usage_vector(usage)
        mid = msg.get("id")
        mid = mid if isinstance(mid, str) and mid else None
        if mid is not None and last is not None and last["id"] == mid:
            # Same message again: take back what the previous chunk added.
            prev = hours.get(last["hour"])
            if prev is not None:
                for i in range(4):
                    prev[i] -= last["u"][i]
        slot = hours.setdefault(key, [0, 0, 0, 0])
        for i in range(4):
            slot[i] += vec[i]
        last = {"id": mid, "hour": key, "u": vec} if mid is not None else None
    ent["last"] = last
    ent["bad"] += bad
    return end + 1


def scan_transcripts(
    projects_dir: str, state_dir: str, now: int, opener: Callable = open
) -> ScanResult:
    """Incrementally sum usage from main + subagent transcripts. Never raises.

    `opener(path, mode)` is injectable so tests can count bytes actually read.
    Caller must have checked that projects_dir exists (a missing dir is the
    "unavailable section" case and is decided in snapshot.py).
    """
    cache = _load_offsets(state_dir, opener)
    dirty = cache is None  # missing/corrupt cache: write a fresh one this run
    entries = cache if cache is not None else {}
    warnings = []  # type: List[str]
    keys = WindowKeys(now)
    cutoff_mtime = now - SCAN_DAYS * DAY_S
    unreadable = 0
    matched = 0
    seen = set()

    for dirpath, _dirs, files in os.walk(projects_dir):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, projects_dir)
            kind = _classify(rel)
            if kind is None:
                continue
            try:
                st = os.stat(path)
            except OSError:
                unreadable += 1
                continue
            if st.st_mtime < cutoff_mtime:
                continue  # not scanned; any cached hours age out below
            matched += 1
            seen.add(rel)

            ent = entries.get(rel)
            if ent is not None and (st.st_size < ent["offset"] or st.st_size < ent["size"]):
                # Truncated or rewritten: drop the entry and recount from byte 0.
                ent = None
                del entries[rel]
                dirty = True
            if ent is None:
                ent = {"offset": 0, "size": 0, "agent": LABEL_MAIN, "bad": 0, "last": None, "hours": {}}
                entries[rel] = ent
                ent["agent"] = LABEL_MAIN if kind == "main" else _read_agent_label(path, opener)
                dirty = True
            elif kind == "sub" and ent["agent"] == LABEL_UNKNOWN:
                # Meta file may have appeared after the transcript did.
                label = _read_agent_label(path, opener)
                if label != ent["agent"]:
                    ent["agent"] = label
                    dirty = True

            if st.st_size == ent["offset"]:
                ent["size"] = st.st_size
                continue  # nothing new: no read at all (the warm-run hot path)
            try:
                with opener(path, "rb") as fh:
                    fh.seek(ent["offset"])
                    data = fh.read()
            except OSError:
                unreadable += 1
                continue
            consumed = _consume(data, ent)
            ent["offset"] += consumed
            new_size = ent["offset"] + (len(data) - consumed)  # include an unterminated tail
            if consumed or new_size != ent["size"]:
                dirty = True  # an unchanged partial tail alone does not rewrite the cache
            ent["size"] = new_size

    # Retention: drop hour buckets older than the horizon everywhere; an entry
    # whose file is no longer scanned (deleted, or idle > 8 days) is pruned once
    # it has no hours left. Deleted files keep counting until they age out.
    for rel in list(entries):
        ent = entries[rel]
        old = [k for k in ent["hours"] if k < keys.key_prune]
        for k in old:
            del ent["hours"][k]
            dirty = True
        if rel not in seen and not ent["hours"]:
            del entries[rel]
            dirty = True

    bad_total = sum(e["bad"] for e in entries.values())
    if bad_total:
        warnings.append("skipped %d unparseable transcript lines" % bad_total)
    if unreadable:
        warnings.append("%d transcript files unreadable" % unreadable)

    if dirty:
        try:
            atomic_write_json(
                os.path.join(state_dir, OFFSETS_FILE), {"schema": OFFSETS_SCHEMA, "files": entries}
            )
        except OSError:
            warnings.append("offset cache not writable")

    return ScanResult(entries, matched, warnings)


def summarize_tokens(entries: Dict[str, dict], now: int) -> dict:
    """Window totals from the hour buckets: last_24h, last_7d and 7 by_day entries."""
    keys = WindowKeys(now)
    zero = lambda: [0, 0, 0, 0]  # noqa: E731 - tiny local factory
    t24, t7 = zero(), zero()
    per_day = {d: zero() for d in keys.days}
    for ent in entries.values():
        for key, vec in ent["hours"].items():
            if keys.in_7d(key):
                for i in range(4):
                    t7[i] += vec[i]
                    if keys.in_24h(key):
                        t24[i] += vec[i]
            day = per_day.get(key[:10])
            if day is not None and key <= keys.now_key:
                for i in range(4):
                    day[i] += vec[i]

    def as_dict(vec):
        return dict(zip(_FIELDS, vec))

    by_day = []
    for d in keys.days:
        row = {"day": d}
        row.update(as_dict(per_day[d]))
        by_day.append(row)
    return {"last_24h": as_dict(t24), "last_7d": as_dict(t7), "by_day": by_day}


def summarize_agents(entries: Dict[str, dict], now: int) -> dict:
    """Subagent leaderboard over the 168-bucket 7d window (spec: Subagent leaderboard).

    Derived entirely from the offset cache (`agent` label + `hours` buckets), so it adds no
    file reads and needs no new cache fields. One cache entry is one transcript:

      invocations  transcripts with at least one usage event (hour bucket) in the window.
                   This is the decided definition (design.md): not spawn events.
      output       output tokens in the window buckets, summed over those transcripts.

    Types come from the per-file label: "main" for session transcripts, `agentType` from the
    meta file for subagents, "unknown" when that is missing or unusable. Transcripts deleted
    within the window still count, same as the token totals.

    Order: invocations descending. The spec gives no tie-break; ties are broken by output
    descending, then type ascending, so the list is deterministic run to run.
    """
    keys = WindowKeys(now)
    acc = {}  # type: Dict[str, List[int]]  label -> [invocations, output]
    for ent in entries.values():
        # An empty bucket dict (no usage lines yet) is not an invocation.
        in_window = [vec for key, vec in ent["hours"].items() if keys.in_7d(key)]
        if not in_window:
            continue
        # Re-sanitise: the cache is a file on disk and is not trusted to hold a safe label.
        label = _sanitize_label(ent["agent"])
        slot = acc.setdefault(label, [0, 0])
        slot[0] += 1
        slot[1] += sum(vec[0] for vec in in_window)
    rows = [{"type": t, "invocations": v[0], "output": v[1]} for t, v in acc.items()]
    rows.sort(key=lambda r: (-r["invocations"], -r["output"], r["type"]))
    return {"last_7d": rows}
