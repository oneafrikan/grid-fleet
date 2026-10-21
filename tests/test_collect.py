"""Tests for `collect` (Group 2): sessions, token totals, incremental scan, fail-soft, privacy.

Scenario names follow openspec/changes/fleet-tracer-bullet/specs/snapshot-collect/spec.md.
NOT covered here on purpose (other groups): Subagent leaderboard (`agents` section) and limits.

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
        # Scenario: Full snapshot (limits/agents are later groups and stay null)
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
        self.assertIsNone(snap["agents"])  # group 6
        self.assertIsNone(snap["limits"])  # group 7

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
        self.assertTrue(any("no transcripts" in w for w in snap["warnings"]))

    def test_idle_host_with_no_transcripts_reports_zeros(self):
        snap = self.collect()
        self.assertEqual(snap["tokens"]["last_7d"]["output"], 0)
        self.assertEqual(len(snap["tokens"]["by_day"]), 7)

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


if __name__ == "__main__":
    unittest.main()
