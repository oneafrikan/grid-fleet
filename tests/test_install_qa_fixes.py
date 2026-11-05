"""Tests for the minor QA fixes to `install` (claude-on-PATH warning, wording, exit paths,
and the survivors from mutation testing). Same harness as test_install / test_install_config:
temp HOME, FakeRunner, injected platform / tty / prompt; nothing real is touched and every
value is synthetic.

These are the author's own self-checks, not independent verification.
"""

import os
import subprocess
import sys
import unittest

from grid_fleet import install

from test_install import FakeRunner
from test_install_config import ConfigCase

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin", "grid-fleet")


class ClaudeCase(ConfigCase):
    def fake_claude(self, name="claude", executable=True):
        """A directory holding a fake `claude`; returns the directory."""
        d = os.path.join(self.work, "bin-" + name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755 if executable else 0o644)
        return d


class TestClaudeOnPathWarning(ClaudeCase):
    """Real-world bug: a timer installed over non-interactive ssh had no ~/.local/bin on
    PATH, so every tick reported `sessions: null`. Install must say so, not refuse."""

    def test_plan_warns_when_claude_is_not_on_the_recorded_path(self):
        rc, out, err = self.run_install(hub="hub-alias")
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", out)
        self.assertIn("`claude` was not found on the PATH", out)
        self.assertIn("PATH=$HOME/.local/bin:$PATH", out)  # names the fix
        self.assertEqual(self.tree(), [self.home])  # still a plan: nothing written

    def test_apply_warns_too_and_still_installs(self):
        rc, out, err = self.run_install(apply=True, hub="hub-alias")
        self.assertEqual(rc, 0, err)  # a warning, not a refusal
        self.assertIn("WARNING: `claude` was not found", out)
        self.assertTrue(os.path.exists(self.plist))

    def test_warns_on_systemd_too(self):
        self.assertIn("WARNING: `claude` was not found",
                      self.run_install("systemd", hub="hub-alias")[1])

    def test_no_warning_when_claude_is_on_the_recorded_path(self):
        d = self.fake_claude()
        env = dict(self.env, PATH=d + os.pathsep + self.env["PATH"])
        rc, out, err = self.run_install(env=env, hub="hub-alias")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("WARNING: `claude`", out)

    def test_a_non_executable_claude_does_not_count(self):
        d = self.fake_claude(executable=False)
        env = dict(self.env, PATH=d)
        self.assertIn("WARNING: `claude` was not found",
                      self.run_install(env=env, hub="hub-alias")[1])

    def test_claude_bin_env_pointing_at_an_executable_means_no_warning(self):
        d = self.fake_claude(name="my-claude")
        env = dict(self.env, GRID_FLEET_CLAUDE_BIN=os.path.join(d, "my-claude"))
        self.assertNotIn("WARNING: `", self.run_install(env=env, hub="hub-alias")[1])

    def test_claude_bin_env_pointing_at_nothing_still_warns(self):
        env = dict(self.env, GRID_FLEET_CLAUDE_BIN=os.path.join(self.work, "missing", "claude"))
        out = self.run_install(env=env, hub="hub-alias")[1]
        self.assertIn("WARNING:", out)
        self.assertIn(os.path.join(self.work, "missing", "claude"), out)

    def test_empty_path_entry_is_not_searched(self):
        # execvp treats an empty entry as the cwd; a timer must not rely on that.
        d = self.fake_claude()
        old = os.getcwd()
        os.chdir(d)
        try:
            self.assertFalse(install.claude_on_path("claude", ":" + self.env["PATH"]))
        finally:
            os.chdir(old)


class TestRewriteNoteNamesTheFile(ConfigCase):
    """The layout note used to say 'settings.json' even when the file was the config."""

    ODD_LAYOUT = '{"schema": 1,   "hub": "old-hub"}'  # json.dumps cannot reproduce this

    def test_config_write_note_names_the_config_file(self):
        self.write_config(text=self.ODD_LAYOUT)
        rc, out, err = self.run_install(config=self.loaded_config(), hub="new-hub")
        self.assertEqual(rc, 0, err)
        self.assertIn("note: config.json is not in a layout", out)
        self.assertNotIn("settings.json", out)

    def test_statusline_note_names_settings_json(self):
        settings = os.path.join(self.work, "claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as fh:
            fh.write('{"statusLine":{"command":"x"},   "k":1}')
        edit = install.plan_statusline_install(settings, "/opt/synth/py", "/srv/synth/grid-fleet")
        self.assertIn("note: settings.json is not in a layout", edit.note)
        self.assertNotIn("config.json", edit.note)

    def test_note_uses_the_actual_file_name(self):
        _text, note = install._render({"a": 1}, (2, False, "\n"), False, "/x/y/other.json")
        self.assertIn("note: other.json is not", note)


class TestTimerFailureAfterConfigWritten(ConfigCase):
    """Config is written BEFORE the timer loads; if the load fails the error must say the
    config is already on disk and that re-running is safe."""

    def test_launchd_bootstrap_failure_mentions_written_config_and_safe_rerun(self):
        runner = FakeRunner(fail_on="bootstrap")
        rc, out, err = self.run_install(config=self.nohub, runner=runner, apply=True,
                                        hub="hub-alias")
        self.assertEqual(rc, install.EXIT_REFUSED)
        self.assertIn("bootstrap", err)  # the original failure is still shown
        self.assertIn("config was already written", err)
        self.assertIn(self.cfg_path, err)
        self.assertIn("re-running the same command is safe", err)
        self.assertEqual(self.config_json()["hub"], "hub-alias")  # it really was written

    def test_systemd_enable_failure_says_the_same(self):
        runner = FakeRunner(fail_on="enable")
        rc, _out, err = self.run_install("systemd", config=self.nohub, runner=runner,
                                         apply=True, hub="hub-alias")
        self.assertEqual(rc, install.EXIT_REFUSED)
        self.assertIn("config was already written", err)

    def test_rerun_after_the_failure_repairs(self):
        self.run_install(config=self.nohub, runner=FakeRunner(fail_on="bootstrap"),
                         apply=True, hub="hub-alias")
        rc, _out, err = self.run_install(config=self.loaded_config(), apply=True,
                                         hub="hub-alias")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(self.plist))
        self.assertEqual(self.runner.loaded, True)

    def test_no_such_line_when_the_config_was_not_changed(self):
        # Config already matches; only the timer step fails: nothing was written to mention.
        self.write_config({"schema": 1, "interval_s": 600, "hub": "hub-alias",
                           "account": "default"})
        rc, _out, err = self.run_install(config=self.loaded_config(),
                                         runner=FakeRunner(fail_on="bootstrap"), apply=True)
        self.assertEqual(rc, install.EXIT_REFUSED)
        self.assertNotIn("already written", err)


class TestConfigOnlyChangeWording(ConfigCase):
    def test_config_only_change_does_not_claim_the_timer_was_installed(self):
        for kind in ("launchd", "systemd"):
            if os.path.exists(self.cfg_path):
                os.remove(self.cfg_path)
            self.runner = FakeRunner()
            self.run_install(kind, config=self.nohub, apply=True, hub="hub-alias")
            self.runner.calls.clear()
            rc, out, err = self.run_install(kind, config=self.loaded_config(), apply=True,
                                            hub="other-hub")
            self.assertEqual(rc, 0, err)
            self.assertIn("config written", out)
            self.assertIn("config updated; timer unchanged", out)
            # Only what is printed AFTER the write counts (the plan above has logs/linger).
            tail = out.split("config written")[-1]
            self.assertNotIn("installed", tail)
            self.assertNotIn("logs:", tail)
            self.assertNotIn("linger", tail)
            self.assertEqual(self.runner.mutating(), [])

    def test_a_real_timer_install_still_says_installed_with_the_log_hint(self):
        rc, out, _err = self.run_install(config=self.nohub, apply=True, hub="hub-alias")
        tail = out.split("config written")[-1]
        self.assertEqual(rc, 0)
        self.assertIn("installed", tail)
        self.assertIn("logs:", tail)
        self.assertNotIn("timer unchanged", tail)


class TestBareDoubleDashFlagValue(ConfigCase):
    """`--hub=--` / `--account=--` reach install_main as `[]` on Python 3.9 (argparse eats
    the `--`). The error must not print `[]`."""

    def test_non_string_values_get_a_clear_message(self):
        for flag in ("hub", "account"):
            rc, _out, err = self.run_install(**{flag: []})
            self.assertEqual(rc, install.EXIT_USAGE)
            self.assertIn("invalid --%s value" % flag, err)
            self.assertNotIn("[]", err)
        self.assertEqual(self.tree(), [self.home])  # nothing written

    def test_a_string_value_is_still_echoed(self):
        rc, _out, err = self.run_install(hub="bad host")
        self.assertEqual(rc, install.EXIT_USAGE)
        self.assertIn("invalid --hub 'bad host'", err)

    def test_real_cli_process_never_prints_empty_brackets(self):
        # Same interpreter as the suite: on 3.9 this is the `[]` path; newer Pythons keep
        # the string, so `--hub=--` is rejected by the validator and `--account=--` is a
        # legal label (non-empty printable ASCII) and is accepted. Never any `[]`.
        env = dict(os.environ, GRID_FLEET_CONFIG=self.cfg_path, GRID_FLEET_STATE_DIR=self.state,
                   GRID_FLEET_CLAUDE_DIR=os.path.join(self.work, "claude"), HOME=self.home)
        r = subprocess.run([sys.executable, BIN, "install", "--hub=--"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("[]", r.stderr)
        self.assertIn("--hub", r.stderr)
        r = subprocess.run([sys.executable, BIN, "install", "--account=--"], env=env,
                           capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 2), r.stderr)  # 0 on newer Pythons, 2 on 3.9
        self.assertNotIn("[]", r.stderr)


if __name__ == "__main__":
    unittest.main()
