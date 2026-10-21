"""Hub inbox: path helpers (Group 1), reader + staleness + aggregation (Group 4).

Owner: Group 4 unit (grid-frontend-dev) for everything beyond the path helpers.
Layout (design.md "Hub"), under the hub state dir:

    inbox/<host>.json          latest snapshot per host (atomic rename)
    inbox/.<host>.json.tmp     in-flight upload; ignored by readers

Reader contract (hub-view-tui spec):
  - READ-ONLY. This module never creates, writes, renames or deletes anything.
  - Ignored silently (not counted): dotfiles (in-flight uploads) and entries that do
    not end in `.json`.
  - Skipped AND counted (the view footer says "k files skipped"): `*.json` entries that
    are not regular files, are larger than 256 KiB, are unreadable, are not a JSON
    object, or carry an unknown `schema`.
  - Staleness uses the inbox file mtime on the HUB clock, never the snapshot's own
    `collected_at` (removes clock skew between machines). Stale when
    `now - mtime > 2 * interval_s`, interval taken from the snapshot.
  - Stale and never-reported hosts carry no metrics at all, so they cannot leak into
    totals by accident.

Public API:

    inbox_dir / inbox_path / inbox_tmp_path      (Group 1, unchanged)
    scan_inbox(state_dir) -> InboxScan           I/O: parse the inbox once
    inbox_signature(state_dir) -> tuple          cheap I/O: (name, mtime_ns, size) per file
    build_state(scan, now, expected_hosts=()) -> HubState   PURE: staleness + aggregation
    read_hub(state_dir, now, expected_hosts=()) -> HubState scan + build in one call
    Total(value, complete)                       a column total; see its docstring
"""

import json
import os
import stat
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from grid_fleet.config import DEFAULT_INTERVAL_S, sanitize_host_name

# Files above this are skipped unread (a snapshot is a few KiB; this is a safety cap).
MAX_FILE_BYTES = 256 * 1024
# Snapshot schemas this hub understands. Anything else is skipped and counted.
KNOWN_SCHEMAS = (1,)
# Stale when age > STALE_FACTOR * interval_s (design.md "Staleness").
STALE_FACTOR = 2
# Largest count the reader believes. Real token counts stay far below this; a snapshot
# claiming more (e.g. 10**400, which fits in the file size cap) is treated as "not
# promised" so the view never does float maths on an astronomically large int.
MAX_COUNT = 10 ** 15

FRESH = "fresh"
STALE = "stale"
NEVER = "never"  # named in expected_hosts, no inbox file


def _require_safe_host(host: str) -> str:
    # The host becomes a file name. Refuse anything the sanitiser would change,
    # so a caller that forgot to sanitise fails loudly instead of path-traversing.
    if host != sanitize_host_name(host):
        raise ValueError("unsafe host name: %r" % (host,))
    return host


def inbox_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "inbox")


def inbox_path(state_dir: str, host: str) -> str:
    return os.path.join(inbox_dir(state_dir), _require_safe_host(host) + ".json")


def inbox_tmp_path(state_dir: str, host: str) -> str:
    # Leading dot: readers skip dotfiles, so a half-written upload is never read.
    return os.path.join(inbox_dir(state_dir), "." + _require_safe_host(host) + ".json.tmp")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One accepted inbox file: display name, mtime (hub clock, epoch s) and parsed JSON."""

    name: str
    mtime: int
    snapshot: dict


@dataclass(frozen=True)
class InboxScan:
    """Result of one directory read; no clock involved so it can be cached between ticks."""

    entries: Tuple[Entry, ...] = ()
    skipped: int = 0


@dataclass(frozen=True)
class Total:
    """A summed column.

    value    sum over the fresh hosts that reported this figure; None when no fresh host
             did (the view renders that as `unknown`, never 0).
    complete False when at least one fresh host could not report it (null section), i.e.
             the sum is a lower bound. The view marks that with a trailing `+`.
    """

    value: Optional[int] = None
    complete: bool = True


@dataclass(frozen=True)
class HostState:
    """One row of the hosts table. Metrics are None unless status == FRESH."""

    name: str
    status: str  # FRESH | STALE | NEVER
    mtime: Optional[int] = None  # None for NEVER
    age_s: Optional[int] = None  # now - mtime, clamped >= 0; None for NEVER
    interval_s: int = DEFAULT_INTERVAL_S
    busy: Optional[int] = None
    waiting: Optional[int] = None
    out_24h: Optional[int] = None
    out_7d: Optional[int] = None
    cache_24h: Optional[int] = None  # input + cache_creation + cache_read (muted line)
    cache_7d: Optional[int] = None


@dataclass(frozen=True)
class HubState:
    now: int
    hosts: Tuple[HostState, ...]  # sorted by name; includes stale and never-reported
    skipped: int  # files skipped by the reader
    fresh: int  # hosts with status FRESH
    busy: Total
    waiting: Total
    out_24h: Total
    out_7d: Total
    cache_24h: Total
    cache_7d: Total

    @property
    def host_count(self) -> int:
        """The `m` in "n of m hosts fresh": every host seen or expected."""
        return len(self.hosts)


# ---------------------------------------------------------------------------
# Reading (the only I/O; read-only)
# ---------------------------------------------------------------------------


def _list_inbox(state_dir: str) -> List[str]:
    """Names of non-dot `*.json` entries. A missing inbox is just empty, not an error."""
    try:
        names = os.listdir(inbox_dir(state_dir))
    except OSError:
        return []
    return sorted(n for n in names if not n.startswith(".") and n.endswith(".json"))


def inbox_signature(state_dir: str) -> tuple:
    """Cheap change detector for the refresh poll: (name, mtime_ns, size) per file.

    Stat only, no reads. Compare to the previous value; re-scan when it differs.
    """
    sig = []
    for n in _list_inbox(state_dir):
        try:
            st = os.stat(os.path.join(inbox_dir(state_dir), n))
        except OSError:
            continue  # vanished between listdir and stat (atomic replace); next poll sees it
        sig.append((n, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _display_name(file_name: str) -> str:
    # Stem of `<host>.json`, passed through the host sanitiser so a hostile file name
    # cannot put control characters or escape sequences on the user's terminal.
    return sanitize_host_name(file_name[: -len(".json")])


def _load_one(path: str) -> Optional[Entry]:
    """Parse one inbox file; None means 'skip and count it'."""
    name = os.path.basename(path)
    try:
        st = os.stat(path)
        # Regular files only: opening a FIFO named x.json would block the UI forever.
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            # +1 so a file that grew past the cap after the stat is still rejected.
            raw = fh.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None
        # RecursionError: 256 KiB of nested brackets must not kill the UI.
        snap = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(snap, dict):
        return None
    schema = snap.get("schema")
    # `True == 1` in Python; a boolean schema is not schema 1.
    if isinstance(schema, bool) or schema not in KNOWN_SCHEMAS:
        return None
    return Entry(name=_display_name(name), mtime=int(st.st_mtime), snapshot=snap)


def scan_inbox(state_dir: str) -> InboxScan:
    """Read every inbox file once. Dotfiles are not even opened; see module docstring."""
    entries = []  # type: List[Entry]
    skipped = 0
    for n in _list_inbox(state_dir):
        entry = _load_one(os.path.join(inbox_dir(state_dir), n))
        if entry is None:
            skipped += 1
        else:
            entries.append(entry)
    return InboxScan(entries=tuple(entries), skipped=skipped)


# ---------------------------------------------------------------------------
# Pure part: extraction, staleness, aggregation
# ---------------------------------------------------------------------------


def _count(value) -> Optional[int]:
    """A non-negative int up to MAX_COUNT (bool excluded) or None. Anything else is 'not promised'."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        return None
    return value


def _interval(snapshot: dict) -> int:
    v = snapshot.get("interval_s")
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return DEFAULT_INTERVAL_S
    return v


def _section(snapshot: dict, key: str) -> Optional[dict]:
    v = snapshot.get(key)
    return v if isinstance(v, dict) else None


def _window(snapshot: dict, window: str) -> Optional[dict]:
    tokens = _section(snapshot, "tokens")
    if tokens is None:
        return None
    w = tokens.get(window)
    return w if isinstance(w, dict) else None


def _cache(window: Optional[dict]) -> Optional[int]:
    """input + cache_creation + cache_read; None unless all three are present."""
    if window is None:
        return None
    parts = [_count(window.get(k)) for k in ("input", "cache_creation", "cache_read")]
    if any(p is None for p in parts):
        return None
    return sum(parts)


def _host_state(entry: Entry, now: int) -> HostState:
    interval = _interval(entry.snapshot)
    # Clamp: a file stamped in the future (hub clock stepped back) is fresh, age 0.
    age = max(0, now - entry.mtime)
    if age > STALE_FACTOR * interval:
        return HostState(entry.name, STALE, entry.mtime, age, interval)
    snap = entry.snapshot
    sessions = _section(snap, "sessions")
    w24, w7 = _window(snap, "last_24h"), _window(snap, "last_7d")
    return HostState(
        name=entry.name,
        status=FRESH,
        mtime=entry.mtime,
        age_s=age,
        interval_s=interval,
        busy=_count(sessions.get("busy")) if sessions else None,
        waiting=_count(sessions.get("waiting")) if sessions else None,
        out_24h=_count(w24.get("output")) if w24 else None,
        out_7d=_count(w7.get("output")) if w7 else None,
        cache_24h=_cache(w24),
        cache_7d=_cache(w7),
    )


def _sum(values: Iterable[Optional[int]]) -> Total:
    """Sum the known values; remember whether any host was unable to report."""
    vals = list(values)
    known = [v for v in vals if v is not None]
    if not known:
        return Total(None, not vals)
    return Total(sum(known), len(known) == len(vals))


def build_state(scan: InboxScan, now: int, expected_hosts: Iterable[str] = ()) -> HubState:
    """Pure: scan + hub clock + expected hosts -> everything the view needs.

    `now` is passed in (config.now()) so tests control the clock. Totals cover FRESH
    hosts only; `expected_hosts` with no inbox entry become NEVER rows.
    """
    hosts = [_host_state(e, now) for e in scan.entries]
    seen = set(h.name for h in hosts)
    for name in expected_hosts:
        if name not in seen:
            hosts.append(HostState(name=name, status=NEVER))
            seen.add(name)
    hosts.sort(key=lambda h: h.name)
    fresh = [h for h in hosts if h.status == FRESH]
    return HubState(
        now=now,
        hosts=tuple(hosts),
        skipped=scan.skipped,
        fresh=len(fresh),
        busy=_sum(h.busy for h in fresh),
        waiting=_sum(h.waiting for h in fresh),
        out_24h=_sum(h.out_24h for h in fresh),
        out_7d=_sum(h.out_7d for h in fresh),
        cache_24h=_sum(h.cache_24h for h in fresh),
        cache_7d=_sum(h.cache_7d for h in fresh),
    )


def read_hub(state_dir: str, now: int, expected_hosts: Iterable[str] = ()) -> HubState:
    """Convenience: scan_inbox + build_state."""
    return build_state(scan_inbox(state_dir), now, expected_hosts)
