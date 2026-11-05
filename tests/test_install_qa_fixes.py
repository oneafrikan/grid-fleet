"""Tests for the minor QA fixes to `install` (claude-on-PATH warning, wording, exit paths,
and the survivors from mutation testing). Same harness as test_install / test_install_config:
temp HOME, FakeRunner, injected platform / tty / prompt; nothing real is touched and every
value is synthetic.

These are the author's own self-checks, not independent verification.
"""

import os
import unittest

from grid_fleet import install

from test_install import FakeRunner
from test_install_config import ConfigCase


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


if __name__ == "__main__":
    unittest.main()
