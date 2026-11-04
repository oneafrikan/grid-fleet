"""Tests for `collect` (Group 2): sessions, token totals, incremental scan, fail-soft, privacy.

Scenario names follow openspec/changes/fleet-tracer-bullet/specs/snapshot-collect/spec.md.
NOT covered here on purpose (other groups): the tap itself (see test_limits.py). The `agents`
leaderboard and `tokens.by_day` (Group 6, collect side) are covered by TestAgentsLeaderboard
and TestByDay below. The `limits` section (read from the tap's limits.json) is covered by
TestLimitsSection below.

Everything runs against a synthetic Claude tree and a fake `claude` executable in temp
dirs, with GRID_FLEET_* overrides and a fixed clock. No test reads the real ~/.claude.
These are the author's own self-checks, not independent verification.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helpers.claude_tree import (
    SYNTH_CWD,
    SYNTH_SESSION_NAME,
    SYNTH_TEXT,
    ClaudeTree,
    agent_row,
    dead_pid,
    usage_event,
    user_event,
)

from grid_fleet import config as cfg
from grid_fleet import snapshot, sources_claude

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fixed clock: 2026-10-21T14:03:00Z, so the current UTC hour bucket is 14:00.
NOW = 1792591380
H = 3600
D = 86400


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


class CountingOpener(object):
    """open() replacement that records how many bytes each path actually had read from it.

    Lets the warm-run test assert "reads only the appended bytes" instead of trusting timing.
    """

    def __init__(self):
        self.bytes_read = {}  # path -> bytes returned by read()
        self.opened = []  # every path opened, in order

    def __call__(self, path, mode="r", *a, **kw):
        self.opened.append(path)
        return _CountingFile(open(path, mode, *a, **kw), self, path)


class _CountingFile(object):
    def __init__(self, fh, owner, path):
        self._fh, self._owner, self._path = fh, owner, path

    def read(self, *a):
        data = self._fh.read(*a)
        self._owner.bytes_read[self._path] = self._owner.bytes_read.get(self._path, 0) + len(data)
        return data

    def seek(self, *a):
        return self._fh.seek(*a)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False


class CollectBase(unittest.TestCase):
    def setUp(self):
        self.tree = ClaudeTree()
        self.addCleanup(self.tree.cleanup)
        self.work = tempfile.mkdtemp(prefix="gf-collect-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.state = os.path.join(self.work, "state")
        patcher = mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.make_config()

    def make_config(self, config_json=None, state=None, extra_env=None):
        """Config with every path steered into temp dirs; host pinned so the real hostname is unused."""
        body = {"schema": 1, "host_name": "alpha"}
        body.update(config_json or {})
        cfg_path = os.path.join(self.work, "config-%d.json" % len(os.listdir(self.work)))
        with open(cfg_path, "w") as fh:
            json.dump(body, fh)
        env = self.tree.env({"GRID_FLEET_STATE_DIR": state or self.state, "GRID_FLEET_CONFIG": cfg_path})
        env.update(extra_env or {})
        return cfg.load_config(env)

    def session(self, project, sid, events):
        path = self.tree.add_session(project, sid, events)
        self.tree.set_mtime(path, NOW)
        return path

    def subagent(self, project, sid, aid, agent_type, events, **kw):
        path = self.tree.add_subagent(project, sid, aid, agent_type, events, **kw)
        self.tree.set_mtime(path, NOW)
        return path

    def collect(self, config=None, **kw):
        return snapshot.collect(config or self.config, **kw)

    def offsets(self):
        with open(os.path.join(self.state, "offsets.json")) as fh:
            return json.load(fh)


def ev(age_s, **tokens):
    """A usage event `age_s` seconds before NOW."""
    return usage_event(NOW - age_s, **tokens)


class TestSnapshotContent(CollectBase):
    def test_full_snapshot(self):
        # Scenario: Full snapshot (agents is a later group and stays null; limits is null here
        # only because no limits.json exists in this fixture)
        self.session("proj-a", "sess-1", [ev(60, output=100, input=10, cache_creation=5, cache_read=1000)])
        self.subagent("proj-a", "sess-1", "a1", "core-researcher", [ev(30, output=50, input=2)])
        self.tree.set_agents([agent_row(status="busy"), agent_row(status="waiting")])
        self.tree.set_version("2.1.305")
        snap = self.collect()
        self.assertEqual(snap["schema"], 1)
        self.assertEqual(snap["agent"], "claude")
        self.assertEqual(snap["agent_version"], "2.1.305")
        self.assertEqual(snap["host"], "alpha")
        self.assertEqual(snap["account"], "default")
        self.assertEqual(snap["collected_at"], NOW)
        self.assertEqual(snap["interval_s"], 600)
        self.assertEqual(snap["sessions"], {"busy": 1, "waiting": 1, "idle": 0, "other": 0, "total": 2})
        t24 = snap["tokens"]["last_24h"]
        self.assertEqual(t24, {"output": 150, "input": 12, "cache_creation": 5, "cache_read": 1000})
        self.assertEqual(snap["tokens"]["last_7d"], t24)
        self.assertEqual(len(snap["tokens"]["by_day"]), 7)
        self.assertEqual(snap["warnings"], [])
        self.assertIn("agents", snap)
        self.assertIn("limits", snap)
        self.assertEqual(snap["agents"], {"last_7d": [
            {"type": "main", "invocations": 1, "output": 100},  # tie on invocations: output desc
            {"type": "core-researcher", "invocations": 1, "output": 50},
        ]})
        self.assertIsNone(snap["limits"])  # no limits.json captured

    def test_section_unavailable_projects_dir_missing(self):
        # Scenario: Section unavailable (exit code 0 via collect_main)
        shutil.rmtree(self.tree.projects_dir)
        snap = self.collect()
        self.assertIsNone(snap["tokens"])
        self.assertIsNone(snap["agents"])
        self.assertTrue(snap["warnings"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = snapshot.collect_main(self.config, None)
        self.assertEqual(rc, 0)

    def test_busy_sessions_but_no_transcripts_is_null_not_zero(self):
        # design.md: "zero matching files while sessions are busy" => tokens null + warning
        self.tree.set_agents([agent_row(status="busy")])
        snap = self.collect()
        self.assertIsNone(snap["tokens"])
        self.assertIsNone(snap["agents"])  # null together with tokens, never an empty list
        self.assertTrue(any("no transcripts" in w for w in snap["warnings"]))

    def test_idle_host_with_no_transcripts_reports_zeros(self):
        snap = self.collect()
        self.assertEqual(snap["tokens"]["last_7d"]["output"], 0)
        self.assertEqual(len(snap["tokens"]["by_day"]), 7)
        self.assertEqual(snap["agents"], {"last_7d": []})  # collected, nobody invoked: empty, not null

    def test_host_name_is_safe_for_use_as_a_file_name(self):
        # Scenario: Host name is safe (via config file, and via a hand-built Config)
        config = self.make_config({"host_name": "../etc/pa sséwd" + "x" * 100})
        host = self.collect(config)["host"]
        self.assertRegex(host, r"^[A-Za-z0-9._-]+$")
        self.assertLessEqual(len(host), 64)
        self.assertFalse(host.startswith("."))
        raw = cfg.Config(host_name="..hidden/one", state_dir=self.state, claude_dir=self.tree.claude_dir,
                         claude_bin=self.tree.bin_path)
        host = snapshot.collect(raw)["host"]
        self.assertRegex(host, r"^[A-Za-z0-9._-]+$")
        self.assertFalse(host.startswith("."))

    def test_account_and_interval_come_from_config(self):
        snap = self.collect(self.make_config({"account": "team-x", "interval_s": 300}))
        self.assertEqual((snap["account"], snap["interval_s"]), ("team-x", 300))


class TestSessions(CollectBase):
    def test_busy_and_waiting_split(self):
        # Scenario: Busy and waiting split
        self.tree.set_agents([agent_row(status="busy"), agent_row(status="busy"), agent_row(status="waiting")])
        self.assertEqual(self.collect()["sessions"],
                         {"busy": 2, "waiting": 1, "idle": 0, "other": 0, "total": 3})

    def test_stale_background_rows_ignored(self):
        # Scenario: Stale background rows ignored (background kind, and a dead pid)
        self.tree.set_agents([agent_row(kind="background"), agent_row(pid=dead_pid()), agent_row(status="idle")])
        self.assertEqual(self.collect()["sessions"],
                         {"busy": 0, "waiting": 0, "idle": 1, "other": 0, "total": 1})

    def test_unknown_status_counts_as_other(self):
        # Scenario: Unknown status
        self.tree.set_agents([agent_row(status="frobnicating"), agent_row(status=None)])
        self.assertEqual(self.collect()["sessions"],
                         {"busy": 0, "waiting": 0, "idle": 0, "other": 2, "total": 2})

    def test_pid_zero_or_negative_is_never_live(self):
        # os.kill(0, 0) would "succeed" (signals our process group): must be rejected explicitly
        self.tree.set_agents([agent_row(pid=0), agent_row(pid=-1), agent_row(pid=True)])
        self.assertEqual(self.collect()["sessions"]["total"], 0)

    def test_permission_error_means_alive(self):
        self.tree.set_agents([agent_row(pid=424242)])
        with mock.patch("grid_fleet.sources_claude.os.kill", side_effect=PermissionError):
            self.assertEqual(self.collect()["sessions"]["total"], 1)

    def test_non_dict_rows_are_skipped(self):
        self.tree.set_agents(["junk", 7, None, agent_row()])
        self.assertEqual(self.collect()["sessions"]["total"], 1)

    def test_command_unavailable_missing_binary(self):
        # Scenario: Command unavailable (binary missing)
        config = self.make_config(extra_env={"GRID_FLEET_CLAUDE_BIN": self.tree.missing_bin()})
        snap = self.collect(config)
        self.assertIsNone(snap["sessions"])
        self.assertTrue(snap["warnings"])
        self.assertIsNone(snap["agent_version"])

    def test_command_unavailable_nonzero_exit(self):
        self.tree.set_agents_mode("fail")
        snap = self.collect()
        self.assertIsNone(snap["sessions"])
        self.assertTrue(snap["warnings"])

    def test_command_unavailable_non_json(self):
        self.tree.set_agents_mode("garbage")
        snap = self.collect()
        self.assertIsNone(snap["sessions"])
        self.assertTrue(snap["warnings"])

    def test_command_unavailable_json_but_not_a_list(self):
        with open(os.path.join(self.tree._data, "agents.json"), "w") as fh:
            fh.write('{"not": "a list"}')
        self.assertIsNone(self.collect()["sessions"])

    def test_command_unavailable_timeout(self):
        # Scenario: Command unavailable (takes too long). The 10 s limit is a module constant;
        # shortened here so the test stays fast. The fake sleeps 30 s in "hang" mode.
        self.tree.set_agents_mode("hang")
        started = time.time()
        with mock.patch.object(sources_claude, "AGENTS_TIMEOUT_S", 0.5):
            snap = self.collect()
        self.assertLess(time.time() - started, 10)
        self.assertIsNone(snap["sessions"])
        self.assertTrue(any("timed out" in w for w in snap["warnings"]))

    def test_default_timeout_is_ten_seconds(self):
        self.assertEqual(sources_claude.AGENTS_TIMEOUT_S, 10)

    def test_agents_called_with_json_flag_only(self):
        self.collect()
        self.assertIn("agents --json", self.tree.calls())


class TestVersion(CollectBase):
    def test_parse_version_tolerates_both_forms(self):
        self.assertEqual(sources_claude.parse_version("2.1.305"), "2.1.305")
        self.assertEqual(sources_claude.parse_version("2.1.305\n"), "2.1.305")
        self.assertEqual(sources_claude.parse_version("2.1.305 (Claude Code)\n"), "2.1.305")
        self.assertIsNone(sources_claude.parse_version("no version here"))
        self.assertIsNone(sources_claude.parse_version(None))

    def test_tested_list_is_seeded_with_verified_version(self):
        self.assertIn("2.1.305", sources_claude.TESTED_VERSIONS)

    def test_untested_version_warns_but_still_collects(self):
        # Scenario: Untested version
        self.tree.set_version("9.9.9")
        self.session("proj-a", "sess-1", [ev(60, output=7)])
        snap = self.collect()
        self.assertEqual(snap["agent_version"], "9.9.9")
        self.assertEqual(snap["warnings"], ["claude 9.9.9 not in tested versions"])
        self.assertEqual(snap["tokens"]["last_24h"]["output"], 7)

    def test_tested_version_has_no_warning(self):
        self.assertEqual(self.collect()["warnings"], [])


class TestTokenTotals(CollectBase):
    def setUp(self):
        super().setUp()
        # Pin the process zone far from UTC so these tests prove the bucketing is UTC
        # on any host; with a UTC host zone a local-time bug would go unnoticed.
        old = os.environ.get("TZ")
        self.addCleanup(self._restore_tz, old)
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()

    @staticmethod
    def _restore_tz(old):
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_window_boundaries(self):
        # Scenario: Window boundaries - a 25 h old event is in last_7d, not last_24h
        self.session("proj-a", "sess-1", [
            ev(1 * H, output=1),
            ev(23 * H, output=10),
            ev(24 * H, output=20000),  # exactly 24 h old: just outside the 24 whole-hour buckets
            ev(25 * H, output=100),
            ev(6 * D, output=1000),
            ev(int(7.5 * D), output=10000),  # stored (inside the 8-day horizon) but outside 7d
        ])
        t = self.collect()["tokens"]
        self.assertEqual(t["last_24h"]["output"], 1 + 10)
        self.assertEqual(t["last_7d"]["output"], 1 + 10 + 20000 + 100 + 1000)

    def test_last_7d_edge_is_exact_to_the_hour_bucket(self):
        # NOW is 14:03Z, so last_7d = 168 whole-hour buckets starting 167 h before the
        # 14:00 bucket. The oldest bucket's first second is in; the second before is out.
        oldest_start_age = 167 * H + (NOW % H)  # age of the first second of the oldest bucket
        self.session("proj-a", "sess-1", [
            ev(oldest_start_age, output=1),      # first second of the oldest bucket: included
            ev(oldest_start_age + 1, output=10),  # last second of the bucket before: excluded
        ])
        t = self.collect()["tokens"]
        self.assertEqual(t["last_7d"]["output"], 1)
        self.assertEqual(t["last_24h"]["output"], 0)

    def test_by_day_is_seven_utc_dates_ending_today(self):
        self.session("proj-a", "sess-1", [ev(1 * H, output=1), ev(25 * H, output=100), ev(6 * D, output=1000)])
        by_day = self.collect()["tokens"]["by_day"]
        self.assertEqual([r["day"] for r in by_day], [day_of(NOW - i * D) for i in range(6, -1, -1)])
        by = {r["day"]: r["output"] for r in by_day}
        self.assertEqual(by[day_of(NOW)], 1)
        self.assertEqual(by[day_of(NOW - 25 * H)], 100)
        self.assertEqual(by[day_of(NOW - 6 * D)], 1000)
        for row in by_day:
            self.assertEqual(set(row), {"day", "output", "input", "cache_creation", "cache_read"})

    def test_offset_timestamps_are_bucketed_in_utc(self):
        # 15:03+02:00 is 13:03Z
        self.assertEqual(sources_claude._ts_to_hour_key("2026-10-21T15:03:00.000+02:00"), "2026-10-21T13")
        self.assertEqual(sources_claude._ts_to_hour_key("2026-10-21T13:03:00.000Z"), "2026-10-21T13")
        self.assertIsNone(sources_claude._ts_to_hour_key("yesterday"))
        self.assertIsNone(sources_claude._ts_to_hour_key(None))

    def test_duplicate_usage_lines_count_once(self):
        # Scenario: Duplicate usage lines - streaming chunks of one message keep the LAST
        chunks = [ev(60, output=10, message_id="m1"), ev(59, output=50, message_id="m1"),
                  ev(58, output=120, message_id="m1"), ev(30, output=7, message_id="m2")]
        self.session("proj-a", "sess-1", chunks)
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 127)

    def test_duplicate_split_across_runs_still_counts_once(self):
        # A chunk run straddling two collect runs must equal a from-scratch recount.
        path = self.session("proj-a", "sess-1", [ev(60, output=10, message_id="m1")])
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 10)
        self.tree.append_events(path, [ev(59, output=50, message_id="m1"), ev(58, output=120, message_id="m1")])
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 120)
        fresh = self.make_config(state=os.path.join(self.work, "fresh-state"))
        self.assertEqual(self.collect(fresh)["tokens"], self.collect()["tokens"])

    def test_lines_without_usage_or_not_assistant_are_ignored(self):
        self.session("proj-a", "sess-1", [user_event(NOW - 60), ev(30, output=5)])
        self.tree.append_raw(os.path.join(self.tree.projects_dir, "proj-a", "sess-1.jsonl"),
                             json.dumps({"type": "user", "timestamp": "2026-10-21T14:00:00.000Z",
                                         "message": {"usage": {"output_tokens": 999}}}) + "\n")
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 5)

    def test_subagent_tokens_included(self):
        # Scenario: Subagent tokens included
        self.session("proj-a", "sess-1", [ev(60, output=100)])
        self.subagent("proj-a", "sess-1", "a1", "core-researcher", [ev(30, output=50)])
        self.subagent("proj-a", "sess-1", "a2", "grid-backend-dev", [ev(20, output=25)])
        self.assertEqual(self.collect()["tokens"]["last_7d"]["output"], 175)

    def test_agent_label_stored_per_file_for_the_leaderboard_group(self):
        self.session("proj-a", "sess-1", [ev(60, output=1)])
        self.subagent("proj-a", "sess-1", "a1", "core-researcher", [ev(30, output=1)])
        self.subagent("proj-a", "sess-1", "a2", None, [ev(30, output=1)], meta=False)
        self.subagent("proj-a", "sess-1", "a3", None, [ev(30, output=1)], meta={"agentType": ["not", "str"]})
        self.collect()
        labels = sorted(e["agent"] for e in self.offsets()["files"].values())
        self.assertEqual(labels, ["core-researcher", "main", "unknown", "unknown"])

    def test_files_older_than_eight_days_are_not_scanned(self):
        path = self.session("proj-a", "old", [ev(60, output=500)])
        self.tree.set_mtime(path, NOW - 9 * D)
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 0)


def pin_tz(testcase, zone):
    """Run the rest of `testcase` with the process zone set to `zone`, restoring it afterwards.

    Used to prove day/hour bucketing is UTC on any host: with a UTC host zone a local-time bug
    would go unnoticed.
    """
    old = os.environ.get("TZ")

    def restore():
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    testcase.addCleanup(restore)
    os.environ["TZ"] = zone
    time.tzset()


def labels(snap):
    """{type: (invocations, output)} from a snapshot's leaderboard."""
    return {r["type"]: (r["invocations"], r["output"]) for r in snap["agents"]["last_7d"]}


# Label rule from sources_claude._sanitize_label: printable ASCII subset, 1..64 chars.
SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class TestAgentsLeaderboard(CollectBase):
    """Scenarios under 'Subagent leaderboard' (spec: snapshot-collect) plus the hostile-meta cases."""

    def setUp(self):
        super().setUp()
        pin_tz(self, "America/Los_Angeles")
        self._n = 0

    @staticmethod
    def meta_path(transcript):
        return transcript[: -len(".jsonl")] + ".meta.json"

    def hostile(self, raw_bytes, output=1):
        """Add a subagent (own session, so every call is independent) whose meta file is exactly
        `raw_bytes`; returns the transcript path."""
        self._n += 1
        path = self.subagent("p", "hs%d" % self._n, "h%d" % self._n, None, [ev(60, output=output)], meta=False)
        with open(self.meta_path(path), "wb") as fh:
            fh.write(raw_bytes)
        return path

    def test_main_counts_as_an_agent(self):
        # Scenario: Main counts as an agent
        self.session("proj-a", "s1", [ev(60, output=10)])
        self.session("proj-a", "s2", [ev(60, output=20)])
        self.subagent("proj-a", "s1", "a1", "core-researcher", [ev(30, output=5)])
        agents = self.collect()["agents"]
        self.assertEqual(agents, {"last_7d": [
            {"type": "main", "invocations": 2, "output": 30},
            {"type": "core-researcher", "invocations": 1, "output": 5},
        ]})

    def test_sorted_by_invocations_descending(self):
        # Scenario: Sorted output
        self.session("p", "s1", [ev(60, output=1)])
        for i in range(3):
            self.subagent("p", "s1", "r%d" % i, "core-researcher", [ev(60, output=1)])
        for i in range(2):
            self.subagent("p", "s1", "b%d" % i, "grid-backend-dev", [ev(60, output=1)])
        types = [r["type"] for r in self.collect()["agents"]["last_7d"]]
        self.assertEqual(types, ["core-researcher", "grid-backend-dev", "main"])

    def test_ties_break_by_output_then_type_and_are_stable(self):
        # The spec has no tie-break; ours is output desc, then type asc, so runs are deterministic.
        self.subagent("p", "s1", "a", "zeta", [ev(60, output=5)])
        self.subagent("p", "s1", "b", "alpha", [ev(60, output=5)])
        self.subagent("p", "s1", "c", "mid", [ev(60, output=9)])
        first = self.collect()["agents"]["last_7d"]
        self.assertEqual([r["type"] for r in first], ["mid", "alpha", "zeta"])
        self.assertEqual(self.collect()["agents"]["last_7d"], first)

    def test_meta_file_missing_is_reported_as_unknown(self):
        # Scenario: Meta file missing
        self.subagent("p", "s1", "a1", None, [ev(60, output=7)], meta=False)
        self.assertEqual(labels(self.collect()), {"unknown": (1, 7)})

    def test_invocations_are_transcripts_not_events(self):
        # Design: one transcript with many usage events is one invocation.
        self.subagent("p", "s1", "a1", "core-researcher", [ev(60 * i, output=1) for i in range(1, 6)])
        self.assertEqual(labels(self.collect()), {"core-researcher": (1, 5)})

    def test_transcript_without_usage_in_window_is_not_an_invocation(self):
        self.subagent("p", "s1", "none", "core-researcher", [user_event(NOW - 60)])  # no usage line at all
        self.subagent("p", "s1", "old", "core-researcher", [ev(int(7.5 * D), output=99)])  # outside 7d
        self.assertEqual(self.collect()["agents"], {"last_7d": []})

    def test_cross_window_only_in_window_output_counts(self):
        # A transcript straddling the window edge is ONE invocation with only its in-window output.
        oldest_start_age = 167 * H + (NOW % H)  # age of the first second of the oldest 7d bucket
        self.subagent("p", "s1", "edge", "core-researcher", [
            ev(oldest_start_age, output=1),       # in
            ev(oldest_start_age + 1, output=10),  # one second too old: out
            ev(int(7.5 * D), output=100),         # out
        ])
        self.subagent("p", "s1", "gone", "core-researcher", [ev(oldest_start_age + 1, output=1000)])  # all out
        self.assertEqual(labels(self.collect()), {"core-researcher": (1, 1)})

    def test_leaderboard_output_sums_to_last_7d_tokens(self):
        # Invariant: both come from the same buckets, so total output must agree.
        self.session("p", "s1", [ev(60, output=100), ev(3 * D, output=40), ev(7 * D + H, output=9)])
        self.subagent("p", "s1", "a1", "core-researcher", [ev(2 * H, output=11)])
        self.subagent("p", "s1", "a2", None, [ev(5 * D, output=3)], meta=False)
        snap = self.collect()
        self.assertEqual(sum(r["output"] for r in snap["agents"]["last_7d"]), snap["tokens"]["last_7d"]["output"])

    def test_deleted_transcript_counts_until_it_ages_out(self):
        path = self.subagent("p", "s1", "a1", "core-researcher", [ev(2 * D, output=30)])
        self.assertEqual(labels(self.collect()), {"core-researcher": (1, 30)})
        os.remove(path)
        os.remove(self.meta_path(path))
        self.assertEqual(labels(self.collect()), {"core-researcher": (1, 30)})
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW + 9 * D)}):
            self.assertEqual(self.collect()["agents"], {"last_7d": []})

    def test_warm_run_reads_only_appended_bytes_and_matches_a_recount(self):
        main = self.session("p", "s1", [ev(600, output=10) for _ in range(5)])
        sub = self.subagent("p", "s1", "a1", "core-researcher", [ev(600, output=3) for _ in range(5)])
        self.collect()
        appended = [ev(60, output=2) for _ in range(40)]
        self.tree.append_events(sub, appended)
        new_bytes = sum(len(json.dumps(e)) + 1 for e in appended)
        counter = CountingOpener()
        warm = self.collect(opener=counter)
        self.assertEqual(counter.bytes_read.get(sub), new_bytes)  # only the appended bytes
        self.assertNotIn(main, counter.opened)  # unchanged transcript: not opened
        # The meta file is read once, when the entry is created, never on a warm run.
        self.assertFalse([p for p in counter.opened if p.endswith(".meta.json")])
        fresh = self.make_config(state=os.path.join(self.work, "fresh-state"))
        self.assertEqual(warm["agents"], self.collect(fresh)["agents"])
        self.assertEqual(labels(warm), {"main": (1, 50), "core-researcher": (1, 15 + 80)})

    def test_new_transcript_between_runs_adds_an_invocation(self):
        self.session("p", "s1", [ev(60, output=1)])
        self.assertEqual(labels(self.collect()), {"main": (1, 1)})
        self.subagent("p", "s1", "a1", "core-researcher", [ev(30, output=2)])
        self.assertEqual(labels(self.collect()), {"main": (1, 1), "core-researcher": (1, 2)})

    def test_late_meta_file_moves_invocation_out_of_unknown(self):
        path = self.subagent("p", "s1", "a1", None, [ev(60, output=4)], meta=False)
        self.assertEqual(labels(self.collect()), {"unknown": (1, 4)})
        with open(self.meta_path(path), "w") as fh:
            json.dump({"agentType": "core-researcher"}, fh)
        self.assertEqual(labels(self.collect()), {"core-researcher": (1, 4)})

    def test_old_offsets_cache_without_needed_fields_is_rebuilt_not_trusted(self):
        # The leaderboard needs no new cache field (it reads `agent` + `hours`, both in schema 1
        # since Group 2). A cache missing them must rebuild, never crash or miscount.
        self.session("p", "s1", [ev(60, output=10)])
        self.subagent("p", "s1", "a1", "core-researcher", [ev(30, output=5)])
        want = self.collect()["agents"]
        old_shapes = [
            {"offset": 5, "size": 5, "hours": {"2026-10-21T14": [1, 0, 0, 0]}},                 # no agent
            {"offset": 5, "size": 5, "agent": "main"},                                           # no hours
            {"offset": 5, "size": 5, "agent": "main", "hours": {"2026-10-21T14": [1, 0, 0]}},    # short vector
        ]
        for i, ent in enumerate(old_shapes):
            with self.subTest(shape=i):
                with open(os.path.join(self.state, "offsets.json"), "w") as fh:
                    json.dump({"schema": 1, "files": {"p/s1.jsonl": ent}}, fh)
                self.assertEqual(self.collect()["agents"], want)  # rebuilt: correct counts
                self.assertEqual(self.offsets()["schema"], 1)

    def test_agents_null_with_tokens_when_projects_dir_missing(self):
        shutil.rmtree(self.tree.projects_dir)
        snap = self.collect()
        self.assertIsNone(snap["agents"])
        self.assertIsNone(snap["tokens"])

    # -- privacy ------------------------------------------------------------

    def test_decoy_names_never_reach_the_agents_section(self):
        # Types come from meta `agentType` only. Project dir, session id and agent id decoys,
        # and extra identifying meta fields, must not appear anywhere in the snapshot.
        proj, sid, aid = "decoyproj-dirname-q1", "decoy-session-id-9", "decoyagent77"
        self.session(proj, sid, [ev(60, output=1, session_id=sid)])
        self.subagent(proj, sid, aid, None, [ev(30, output=2, session_id=sid)],
                      meta={"agentType": "core-researcher", "cwd": SYNTH_CWD,
                            "name": SYNTH_SESSION_NAME, "description": SYNTH_TEXT, "sessionId": sid})
        self.subagent(proj, sid, "decoyagent78", None, [ev(30, output=2)], meta=False)
        snap = self.collect()
        text = json.dumps(snap)
        for s in (proj, sid, aid, "decoyagent78", SYNTH_CWD, SYNTH_SESSION_NAME, SYNTH_TEXT):
            self.assertNotIn(s, text)
        self.assertEqual({r["type"] for r in snap["agents"]["last_7d"]}, {"main", "core-researcher", "unknown"})

    def test_agent_labels_come_only_from_agenttype(self):
        # A meta without agentType but with other string fields must not borrow any of them.
        self.subagent("p", "s1", "a1", None, [ev(60, output=1)], meta={"name": "leaky-name", "type": "leaky"})
        self.assertEqual(labels(self.collect()), {"unknown": (1, 1)})

    # -- hostile meta files -------------------------------------------------

    def test_non_json_wrong_type_and_empty_meta_become_unknown(self):
        bodies = [b"", b"not json {{{", b"\xff\xfe\x00garbage", b"null", b"[]", b"42", b"true", b'"str"',
                  b'{"agentType": 7}', b'{"agentType": null}', b'{"agentType": ["a"]}',
                  b'{"agentType": {"x": 1}}', b'{"agentType": ""}', b'{"agentType": true}',
                  b"[" * 100000]  # nesting bomb: must not crash with RecursionError
        for body in bodies:
            self.hostile(body)
        self.assertEqual(labels(self.collect()), {"unknown": (len(bodies), len(bodies))})

    def test_huge_agenttype_is_truncated_to_64_chars(self):
        self.hostile(json.dumps({"agentType": "a" * 5000}).encode())
        self.assertEqual(labels(self.collect()), {"a" * 64: (1, 1)})

    def test_oversized_meta_file_is_not_read_in_full(self):
        # A hostile multi-MB "meta" file: bounded read, reported as unknown.
        path = self.hostile(json.dumps({"agentType": "core-researcher", "pad": "x" * (2 * 1024 * 1024)}).encode())
        counter = CountingOpener()
        snap = self.collect(opener=counter)
        self.assertEqual(labels(snap), {"unknown": (1, 1)})
        self.assertLessEqual(counter.bytes_read[self.meta_path(path)], sources_claude._META_MAX_BYTES + 1)

    def test_control_characters_and_non_ascii_are_sanitised(self):
        nasty = "evil\x00\x1b[31m\n\r\t name/../‮é\U0001f600 ok"
        self.hostile(json.dumps({"agentType": nasty}).encode())
        types = [r["type"] for r in self.collect()["agents"]["last_7d"]]
        self.assertEqual(len(types), 1)
        self.assertRegex(types[0], SAFE_LABEL)
        self.assertTrue(types[0].startswith("evil"))

    def test_meta_that_is_a_directory_becomes_unknown(self):
        path = self.subagent("p", "s1", "a1", None, [ev(60, output=1)], meta=False)
        os.makedirs(self.meta_path(path))
        self.assertEqual(labels(self.collect()), {"unknown": (1, 1)})

    def test_unsafe_label_in_a_tampered_cache_is_sanitised_on_output(self):
        # The cache is a plain file: a hand-edited label must still not reach the snapshot raw.
        self.subagent("p", "s1", "a1", "core-researcher", [ev(60, output=1)])
        self.collect()
        cache = self.offsets()
        for ent in cache["files"].values():
            ent["agent"] = "bad\x1b[0m/" + "z" * 200
        with open(os.path.join(self.state, "offsets.json"), "w") as fh:
            json.dump(cache, fh)
        types = [r["type"] for r in self.collect()["agents"]["last_7d"]]
        self.assertEqual(len(types), 1)
        self.assertRegex(types[0], SAFE_LABEL)


class TestByDay(CollectBase):
    """`tokens.by_day`: seven UTC-date entries derived from the hourly buckets."""

    UTC_MIDNIGHT = NOW - (14 * H + 180)  # 2026-10-21T00:00:00Z

    def reset_tree(self):
        """Empty projects dir and cache, for subTests that re-run under another zone."""
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.tree.projects_dir)
        os.makedirs(self.tree.projects_dir)

    def test_shape_is_seven_ascending_dates_with_zero_filled_gaps(self):
        self.session("p", "s1", [ev(60, output=5, input=1, cache_creation=2, cache_read=3)])
        by_day = self.collect()["tokens"]["by_day"]
        self.assertEqual(len(by_day), 7)
        self.assertEqual([r["day"] for r in by_day], [day_of(NOW - i * D) for i in range(6, -1, -1)])
        for row in by_day[:-1]:
            self.assertEqual(row, {"day": row["day"], "output": 0, "input": 0, "cache_creation": 0, "cache_read": 0})
        self.assertEqual(by_day[-1], {"day": day_of(NOW), "output": 5, "input": 1, "cache_creation": 2, "cache_read": 3})

    def test_utc_date_derivation_in_non_utc_zones(self):
        # 23:30Z on the 20th and 00:30Z on the 21st share ONE local date in Los Angeles (the 20th)
        # and in Auckland (the 21st), but are on two different UTC dates. by_day must follow UTC.
        for zone in ("America/Los_Angeles", "Pacific/Auckland", "Asia/Kolkata"):
            with self.subTest(zone=zone):
                pin_tz(self, zone)
                self.reset_tree()
                self.session("p", "s1", [usage_event(self.UTC_MIDNIGHT - 30 * 60, output=7),   # 10-20T23:30Z
                                         usage_event(self.UTC_MIDNIGHT + 30 * 60, output=11)])  # 10-21T00:30Z
                by = {r["day"]: r["output"] for r in self.collect()["tokens"]["by_day"]}
                self.assertEqual(by["2026-10-20"], 7)
                self.assertEqual(by["2026-10-21"], 11)

    def test_today_is_the_utc_date_even_when_local_date_differs(self):
        # 00:30Z on the 22nd is still the 21st in Los Angeles; the last entry must be the 22nd.
        pin_tz(self, "America/Los_Angeles")
        now2 = self.UTC_MIDNIGHT + D + 30 * 60
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(now2)}):
            self.session("p", "s1", [usage_event(now2 - 60, output=3)])
            by_day = self.collect()["tokens"]["by_day"]
        self.assertEqual(by_day[-1]["day"], "2026-10-22")
        self.assertEqual(by_day[-1]["output"], 3)
        self.assertEqual(by_day[0]["day"], "2026-10-16")

    def test_by_day_is_calendar_dates_so_it_differs_from_last_7d(self):
        # Spec: last_7d is 168 hour buckets, by_day is 7 calendar dates, so they need not agree.
        # 2026-10-14T15:00Z is inside the 168 buckets but before the first by_day date (10-15).
        epoch = self.UTC_MIDNIGHT - 7 * D + 15 * H
        self.session("p", "s1", [usage_event(epoch, output=100), ev(60, output=1)])
        t = self.collect()["tokens"]
        self.assertEqual(t["last_7d"]["output"], 101)
        self.assertEqual(sum(r["output"] for r in t["by_day"]), 1)
        self.assertNotIn("2026-10-14", [r["day"] for r in t["by_day"]])

    def test_by_day_warm_run_equals_recount(self):
        path = self.session("p", "s1", [ev(60, output=1), ev(2 * D, output=2)])
        self.collect()
        self.tree.append_events(path, [ev(30, output=4), ev(3 * D, output=8)])
        warm = self.collect()["tokens"]["by_day"]
        fresh = self.make_config(state=os.path.join(self.work, "fresh-state"))
        self.assertEqual(warm, self.collect(fresh)["tokens"]["by_day"])


class TestIncrementalScan(CollectBase):
    def test_warm_run_reads_only_new_bytes(self):
        # Scenario: Warm run reads only new bytes
        a = self.session("proj-a", "sess-1", [ev(600, output=100) for _ in range(5)])
        b = self.session("proj-a", "sess-2", [ev(600, output=1) for _ in range(5)])
        self.collect()  # cold: builds the cache
        appended = [ev(60, output=20) for _ in range(4)]
        self.tree.append_events(a, appended)
        new_bytes = sum(len(json.dumps(e)) + 1 for e in appended)
        self.assertGreater(new_bytes, 1000)  # ~1 KiB as in the scenario

        counter = CountingOpener()
        warm = self.collect(opener=counter)
        # Only the appended bytes of the one changed transcript were read; the unchanged one
        # was not even opened.
        self.assertEqual(counter.bytes_read.get(a), new_bytes)
        self.assertNotIn(b, counter.opened)
        # ...and the totals equal a from-scratch recount.
        fresh = self.make_config(state=os.path.join(self.work, "fresh-state"))
        self.assertEqual(warm["tokens"], self.collect(fresh)["tokens"])
        self.assertEqual(warm["tokens"]["last_24h"]["output"], 500 + 5 + 80)

    def test_unchanged_run_opens_no_transcripts(self):
        self.session("proj-a", "sess-1", [ev(60, output=1)])
        self.collect()
        counter = CountingOpener()
        self.collect(opener=counter)
        self.assertFalse([p for p in counter.opened if p.endswith(".jsonl")])

    def test_partial_last_line_not_counted_until_complete(self):
        # Scenario: Partial last line
        path = self.session("proj-a", "sess-1", [ev(60, output=10)])
        line = json.dumps(ev(30, output=40, message_id="m-late"))
        self.tree.append_raw(path, line[:25])  # half a line, no newline
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 10)
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 10)  # still not counted, no warning
        self.assertEqual(self.collect()["warnings"], [])
        self.tree.append_raw(path, line[25:] + "\n")  # now complete
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 50)
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 50)  # no double count

    def test_truncated_transcript_recounted_without_double_counting(self):
        # Scenario: Truncated transcript
        path = self.session("proj-a", "sess-1", [ev(60, output=100), ev(50, output=100), ev(40, output=100)])
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 300)
        with open(path, "w") as fh:  # rewritten smaller
            fh.write(json.dumps(ev(30, output=7)) + "\n")
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 7)
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 7)

    def test_cleaned_up_log_still_counts_until_it_ages_out(self):
        # Scenario: Cleaned-up log
        path = self.session("proj-a", "sess-1", [ev(2 * D, output=300)])
        self.assertEqual(self.collect()["tokens"]["last_7d"]["output"], 300)
        os.remove(path)
        self.assertEqual(self.collect()["tokens"]["last_7d"]["output"], 300)
        # Ageing: 9 days later the hours are past the 8-day horizon and the entry is pruned.
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW + 9 * D)}):
            self.assertEqual(self.collect()["tokens"]["last_7d"]["output"], 0)
        self.assertEqual(self.offsets()["files"], {})

    def test_cache_lost_missing(self):
        # Scenario: Cache lost (missing)
        self.session("proj-a", "sess-1", [ev(60, output=11)])
        self.subagent("proj-a", "sess-1", "a1", "x", [ev(60, output=22)])
        before = self.collect()["tokens"]
        os.remove(os.path.join(self.state, "offsets.json"))
        self.assertEqual(self.collect()["tokens"], before)
        self.assertEqual(before["last_24h"]["output"], 33)

    def test_cache_lost_corrupt_variants(self):
        # Scenario: Cache lost (corrupt) - garbage, wrong schema, wrong shapes: all rebuild
        self.session("proj-a", "sess-1", [ev(60, output=11)])
        before = self.collect()["tokens"]
        bad_bodies = [
            "{not json",
            "[]",
            json.dumps({"schema": 99, "files": {}}),
            json.dumps({"schema": 1, "files": []}),
            json.dumps({"schema": 1, "files": {"x": "nope"}}),
            json.dumps({"schema": 1, "files": {"x": {"offset": "1", "size": 1, "agent": "a", "hours": {}}}}),
            json.dumps({"schema": 1, "files": {"x": {"offset": 1, "size": 1, "agent": "a", "hours": {},
                                                    "last": {"id": 1}}}}),
        ]
        for body in bad_bodies:
            with self.subTest(body=body[:30]):
                with open(os.path.join(self.state, "offsets.json"), "w") as fh:
                    fh.write(body)
                self.assertEqual(self.collect()["tokens"], before)
                self.assertEqual(self.offsets()["schema"], 1)  # healed

    def test_corrupt_line_skipped_counted_exit_zero(self):
        # Scenario: Corrupt line
        path = self.session("proj-a", "sess-1", [ev(60, output=10)])
        self.tree.append_raw(path, "{this is not json\n")
        self.tree.append_events(path, [ev(30, output=5)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = snapshot.collect_main(self.config, None)
        self.assertEqual(rc, 0)
        snap = self.collect()
        self.assertEqual(snap["tokens"]["last_24h"]["output"], 15)
        self.assertEqual(snap["warnings"], ["skipped 1 unparseable transcript lines"])
        # The count is kept in the cache, so the warning does not vanish on the next (warm) run.
        self.assertEqual(self.collect()["warnings"], ["skipped 1 unparseable transcript lines"])

    def test_unreadable_file_is_counted_not_fatal(self):
        self.session("proj-a", "sess-1", [ev(60, output=10)])
        bad = self.session("proj-a", "sess-2", [ev(60, output=5)])
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o644)
        if os.access(bad, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        snap = self.collect()
        self.assertEqual(snap["tokens"]["last_24h"]["output"], 10)
        self.assertEqual(snap["warnings"], ["1 transcript files unreadable"])

    def test_late_meta_file_updates_unknown_label(self):
        path = self.subagent("proj-a", "sess-1", "a1", None, [ev(60, output=1)], meta=False)
        self.collect()
        self.assertEqual(list(self.offsets()["files"].values())[0]["agent"], "unknown")
        with open(path[: -len(".jsonl")] + ".meta.json", "w") as fh:
            json.dump({"agentType": "core-researcher"}, fh)
        self.collect()
        self.assertEqual(list(self.offsets()["files"].values())[0]["agent"], "core-researcher")

    def test_unrecognised_layouts_are_ignored(self):
        odd = os.path.join(self.tree.projects_dir, "proj-a", "deeper", "nest", "x", "y.jsonl")
        os.makedirs(os.path.dirname(odd))
        with open(odd, "w") as fh:
            fh.write(json.dumps(ev(60, output=999)) + "\n")
        self.assertEqual(self.collect()["tokens"]["last_24h"]["output"], 0)

    def test_offsets_and_snapshot_are_private_and_atomic(self):
        self.session("proj-a", "sess-1", [ev(60, output=1)])
        snapshot.collect_and_write(self.config)
        for name in ("offsets.json", "snapshot.json"):
            mode = os.stat(os.path.join(self.state, name)).st_mode & 0o777
            self.assertEqual(mode, 0o600, name)
        # No temp files left behind by the tmp + rename write.
        self.assertEqual(sorted(os.listdir(self.state)), ["offsets.json", "snapshot.json"])
        with open(os.path.join(self.state, "snapshot.json")) as fh:
            self.assertEqual(json.load(fh)["schema"], 1)

    def test_offsets_cache_holds_no_transcript_text(self):
        self.session("proj-a", "sess-1", [ev(60, output=1)])
        self.collect()
        with open(os.path.join(self.state, "offsets.json")) as fh:
            raw = fh.read()
        for secret in (SYNTH_CWD, SYNTH_TEXT, SYNTH_SESSION_NAME):
            self.assertNotIn(secret, raw)

    def test_offsets_not_writable_warns_but_still_reports(self):
        self.session("proj-a", "sess-1", [ev(60, output=4)])
        with mock.patch.object(sources_claude, "atomic_write_json", side_effect=OSError("denied")):
            snap = self.collect()
        self.assertEqual(snap["tokens"]["last_24h"]["output"], 4)
        self.assertEqual(snap["warnings"], ["offset cache not writable"])


class TestCollectCommand(CollectBase):
    def test_collect_main_writes_snapshot_and_prints_one_line(self):
        self.session("proj-a", "sess-1", [ev(60, output=100)])
        self.tree.set_agents([agent_row(status="busy")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = snapshot.collect_main(self.config, None)
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("sessions 1", lines[0])
        self.assertIn("7d 100", lines[0])
        self.assertNotIn(self.work, lines[0])
        with open(os.path.join(self.state, "snapshot.json")) as fh:
            self.assertEqual(json.load(fh)["tokens"]["last_7d"]["output"], 100)

    def test_collect_main_nonzero_when_snapshot_cannot_be_written(self):
        blocker = os.path.join(self.work, "a-file")
        with open(blocker, "w") as fh:
            fh.write("x")
        config = self.make_config(state=os.path.join(blocker, "state"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = snapshot.collect_main(config, None)
        self.assertNotEqual(rc, 0)
        self.assertNotIn(self.work, err.getvalue())  # no paths in messages

    def test_collect_and_write_returns_the_written_snapshot(self):
        snap = snapshot.collect_and_write(self.config)
        with open(snapshot.snapshot_path(self.config)) as fh:
            self.assertEqual(json.load(fh), snap)

    def test_cli_end_to_end_runs_under_this_python(self):
        # Scenario: Stock macOS Python - the whole suite is run with /usr/bin/python3 (3.9.6) as
        # the success command; this additionally drives the real CLI entry point in a subprocess
        # with this interpreter and no third-party packages.
        self.session("proj-a", "sess-1", [ev(60, output=3)])
        env = dict(os.environ)
        env.update(self.tree.env({"GRID_FLEET_STATE_DIR": self.state,
                                  "GRID_FLEET_CONFIG": os.path.join(self.work, "none.json")}))
        proc = subprocess.run([sys.executable, "-I", os.path.join(REPO, "bin", "grid-fleet"), "collect"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.decode().splitlines()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.state, "snapshot.json")))


class TestPrivacy(CollectBase):
    """Scenario: No identifying strings. Sessions have names, cwds, project dirs and ids."""

    PROJECT = "secretproj-dirname-xyz"
    SESSION_ID = "sess-secretid-7777"

    def test_no_identifying_strings_under_any_config(self):
        self.session(self.PROJECT, self.SESSION_ID, [ev(60, output=10, session_id=self.SESSION_ID)])
        self.subagent(self.PROJECT, self.SESSION_ID, "agentid9", "core-researcher",
                      [ev(30, output=5, session_id=self.SESSION_ID)])
        # Provoke every warning path too: a corrupt line, unreadable agents, untested version.
        self.tree.append_raw(os.path.join(self.tree.projects_dir, self.PROJECT, self.SESSION_ID + ".jsonl"),
                             "{garbage " + SYNTH_CWD + "\n")
        self.tree.set_agents([agent_row(status="busy"), agent_row(status="waiting")])
        self.tree.set_version("9.9.9")
        forbidden = [SYNTH_CWD, SYNTH_SESSION_NAME, SYNTH_TEXT, "synthetic-session-id-0001",
                     self.PROJECT, self.SESSION_ID, "agentid9", "/synthetic", self.work, self.tree.root]
        configs = [
            {},
            {"account": "team-x", "interval_s": 300, "hub": "local"},
            {"view": {"expected_hosts": ["alpha", "beta"], "ascii": True}, "statusline_original": "echo hi"},
        ]
        for i, extra in enumerate(configs):
            with self.subTest(config=i):
                config = self.make_config(extra)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    snapshot.collect_main(config, None)
                with open(os.path.join(self.state, "snapshot.json")) as fh:
                    text = fh.read()
                for s in forbidden:
                    self.assertNotIn(s, text, "snapshot leaks %r" % s)
                    self.assertNotIn(s, out.getvalue(), "stdout leaks %r" % s)

    def test_failure_warnings_carry_no_paths(self):
        for mode in ("fail", "garbage"):
            self.tree.set_agents_mode(mode)
            for w in self.collect()["warnings"]:
                self.assertNotIn(self.tree.root, w)
        config = self.make_config(extra_env={"GRID_FLEET_CLAUDE_BIN": self.tree.missing_bin()})
        for w in self.collect(config)["warnings"]:
            self.assertNotIn(self.tree.root, w)
            self.assertNotIn("no-such-claude", w)

    def test_snapshot_keys_are_exactly_the_documented_set(self):
        snap = self.collect()
        self.assertEqual(set(snap), {"schema", "agent", "agent_version", "host", "account", "collected_at",
                                     "interval_s", "sessions", "tokens", "agents", "limits", "warnings"})
        self.assertTrue(re.match(r"^[A-Za-z0-9._-]+$", snap["host"]))


class TestLimitsSection(CollectBase):
    """Collect side of limits capture (snapshot-collect + limits-capture specs).

    The file is written by hand here (the tap has its own tests in test_limits.py).
    """

    GOOD = {"schema": 1, "captured_at": NOW - 120,
            "five_hour": {"used_pct": 42.0, "resets_at": NOW + 3600},
            "seven_day": {"used_pct": 18.0, "resets_at": NOW + 86400}}

    def put(self, obj):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "limits.json"), "w") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)

    def test_full_snapshot_carries_captured_limits(self):
        # Scenario: Full snapshot (limits populated from the limits file)
        self.put(self.GOOD)
        limits = self.collect()["limits"]
        self.assertEqual(limits, {"captured_at": NOW - 120,
                                  "five_hour": {"used_pct": 42.0, "resets_at": NOW + 3600},
                                  "seven_day": {"used_pct": 18.0, "resets_at": NOW + 86400}})

    def test_no_file_is_null_not_zero(self):
        # Scenario: Non-Max plan / Fresh session -> limits: null
        self.assertIsNone(self.collect()["limits"])

    def test_each_window_is_independently_optional(self):
        for keep in ("five_hour", "seven_day"):
            body = {k: v for k, v in self.GOOD.items() if k in ("schema", "captured_at", keep)}
            self.put(body)
            limits = self.collect()["limits"]
            self.assertEqual(set(limits), {"captured_at", keep})

    def test_invalid_file_is_null_and_collect_still_succeeds(self):
        for bad in ("{not json", "[]", "null", '{"schema": 1}',
                    json.dumps({"schema": 99, "captured_at": 1, "five_hour": {"used_pct": 1, "resets_at": 2}}),
                    json.dumps({"schema": 1, "captured_at": "x", "five_hour": {"used_pct": 1, "resets_at": 2}}),
                    json.dumps({"schema": 1, "captured_at": 1, "five_hour": {"used_pct": True, "resets_at": 2}})):
            self.put(bad)
            self.assertIsNone(self.collect()["limits"], bad)

    def test_huge_ints_in_the_file_do_not_crash_collect(self):
        # F2: math.isfinite(10**400) raises OverflowError; a hand-edited file must yield
        # limits: null (or drop just the bad window), never a traceback.
        huge = 10 ** 400
        self.put(json.dumps({"schema": 1, "captured_at": huge,
                             "five_hour": {"used_pct": 1, "resets_at": NOW + 9}}))
        self.assertIsNone(self.collect()["limits"])
        self.put(json.dumps({"schema": 1, "captured_at": NOW,
                             "five_hour": {"used_pct": huge, "resets_at": NOW + 9},
                             "seven_day": {"used_pct": 2, "resets_at": NOW + 9}}))
        self.assertEqual(self.collect()["limits"],
                         {"captured_at": NOW, "seven_day": {"used_pct": 2, "resets_at": NOW + 9}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect_main(self.config, None), 0)

    def test_unknown_fields_in_the_file_never_reach_the_snapshot(self):
        body = json.loads(json.dumps(self.GOOD))
        body["cwd"] = SYNTH_CWD
        body["five_hour"]["session_id"] = SYNTH_SESSION_NAME
        self.put(body)
        text = json.dumps(self.collect())
        for s in (SYNTH_CWD, SYNTH_SESSION_NAME):
            self.assertNotIn(s, text)

    def test_account_label_comes_from_config(self):
        self.put(self.GOOD)
        snap = self.collect(self.make_config({"account": "team-plan"}))
        self.assertEqual(snap["account"], "team-plan")
        self.assertIsNotNone(snap["limits"])

    def test_collect_main_exits_zero_with_limits_present(self):
        self.put(self.GOOD)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect_main(self.config, None), 0)
        with open(os.path.join(self.state, "snapshot.json")) as fh:
            self.assertEqual(json.load(fh)["limits"]["five_hour"]["used_pct"], 42.0)


if __name__ == "__main__":
    unittest.main()
