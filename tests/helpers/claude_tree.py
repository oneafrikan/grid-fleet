"""Synthetic Claude dir + fake `claude` executable for tests.

Nothing here touches the real ~/.claude, the real `claude` binary or the network.
All names, paths and ids are synthetic. Python 3.9, stdlib only.

Quick start
-----------
    from helpers.claude_tree import ClaudeTree, usage_event, agent_row, iso_z

    class T(unittest.TestCase):
        def setUp(self):
            self.tree = ClaudeTree()          # temp dir, removed by cleanup
            self.addCleanup(self.tree.cleanup)

        def test_x(self):
            now = 1792580580                  # use config.now() / GRID_FLEET_NOW in real tests
            self.tree.add_session("proj-a", "sess-1",
                [usage_event(now - 60, output=100, input=10)])
            self.tree.add_subagent("proj-a", "sess-1", "a1", "core-researcher",
                [usage_event(now - 30, output=50)])
            self.tree.set_agents([agent_row(status="busy"),
                                  agent_row(status="waiting"),
                                  agent_row(kind="background"),
                                  agent_row(pid=dead_pid())])
            env = self.tree.env()             # {GRID_FLEET_CLAUDE_DIR, GRID_FLEET_CLAUDE_BIN}
            # pass `env` into config.load_config(env) or patch os.environ

API
---
ClaudeTree(root=None)
    .root .claude_dir .projects_dir .bin_path          paths (strings)
    .env(extra=None) -> dict                           GRID_FLEET_CLAUDE_DIR + GRID_FLEET_CLAUDE_BIN
    .add_session(project, session_id, events) -> path  projects/<project>/<session_id>.jsonl
    .add_subagent(project, session_id, agent_id, agent_type, events, meta=True) -> path
                                                       .../<session_id>/subagents/agent-<id>.jsonl
                                                       + agent-<id>.meta.json {"agentType": ...}
                                                       (meta=False: no meta file; meta=<dict>: custom)
    .append_events(path, events)                       append complete JSONL lines
    .append_raw(path, text)                            append raw text (partial last line, corrupt line)
    .set_mtime(path, epoch)                            age a file (for the 8-day scan window)
    .set_agents(rows)                                  canned `claude agents --json` output
    .set_agents_mode("ok"|"fail"|"garbage"|"hang")     non-zero exit / non-JSON / sleep 30s
    .set_version("2.1.305")                            canned `claude --version`
    .calls() -> list of argv strings the fake was run with
    .missing_bin() -> a path that does not exist       for the "claude absent" case
    .cleanup()

Module functions
----------------
usage_event(ts, output=0, input=0, cache_creation=0, cache_read=0,
            message_id=None, noise=True, session_id=None) -> dict
    An assistant line. `ts` is epoch seconds (int) or an ISO string. With noise=True the line
    also carries cwd / sessionId / message text that the collector must NOT read or leak
    (SYNTH_CWD, SYNTH_SESSION_NAME, SYNTH_TEXT): privacy tests grep the snapshot for them.
user_event(ts)  -> a non-usage line (type "user")
iso_z(epoch)    -> "2026-10-21T14:03:00.000Z"
agent_row(kind="interactive", status="busy", pid=None) -> dict
    pid defaults to this test process (live). cwd/name/sessionId are synthetic strings.
    NOTE: the real row shape is only partly verified (spike 2.2 records the real keys);
    kind/status/pid are the fields the spec relies on, the rest is a guess.
dead_pid() -> int   a pid that was alive and has been reaped (os.kill(pid, 0) fails)
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable, List, Optional, Union

# Strings that must never appear in a snapshot (privacy contract). Distinctive on purpose.
SYNTH_CWD = "/synthetic/workdir/secret-project-cwd"
SYNTH_SESSION_NAME = "synthetic-secret-session-name"
SYNTH_TEXT = "synthetic secret transcript text"

_msg_counter = [0]


def iso_z(epoch: Union[int, float]) -> str:
    """Epoch seconds -> ISO-8601 UTC with ms and trailing Z (Claude's timestamp style)."""
    t = time.gmtime(int(epoch))
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + ".000Z"


def usage_event(ts, output=0, input=0, cache_creation=0, cache_read=0,
                message_id=None, noise=True, session_id=None) -> dict:
    if isinstance(ts, (int, float)):
        ts = iso_z(ts)
    if message_id is None:
        _msg_counter[0] += 1
        message_id = "msg_synth_%06d" % _msg_counter[0]
    ev = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": message_id,
            "usage": {
                "input_tokens": input,
                "output_tokens": output,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if noise:
        ev["cwd"] = SYNTH_CWD
        ev["sessionId"] = session_id or SYNTH_SESSION_NAME
        ev["message"]["content"] = [{"type": "text", "text": SYNTH_TEXT}]
    return ev


def user_event(ts, noise=True) -> dict:
    if isinstance(ts, (int, float)):
        ts = iso_z(ts)
    ev = {"type": "user", "timestamp": ts, "message": {"role": "user"}}
    if noise:
        ev["cwd"] = SYNTH_CWD
        ev["message"]["content"] = SYNTH_TEXT
    return ev


def agent_row(kind="interactive", status="busy", pid=None) -> dict:
    return {
        "kind": kind,
        "status": status,
        "pid": os.getpid() if pid is None else pid,
        "name": SYNTH_SESSION_NAME,
        "cwd": SYNTH_CWD,
        "sessionId": "synthetic-session-id-0001",
    }


def dead_pid() -> int:
    """A pid that is guaranteed not running right now: spawn, wait, reap."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# The fake claude is a POSIX sh script with its data directory baked in. Behaviour is
# driven by plain files in that directory so tests can change it between runs.
_FAKE_CLAUDE = """#!/bin/sh
D='%(data)s'
echo "$*" >> "$D/calls.log"
case "$1" in
  --version) cat "$D/version.txt"; exit 0 ;;
  agents)
    case "$(cat "$D/mode" 2>/dev/null)" in
      fail) echo "boom" >&2; exit 1 ;;
      garbage) echo "this is not json"; exit 0 ;;
      hang) exec sleep 30 ;;
      *) cat "$D/agents.json"; exit 0 ;;
    esac ;;
esac
exit 0
"""


class ClaudeTree:
    def __init__(self, root: Optional[str] = None):
        self._owns_root = root is None
        self.root = root or tempfile.mkdtemp(prefix="gf-test-")
        self.claude_dir = os.path.join(self.root, "claude")
        self.projects_dir = os.path.join(self.claude_dir, "projects")
        self._data = os.path.join(self.root, "fake-claude-data")
        self.bin_path = os.path.join(self.root, "bin", "claude")
        os.makedirs(self.projects_dir)
        os.makedirs(self._data)
        os.makedirs(os.path.dirname(self.bin_path))
        with open(self.bin_path, "w") as fh:
            fh.write(_FAKE_CLAUDE % {"data": self._data})
        os.chmod(self.bin_path, os.stat(self.bin_path).st_mode | stat.S_IXUSR)
        self.set_version("2.1.305")
        self.set_agents([])
        self.set_agents_mode("ok")

    # -- environment -----------------------------------------------------
    def env(self, extra: Optional[dict] = None) -> dict:
        e = {
            "GRID_FLEET_CLAUDE_DIR": self.claude_dir,
            "GRID_FLEET_CLAUDE_BIN": self.bin_path,
        }
        e.update(extra or {})
        return e

    def missing_bin(self) -> str:
        return os.path.join(self.root, "bin", "no-such-claude")

    # -- transcripts -----------------------------------------------------
    def _write(self, path: str, events: Iterable[dict]) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")
        return path

    def add_session(self, project: str, session_id: str, events: Iterable[dict]) -> str:
        return self._write(os.path.join(self.projects_dir, project, session_id + ".jsonl"), events)

    def add_subagent(self, project: str, session_id: str, agent_id: str, agent_type: str,
                     events: Iterable[dict], meta=True) -> str:
        d = os.path.join(self.projects_dir, project, session_id, "subagents")
        path = self._write(os.path.join(d, "agent-%s.jsonl" % agent_id), events)
        if meta is not False:
            body = meta if isinstance(meta, dict) else {"agentType": agent_type}
            with open(os.path.join(d, "agent-%s.meta.json" % agent_id), "w") as fh:
                json.dump(body, fh)
        return path

    def append_events(self, path: str, events: Iterable[dict]) -> None:
        with open(path, "a") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")

    def append_raw(self, path: str, text: str) -> None:
        with open(path, "a") as fh:
            fh.write(text)

    def set_mtime(self, path: str, epoch: Union[int, float]) -> None:
        os.utime(path, (epoch, epoch))

    # -- fake claude -----------------------------------------------------
    def set_agents(self, rows: List[dict]) -> None:
        with open(os.path.join(self._data, "agents.json"), "w") as fh:
            json.dump(rows, fh)

    def set_agents_mode(self, mode: str) -> None:
        assert mode in ("ok", "fail", "garbage", "hang"), mode
        with open(os.path.join(self._data, "mode"), "w") as fh:
            fh.write(mode)

    def set_version(self, version: str) -> None:
        with open(os.path.join(self._data, "version.txt"), "w") as fh:
            fh.write("%s (Claude Code)\n" % version)

    def calls(self) -> List[str]:
        try:
            with open(os.path.join(self._data, "calls.log")) as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []

    def cleanup(self) -> None:
        if self._owns_root:
            shutil.rmtree(self.root, ignore_errors=True)
