"""Hub inbox: path helpers (Group 1), reader + staleness + aggregation (Group 4).

Owner: Group 4 unit (grid-frontend-dev) for everything beyond the path helpers.
Layout (design.md "Hub"), under the hub state dir:

    inbox/<host>.json          latest snapshot per host (atomic rename)
    inbox/.<host>.json.<uniq>.tmp   in-flight upload (unique per push); ignored by readers

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
    build_limits(fresh_entries, now)             PURE: per-account limits blocks (task 7.4)
    build_agents / build_by_day(fresh_entries)   PURE: leaderboard + sparkline data (task 6.2)
"""

import datetime
import json
import math
import os
import re
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
# The snapshot's own interval_s is untrusted: a host claiming 10**12 would never go stale.
# Values above this are clamped to it. The installer only allows 300/600/900, so every
# legitimate snapshot is far below.
MAX_INTERVAL_S = 3600
# Most inbox files read per scan; the rest are counted as skipped. Real fleets have a dozen
# hosts. Bounds the work (and memory) a directory full of files can cause per refresh.
MAX_INBOX_FILES = 512
# `limits.captured_at` may be at most this far ahead of the hub clock (spoke clock skew).
# Further ahead is not believable and would beat every honest capture forever, so such a
# snapshot's limits are ignored (the windows show as unknown).
MAX_CAPTURE_SKEW_S = 300
# Largest count the reader believes. Real token counts stay far below this; a snapshot
# claiming more (e.g. 10**400, which fits in the file size cap) is treated as "not
# promised" so the view never does float maths on an astronomically large int.
MAX_COUNT = 10 ** 15

# Limits numbers are untrusted too. Epoch seconds above this (year ~5138) are "not promised":
# it keeps time.localtime / strftime in the view well inside what every platform accepts.
MAX_EPOCH = 10 ** 11
# A used percentage the reader believes. Real values are 0-100; a little over is clamped to
# 100 (a maxed-out plan must still show as full), anything beyond MAX_PCT is not promised.
MAX_PCT = 1000.0
# Account label shown in the limits strip: printable ASCII only (it is free text from a
# remote machine and must not put escape sequences on the terminal), bounded length.
ACCOUNT_LABEL_MAX = 32
DEFAULT_ACCOUNT_LABEL = "default"

# Leaderboard / sparkline (task 6.2). Snapshot lists are untrusted: bound how much of each
# one is read so a hostile 256 KiB file full of tiny rows cannot make the view do unbounded
# work, and bound what reaches the screen.
LEADERBOARD_SIZE = 8  # design.md: top 8 agent types by invocations
SPARK_DAYS = 7  # by_day carries 7 UTC dates
MAX_AGENT_ROWS_PER_HOST = 200  # rows of agents.last_7d read per host (real fleets: a dozen)
MAX_BY_DAY_PER_HOST = 14  # rows of tokens.by_day read per host (real: exactly 7)
AGENT_TYPE_MAX = 24  # agent type labels are cut to this many characters

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
class LimitWindow:
    """One limit window (5h or 7d) of one account, as the view should show it.

    pct          used percentage 0-100, or None = render `unknown` (absent, invalid or expired).
                 Never 0 for "we do not know".
    resets_at    epoch s the window resets; None when not promised.
    age_s        hub now - the capture time of the snapshot it came from (>= 0); None when
                 no snapshot supplied the window. The view prints it as `as of <age>`.
    expired      True when the stored window's resets_at has passed: the stored percentage no
                 longer applies, so pct is None. Lets the view say why it is unknown.
    """

    pct: Optional[float] = None
    resets_at: Optional[int] = None
    age_s: Optional[int] = None
    expired: bool = False


@dataclass(frozen=True)
class AccountLimits:
    """The limits block for one account label. `account` is "" only for the placeholder
    block shown when there is no fresh host at all (so nothing names an account)."""

    account: str
    five_hour: LimitWindow = LimitWindow()
    seven_day: LimitWindow = LimitWindow()


@dataclass(frozen=True)
class AgentRow:
    """One leaderboard row: an agent type summed across the fresh hosts.

    type         terminal-safe label (printable ASCII, at most AGENT_TYPE_MAX characters).
    invocations  sum of agents.last_7d[].invocations over fresh hosts (> 0).
    output       sum of the believable `output` figures, or None when no host promised one.
    """

    type: str
    invocations: int
    output: Optional[int] = None


@dataclass(frozen=True)
class DayTotal:
    """Output tokens for one UTC date (`YYYY-MM-DD`), summed across the fresh hosts."""

    day: str
    output: int


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
    # One block per account label among the fresh hosts, sorted by label. Never empty:
    # with nothing to show, one unknown block (see build_limits).
    limits: Tuple[AccountLimits, ...] = ()
    # Task 6.2: top LEADERBOARD_SIZE agent types (invocations desc, then type asc) and the
    # last SPARK_DAYS UTC dates of output tokens, both over FRESH hosts only. Empty tuple
    # means "no fresh host reported it" (the view says so in words; never a row of zeros).
    agents: Tuple[AgentRow, ...] = ()
    by_day: Tuple[DayTotal, ...] = ()

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
    names = _list_inbox(state_dir)
    for n in names[:MAX_INBOX_FILES]:  # same cap as scan_inbox: bounded stat work per poll
        try:
            st = os.stat(os.path.join(inbox_dir(state_dir), n))
        except OSError:
            continue  # vanished between listdir and stat (atomic replace); next poll sees it
        sig.append((n, st.st_mtime_ns, st.st_size))
    if len(names) > MAX_INBOX_FILES:
        # Files past the cap are not stat-ed, but their number feeds the footer's skipped
        # count, so a change in it must still change the signature.
        sig.append(("<over-cap>", len(names) - MAX_INBOX_FILES, 0))
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
    """Read every inbox file once. Dotfiles are not even opened; see module docstring.

    At most MAX_INBOX_FILES files are read (name order, so deterministic); the rest are not
    opened and are counted as skipped.
    """
    entries = []  # type: List[Entry]
    names = _list_inbox(state_dir)
    skipped = max(0, len(names) - MAX_INBOX_FILES)
    for n in names[:MAX_INBOX_FILES]:
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
    return min(v, MAX_INTERVAL_S)  # a hostile huge value must not make a dead host look fresh


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


# ---------------------------------------------------------------------------
# Limits (per account, per window)
# ---------------------------------------------------------------------------

# The two windows, in the order the strip shows them, with the snapshot key for each.
LIMIT_WINDOWS = ("five_hour", "seven_day")


def _epoch(value) -> Optional[int]:
    """A finite epoch-seconds number in (0, MAX_EPOCH] as an int, else None (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not 0 < value <= MAX_EPOCH:
        return None
    return int(value)


def _pct(value) -> Optional[float]:
    """A finite percentage in [0, MAX_PCT], clamped to 100, else None (bool excluded).

    Strings, null, negatives, NaN/inf and absurdly large numbers are all "not promised".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not 0 <= value <= MAX_PCT:
        return None
    return float(min(value, 100))


def _account_label(snapshot: dict) -> str:
    """The snapshot's `account`, made terminal-safe; missing or non-text means the default."""
    raw = snapshot.get("account")
    if not isinstance(raw, str):
        return DEFAULT_ACCOUNT_LABEL
    label = "".join(c if " " <= c <= "~" else "?" for c in raw).strip()[:ACCOUNT_LABEL_MAX]
    return label or DEFAULT_ACCOUNT_LABEL


def _snapshot_limits(snapshot: dict, now: int) -> Tuple[Optional[int], dict]:
    """(captured_at, limits dict) from a snapshot; (None, {}) when there is nothing usable.

    Without a believable captured_at the snapshot cannot take part in "greatest captured_at",
    so its limits are ignored rather than guessed at. "Believable" includes not being dated
    more than MAX_CAPTURE_SKEW_S after the hub clock: a future stamp would otherwise win the
    "greatest captured_at" contest for good.
    """
    lim = snapshot.get("limits")
    if not isinstance(lim, dict):
        return None, {}
    captured = _epoch(lim.get("captured_at"))
    if captured is None or captured > now + MAX_CAPTURE_SKEW_S:
        return None, {}
    return captured, lim


def _pick_window(candidates, window: str, now: int) -> LimitWindow:
    """Choose one window from (captured_at, limits) pairs of one account's fresh hosts.

    Each window is chosen independently: the greatest captured_at among the snapshots that
    carry a usable value for THIS window wins (a host that has only seen the 5h window must
    not blank out another host's 7d). Ties keep the first (candidates are in host order).
    Then the expiry rule: resets_at <= now means the stored percentage is stale -> unknown.
    """
    best = None  # (captured_at, pct, resets_at)
    for captured, lim in candidates:
        w = lim.get(window)
        if not isinstance(w, dict):
            continue
        pct, resets = _pct(w.get("used_pct")), _epoch(w.get("resets_at"))
        if pct is None or resets is None:
            continue  # half a window is no window: never show a bar we cannot place in time
        if best is None or captured > best[0]:
            best = (captured, pct, resets)
    if best is None:
        return LimitWindow()  # absent everywhere: unknown, not 0
    captured, pct, resets = best
    age = max(0, now - captured)  # hub clock; clamped like host age
    if now >= resets:
        return LimitWindow(None, resets, age, True)
    return LimitWindow(pct, resets, age, False)


def build_limits(fresh_entries, now: int) -> Tuple[AccountLimits, ...]:
    """Per-account limits blocks from the FRESH hosts' snapshots (pure).

    Stale hosts are excluded by the caller. Accounts are the labels of the fresh hosts
    (a fresh host without limits still creates its account's all-unknown block). With no
    fresh host at all, a single unnamed all-unknown block keeps the strip visible.
    """
    by_account = {}  # type: dict
    for entry in fresh_entries:
        by_account.setdefault(_account_label(entry.snapshot), []).append(
            _snapshot_limits(entry.snapshot, now))
    if not by_account:
        return (AccountLimits(""),)
    blocks = []
    for account in sorted(by_account):
        cands = [(c, lim) for c, lim in by_account[account] if c is not None]
        blocks.append(AccountLimits(
            account,
            _pick_window(cands, "five_hour", now),
            _pick_window(cands, "seven_day", now),
        ))
    return tuple(blocks)


# ---------------------------------------------------------------------------
# Leaderboard and sparkline (task 6.2)
# ---------------------------------------------------------------------------

# A by_day `day` must look exactly like this AND be a real calendar date; anything else
# (hostile text, "2026-13-45") is dropped rather than shown or used as a sort key.
_DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _ascii_label(raw, max_len: int) -> str:
    """Printable ASCII only (everything else becomes `?`), stripped, cut to max_len.

    Snapshot strings come from remote machines; an ESC or other control byte must never
    reach the terminal. Empty after cleaning returns "".
    """
    return "".join(c if " " <= c <= "~" else "?" for c in raw).strip()[:max_len]


def _valid_day(value) -> Optional[str]:
    """`value` if it is a real `YYYY-MM-DD` date string, else None."""
    if not isinstance(value, str) or not _DAY_RE.match(value):
        return None
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:]))
    except ValueError:
        return None
    return value


def build_agents(fresh_entries) -> Tuple[AgentRow, ...]:
    """Top agent types by invocations summed across the FRESH hosts' `agents.last_7d` (pure).

    Stale hosts are excluded by the caller. A host whose `agents` is null (or not the
    promised shape) contributes nothing and is not an error. Per row, `type` must be text
    and `invocations` a believable count > 0 (see _count); a row failing either is ignored
    on its own, the host's other rows still count. `main` is an ordinary type here.
    Sort: invocations desc; ties by type name asc (the spec only says "invocations desc",
    so the tie-break is ours: deterministic, so the display does not jitter between ticks).
    """
    inv = {}  # type: dict
    out = {}  # type: dict
    for entry in fresh_entries:
        agents = _section(entry.snapshot, "agents")
        rows = agents.get("last_7d") if agents else None
        if not isinstance(rows, list):
            continue
        for row in rows[:MAX_AGENT_ROWS_PER_HOST]:
            if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                continue
            n = _count(row.get("invocations"))
            if not n:  # None (not promised) and 0 (nothing to rank) both drop out
                continue
            # A type that cleans to nothing is reported like a missing meta file would be.
            label = _ascii_label(row["type"], AGENT_TYPE_MAX) or "unknown"
            inv[label] = inv.get(label, 0) + n
            o = _count(row.get("output"))
            if o is not None:
                out[label] = out.get(label, 0) + o
    ranked = sorted(inv.items(), key=lambda kv: (-kv[1], kv[0]))[:LEADERBOARD_SIZE]
    return tuple(AgentRow(t, n, out.get(t)) for t, n in ranked)


def build_by_day(fresh_entries) -> Tuple[DayTotal, ...]:
    """Output tokens per UTC date summed across FRESH hosts' `tokens.by_day` (pure).

    Aligned by the `day` string, not by position, so hosts whose windows end on different
    dates still line up. A host with null/short/odd `by_day` contributes the days it has.
    Rows with an invalid date or an unbelievable `output` are ignored; a repeated date
    within one host counts once (first wins). Returns the most recent SPARK_DAYS dates that
    any fresh host reported, oldest first; a date no host reported is simply absent
    (unknown, not zero).
    """
    totals = {}  # type: dict
    for entry in fresh_entries:
        tokens = _section(entry.snapshot, "tokens")
        rows = tokens.get("by_day") if tokens else None
        if not isinstance(rows, list):
            continue
        seen = set()
        for row in rows[:MAX_BY_DAY_PER_HOST]:
            if not isinstance(row, dict):
                continue
            day, o = _valid_day(row.get("day")), _count(row.get("output"))
            if day is None or o is None or day in seen:
                continue
            seen.add(day)
            totals[day] = totals.get(day, 0) + o
    return tuple(DayTotal(d, totals[d]) for d in sorted(totals)[-SPARK_DAYS:])


def build_state(scan: InboxScan, now: int, expected_hosts: Iterable[str] = ()) -> HubState:
    """Pure: scan + hub clock + expected hosts -> everything the view needs.

    `now` is passed in (config.now()) so tests control the clock. Totals cover FRESH
    hosts only; `expected_hosts` with no inbox entry become NEVER rows.
    """
    hosts = [_host_state(e, now) for e in scan.entries]
    # Entries whose host is FRESH, in the scan's (file name) order: the limits source.
    fresh_entries = [e for e, h in zip(scan.entries, hosts) if h.status == FRESH]
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
        limits=build_limits(fresh_entries, now),
        agents=build_agents(fresh_entries),
        by_day=build_by_day(fresh_entries),
    )


def read_hub(state_dir: str, now: int, expected_hosts: Iterable[str] = ()) -> HubState:
    """Convenience: scan_inbox + build_state."""
    return build_state(scan_inbox(state_dir), now, expected_hosts)
