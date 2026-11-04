"""Snapshot build + schema constants, and the `collect` command.

Owner: Group 2 unit (grid-backend-dev).
Entry point used by cli.py: collect_main(config, args) -> int.

Reusable API (the push unit imports these to re-collect when the stored
snapshot is older than interval_s; design.md "Staleness"):

    collect(config, opener=open) -> dict
        Build a schema-1 snapshot. Never raises for Claude-side problems:
        a section that cannot be collected is None plus a warning.
        `opener` is injectable so tests can count transcript bytes read.
    write_snapshot(config, snap) -> str
        Atomic write (tmp + rename, mode 0600) of <state_dir>/snapshot.json.
        Returns the path. Raises OSError if it cannot be written.
    collect_and_write(config, opener=open) -> dict
        collect + write_snapshot; returns the snapshot. Raises OSError on write failure.
    snapshot_path(config) -> str

`agents` (group 6 leaderboard) is owned by a later group and stays None here.
`limits` is read from the local limits.json the statusLine tap writes (None when
never captured). `tokens.by_day` is filled now because it falls out
of the same hourly buckets.

Privacy: nothing in the snapshot, the one-line summary or any warning comes
from a transcript, cwd, session name, project directory or session id. See
sources_claude.py for the read allow-list.
"""

import os
import sys
from typing import Callable, List

from grid_fleet import config as cfg
from grid_fleet import limits as limits_mod
from grid_fleet import sources_claude

SCHEMA = 1
AGENT = "claude"
SNAPSHOT_FILE = "snapshot.json"


def snapshot_path(config) -> str:
    return os.path.join(config.state_dir, SNAPSHOT_FILE)


def collect(config, opener: Callable = open) -> dict:
    """Collect everything and assemble the snapshot dict (no file is written)."""
    now = cfg.now()  # the one clock; honours GRID_FLEET_NOW
    warnings = []  # type: List[str]

    version = sources_claude.get_version(config.claude_bin)
    vwarn = sources_claude.version_warning(version)
    if vwarn:
        warnings.append(vwarn)

    sessions, swarn = sources_claude.collect_sessions(config.claude_bin)
    warnings.extend(swarn)

    tokens = None
    projects = os.path.join(config.claude_dir, "projects")
    if not os.path.isdir(projects):
        # Unknown layout / never used Claude here: null, not zeros.
        warnings.append("claude projects directory not found")
    else:
        scan = sources_claude.scan_transcripts(projects, config.state_dir, now, opener=opener)
        warnings.extend(scan.warnings)
        if scan.matched_files == 0 and sessions is not None and sessions["busy"] > 0:
            # Sessions are running but we found no transcripts at all: the
            # layout has probably changed. Say so instead of reporting zeros.
            warnings.append("no transcripts found while sessions are busy")
        else:
            tokens = sources_claude.summarize_tokens(scan.entries, now)

    return {
        "schema": SCHEMA,
        "agent": AGENT,
        "agent_version": version,
        # Re-sanitise: the host becomes a remote file name, so it is enforced
        # here as well as at config load (a Config can be built directly).
        "host": cfg.sanitize_host_name(config.host_name),
        "account": config.account,
        "collected_at": now,
        "interval_s": config.interval_s,
        "sessions": sessions,
        "tokens": tokens,
        "agents": None,  # group 6
        # Copied from the tap's limits.json; None if absent or invalid (never zeros).
        "limits": limits_mod.read_limits(config),
        "warnings": warnings,
    }


def write_snapshot(config, snap: dict) -> str:
    path = snapshot_path(config)
    sources_claude.atomic_write_json(path, snap)
    return path


def collect_and_write(config, opener: Callable = open) -> dict:
    snap = collect(config, opener=opener)
    write_snapshot(config, snap)
    return snap


def summary_line(snap: dict) -> str:
    """One line, counts and sums only: no paths, names or ids."""
    s = snap["sessions"]
    t = snap["tokens"]
    sess = "n/a" if s is None else "%d (busy %d, waiting %d)" % (s["total"], s["busy"], s["waiting"])
    tok = "n/a" if t is None else "out 24h %d, 7d %d" % (t["last_24h"]["output"], t["last_7d"]["output"])
    return "collect: sessions %s; tokens %s; warnings %d" % (sess, tok, len(snap["warnings"]))


def collect_main(config, args) -> int:
    """`grid-fleet collect`. Exit 0 unless the snapshot could not be written."""
    snap = collect(config)
    try:
        write_snapshot(config, snap)
    except OSError as exc:
        # strerror only: str(exc) would include the path.
        sys.stderr.write("grid-fleet collect: cannot write snapshot (%s)\n" % (exc.strerror or "error"))
        return 1
    sys.stdout.write(summary_line(snap) + "\n")
    return 0
