"""Self-test of tests/helpers/claude_tree.py so later units can trust the fixture."""

import json
import os
import subprocess
import unittest

from helpers.claude_tree import (
    ClaudeTree, SYNTH_CWD, agent_row, dead_pid, iso_z, usage_event, user_event)


class TestClaudeTree(unittest.TestCase):
    def setUp(self):
        self.tree = ClaudeTree()
        self.addCleanup(self.tree.cleanup)

    def run_fake(self, *args, timeout=15):
        return subprocess.run([self.tree.bin_path] + list(args), capture_output=True,
                              text=True, timeout=timeout)

    def test_transcript_layout_and_line_shape(self):
        main = self.tree.add_session("proj-a", "sess-1", [usage_event(1792580580, output=7, input=1)])
        sub = self.tree.add_subagent("proj-a", "sess-1", "a1", "core-researcher", [usage_event(1792580600)])
        self.assertEqual(main, os.path.join(self.tree.projects_dir, "proj-a", "sess-1.jsonl"))
        self.assertTrue(sub.endswith("proj-a/sess-1/subagents/agent-a1.jsonl"))
        with open(sub.replace(".jsonl", ".meta.json")) as fh:
            self.assertEqual(json.load(fh), {"agentType": "core-researcher"})
        with open(main) as fh:
            ev = json.loads(fh.readline())
        self.assertEqual(ev["type"], "assistant")
        self.assertEqual(ev["timestamp"], "2026-10-21T11:03:00.000Z")
        self.assertEqual(ev["message"]["usage"], {
            "input_tokens": 1, "output_tokens": 7,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        self.assertEqual(ev["cwd"], SYNTH_CWD)  # noise present for privacy tests

    def test_meta_variants_and_unique_message_ids(self):
        p = self.tree.add_subagent("p", "s", "x", "t", [], meta=False)
        self.assertFalse(os.path.exists(p.replace(".jsonl", ".meta.json")))
        self.assertNotEqual(usage_event(0)["message"]["id"], usage_event(0)["message"]["id"])
        self.assertEqual(usage_event(0, message_id="m")["message"]["id"], "m")
        self.assertNotIn("cwd", usage_event(0, noise=False))
        self.assertEqual(user_event(0)["type"], "user")

    def test_append_helpers_and_mtime(self):
        p = self.tree.add_session("p", "s", [usage_event(0)])
        self.tree.append_events(p, [usage_event(1)])
        self.tree.append_raw(p, '{"partial": ')
        with open(p) as fh:
            lines = fh.read().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], '{"partial": ')
        self.tree.set_mtime(p, 1000)
        self.assertEqual(int(os.path.getmtime(p)), 1000)

    def test_fake_claude_version_and_agents(self):
        self.tree.set_version("9.9.9")
        self.assertEqual(self.run_fake("--version").stdout.strip(), "9.9.9 (Claude Code)")
        rows = [agent_row(status="waiting"), agent_row(kind="background")]
        self.tree.set_agents(rows)
        r = self.run_fake("agents", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), rows)
        self.assertEqual(self.tree.calls(), ["--version", "agents --json"])

    def test_fake_claude_failure_modes(self):
        self.tree.set_agents_mode("fail")
        self.assertNotEqual(self.run_fake("agents", "--json").returncode, 0)
        self.tree.set_agents_mode("garbage")
        r = self.run_fake("agents", "--json")
        self.assertEqual(r.returncode, 0)
        with self.assertRaises(ValueError):
            json.loads(r.stdout)
        self.tree.set_agents_mode("hang")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_fake("agents", "--json", timeout=1)

    def test_pids_and_env(self):
        self.assertEqual(agent_row()["pid"], os.getpid())
        os.kill(agent_row()["pid"], 0)  # live
        with self.assertRaises(OSError):
            os.kill(dead_pid(), 0)
        e = self.tree.env({"GRID_FLEET_NOW": "5"})
        self.assertEqual(e["GRID_FLEET_CLAUDE_DIR"], self.tree.claude_dir)
        self.assertEqual(e["GRID_FLEET_CLAUDE_BIN"], self.tree.bin_path)
        self.assertEqual(e["GRID_FLEET_NOW"], "5")
        self.assertFalse(os.path.exists(self.tree.missing_bin()))

    def test_iso_z(self):
        self.assertEqual(iso_z(0), "1970-01-01T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
