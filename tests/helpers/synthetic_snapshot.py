"""Builders for synthetic schema-1 snapshots and hub inbox files (design.md "Snapshot").

Everything here is invented data (hosts like alpha / beta); nothing reads a real
Claude tree or a real inbox. The hub tests and the render tests share it.

    make_snapshot(host, **overrides) -> dict      a full, valid schema-1 snapshot
    make_limits / limit_window                    the snapshot `limits` section (task 7.4)
    agent_row / make_agents / by_day_rows / make_tokens   `agents` and `tokens.by_day` (task 6.3)
    NULL                                          pass as a section value to get JSON null
    write_inbox_file(state_dir, name, obj, mtime) write inbox/<name>.json, then os.utime
    snapshot_tree(state_dir)                      {relpath: (bytes, mtime_ns)} for read-only checks
"""

import json
import os
from typing import Dict, Optional, Tuple, Union

# Fixed "hub now" used across the tests (epoch seconds). Matches the design sketch's
# collected_at plus three minutes, so a file written at COLLECTED_AT is 3m old.
COLLECTED_AT = 1792580580
NOW = COLLECTED_AT + 180


class _Null(object):
    """Marker: 'make this section JSON null' (None means 'use the default' instead)."""

    def __repr__(self) -> str:
        return "NULL"


NULL = _Null()


def _pick(value, default):
    if value is NULL:
        return None
    return default if value is None else value


def tokens_window(output=0, input=0, cache_creation=0, cache_read=0) -> dict:
    return {
        "output": output,
        "input": input,
        "cache_creation": cache_creation,
        "cache_read": cache_read,
    }


def limit_window(used_pct, resets_at) -> dict:
    """One limits window exactly as the snapshot-collect / limits-capture specs shape it."""
    return {"used_pct": used_pct, "resets_at": resets_at}


def make_limits(captured_at: int = COLLECTED_AT, five_hour=None, seven_day=None) -> dict:
    """A snapshot `limits` section. A window left as None is omitted (each is independently
    optional); pass limit_window(...) to include it. Pass the result as make_snapshot(limits=...).
    """
    out = {"captured_at": captured_at}
    if five_hour is not None:
        out["five_hour"] = five_hour
    if seven_day is not None:
        out["seven_day"] = seven_day
    return out


def agent_row(type, invocations, output=0) -> dict:
    """One `agents.last_7d` row, shaped as design.md "Snapshot" / snapshot-collect define it."""
    return {"type": type, "invocations": invocations, "output": output}


def make_agents(*rows) -> dict:
    """A snapshot `agents` section from agent_row(...) rows (kept in the order given; the
    collector sorts, but the hub must not rely on that). Pass as make_snapshot(agents=...)."""
    return {"last_7d": list(rows)}


def by_day_rows(first_day: str, outputs) -> list:
    """`tokens.by_day` entries for consecutive UTC dates starting at `first_day` (YYYY-MM-DD),
    one per value in `outputs`: {"day", "output", "input", "cache_creation", "cache_read"}."""
    import datetime

    start = datetime.date(int(first_day[:4]), int(first_day[5:7]), int(first_day[8:]))
    return [
        dict(day=(start + datetime.timedelta(days=i)).isoformat(), output=o,
             input=0, cache_creation=0, cache_read=0)
        for i, o in enumerate(outputs)
    ]


def make_tokens(by_day=None) -> dict:
    """The default `tokens` section with a chosen `by_day` list (default empty)."""
    return {
        "last_24h": tokens_window(412000, 91000, 1200000, 55000000),
        "last_7d": tokens_window(3100000, 640000, 9000000, 420000000),
        "by_day": list(by_day or []),
    }


def make_snapshot(
    host: str = "alpha",
    *,
    account: str = "default",
    collected_at: int = COLLECTED_AT,
    interval_s: int = 600,
    sessions=None,
    tokens=None,
    agents=None,
    limits=None,
    warnings=None,
    schema: int = 1
) -> dict:
    """A valid snapshot. Pass NULL for sessions/tokens/limits/agents to get JSON null.

    Defaults mirror the design.md example: 2 busy, 1 waiting, 412k output in 24h.
    """
    default_sessions = {"busy": 2, "waiting": 1, "idle": 0, "other": 0, "total": 3}
    default_tokens = {
        "last_24h": tokens_window(412000, 91000, 1200000, 55000000),
        "last_7d": tokens_window(3100000, 640000, 9000000, 420000000),
        "by_day": [],
    }
    return {
        "schema": schema,
        "agent": "claude",
        "agent_version": "0.0.0-test",
        "host": host,
        "account": account,
        "collected_at": collected_at,
        "interval_s": interval_s,
        "sessions": _pick(sessions, default_sessions),
        "tokens": _pick(tokens, default_tokens),
        "agents": _pick(agents, {"last_7d": []}),
        "limits": None if limits is NULL else limits,  # default null: no limits captured
        "warnings": warnings or [],
    }


def write_inbox_file(
    state_dir: str,
    name: str,
    obj: Union[dict, str, bytes],
    mtime: Optional[int] = None,
) -> str:
    """Write inbox/<name> (name includes any extension) with the chosen mtime.

    `obj` may be a dict (dumped as JSON) or raw str/bytes (to fabricate corrupt files).
    The mtime is set with os.utime because staleness is judged from it, not from the
    snapshot's own collected_at.
    """
    d = os.path.join(state_dir, "inbox")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    if isinstance(obj, dict):
        data = json.dumps(obj).encode("utf-8")
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
    else:
        data = obj
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def snapshot_tree(root: str) -> Dict[str, Tuple[bytes, int]]:
    """Every file under root as {relative path: (content, mtime_ns)}; plus dirs as None-content.

    Used to prove a code path is read-only: take one before, one after, compare.
    """
    out = {}  # type: Dict[str, Tuple[bytes, int]]
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        out[rel_dir + "/"] = (b"", 0)  # a created or removed directory shows up too
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (fh.read(), os.stat(p).st_mtime_ns)
    return out
