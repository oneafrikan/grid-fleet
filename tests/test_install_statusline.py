"""Tests for `install --statusline` and its restore (Group 8, tasks 8.3 + 8.4): one group per
statusLine scenario in specs/install-timers/spec.md ("Confirmed statusLine change",
"Uninstall" restore, "Never silent").

Nothing real is touched. The Claude dir is a temp dir (the settings path is injected through
Config.claude_dir, the same value GRID_FLEET_CLAUDE_DIR gives in production); HOME, the
config file and the state dir are temp too; launchctl/systemctl are the FakeRunner from
test_install. No test reads or writes the real ~/.claude or runs the real `claude`.
Settings content is synthetic.

These are the author's own self-checks, not independent verification.
"""

import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from grid_fleet import install
from grid_fleet import config as cfg

from test_install import PY, SCRIPT, FakeRunner, InstallCase, _read, _readb

ORIGINAL = "~/bin/synth-statusline.sh --compact"

# Synthetic settings in the layout Claude Code writes (2-space JSON, trailing newline).
# Key order is deliberately not alphabetical and includes an unknown key.
SETTINGS = {
    "zeta": 1,
    "statusLine": {"type": "command", "command": ORIGINAL, "padding": 0},
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "alpha": {"nested": [1, 2.5, None, True], "text": "café"},
    "futureUnknownKey": {"keep": "me"},
}


def dump(data, indent=2, nl="\n", ascii_=False):
    return json.dumps(data, indent=indent, ensure_ascii=ascii_) + nl


class StatuslineCase(InstallCase):
    """InstallCase plus a temp Claude dir and config file; helpers for both sides."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.work, "claude")
        os.makedirs(self.claude)
        self.settings = os.path.join(self.claude, "settings.json")
        self.cfg_dir = os.path.join(self.work, "cfg")
        os.makedirs(self.cfg_dir)
        self.cfg_path = os.path.join(self.cfg_dir, "config.json")
        self.config = self.make_config()
        # Safety net: every path the tests use lives under the temp dir.
        self.assertTrue(self.settings.startswith(self.work))

    def make_config(self):
        """A Config as cli would build it: re-read from the file each time, because
        install and uninstall are separate processes in real life."""
        return cfg.load_config({"GRID_FLEET_CONFIG": self.cfg_path,
                                "GRID_FLEET_STATE_DIR": self.state,
                                "GRID_FLEET_CLAUDE_DIR": self.claude, "HOME": self.home})

    def write_settings(self, data=SETTINGS, text=None, mode=0o600):
        with open(self.settings, "w") as fh:
            fh.write(text if text is not None else dump(data))
        os.chmod(self.settings, mode)
        return _readb(self.settings)

    def write_config(self, data):
        with open(self.cfg_path, "w") as fh:
            fh.write(dump(data))

    def sl(self, **kw):
        """Run `install --statusline` (fresh config from the file)."""
        self.config = self.make_config()
        return self.run_install("launchd", config=self.config, statusline=True, **kw)

    def uninstall(self, only_statusline=False, **kw):
        self.config = self.make_config()
        return self.run_install("launchd", config=self.config, uninstall=True,
                                statusline=only_statusline, **kw)

    def args(self, **kw):
        base = dict(statusline=False)
        base.update(kw)
        return super().args(**base)

    def backups(self):
        return sorted(n for n in os.listdir(self.claude) if install.BACKUP_INFIX in n)

    def settings_json(self):
        return json.loads(_read(self.settings))

    def wrapped_words(self):
        return shlex.split(self.settings_json()["statusLine"]["command"])


class TestConfirmedChange(StatuslineCase):
    """Scenario: Existing statusLine."""

    def test_command_becomes_tap_wrapping_original_and_other_keys_unchanged(self):
        before = self.write_settings()
        rc, out, err = self.sl()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.prompts), 1)
        data = self.settings_json()
        self.assertEqual(shlex.split(data["statusLine"]["command"]),
                         [PY, SCRIPT, "tap", "--", "/bin/sh", "-c", ORIGINAL])
        # Every other key, known or not, equal; so is the rest of the statusLine object.
        expected = json.loads(before.decode())
        expected["statusLine"]["command"] = data["statusLine"]["command"]
        self.assertEqual(data, expected)
        # Key order preserved (statusLine stays where it was).
        self.assertEqual(list(data), list(SETTINGS))
        self.assertEqual(list(data["statusLine"]), ["type", "command", "padding"])

    def test_layout_is_preserved_when_it_can_be_reproduced(self):
        self.write_settings()
        _, out, _ = self.sl()
        self.assertNotIn("note:", out)
        expected = json.loads(_read(self.settings))
        self.assertEqual(_read(self.settings), dump(expected))

    def test_other_layouts_are_reproduced_too(self):
        for indent, nl in ((4, "\n"), ("\t", ""), (None, "\n")):
            self.write_settings(text=dump(SETTINGS, indent, nl))
            rc, out, _ = self.sl()
            self.assertEqual(rc, 0)
            self.assertNotIn("note:", out)
            expected = json.loads(_read(self.settings))
            self.assertEqual(_read(self.settings), dump(expected, indent, nl))
            self.run_uninstall_cleanup()

    def run_uninstall_cleanup(self):
        self.uninstall(only_statusline=True)

    def test_unreproducible_layout_is_flagged_before_confirmation(self):
        self.write_settings(text='{"statusLine":{"command":"x"},   "k":[1,\n2]}')
        rc, out, _ = self.sl()
        self.assertEqual(rc, 0)
        self.assertIn("note:", out)
        self.assertEqual(self.settings_json()["k"], [1, 2])

    def test_exact_before_and_after_are_printed(self):
        self.write_settings()
        _, out, _ = self.sl()
        wrapped = self.settings_json()["statusLine"]["command"]
        self.assertIn("before: " + json.dumps(ORIGINAL), out)
        self.assertIn("after:  " + json.dumps(wrapped), out)
        self.assertIn("all other settings: unchanged", out)

    def test_backup_is_a_byte_copy_never_wider_than_0600(self):
        # S4: settings.json may hold secrets, so the backup is the original's mode & 0600:
        # a world/group-readable original gives an owner-only backup, never the other way.
        for original_mode, expected in ((0o644, 0o600), (0o640, 0o600), (0o600, 0o600),
                                        (0o400, 0o400)):
            with self.subTest(mode=oct(original_mode)):
                for name in self.backups():  # one backup per sub-case
                    os.chmod(os.path.join(self.claude, name), 0o600)
                    os.remove(os.path.join(self.claude, name))
                if os.path.exists(self.settings):  # the previous case may have left it 0400
                    os.chmod(self.settings, 0o600)
                before = self.write_settings(mode=original_mode)
                self.sl()
                (name,) = self.backups()
                backup = os.path.join(self.claude, name)
                self.assertEqual(os.stat(backup).st_mode & 0o777, expected)
                self.assertEqual(_readb(backup), before)

    def test_settings_mode_unchanged_when_backup_is_tightened(self):
        self.write_settings(mode=0o644)
        self.sl()
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o644)

    def test_config_dir_created_by_install_is_private(self):
        # S4: the config dir (and any parent install had to create) is 0700; the config file
        # itself stays 0600. A config dir that already existed keeps its mode.
        self.write_settings()
        self.cfg_path = os.path.join(self.work, "newcfg", "grid-fleet", "config.json")
        self.sl()
        for d in (os.path.dirname(self.cfg_path), os.path.dirname(os.path.dirname(self.cfg_path))):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)

    def test_existing_config_dir_mode_is_left_alone(self):
        self.write_settings()
        os.chmod(self.cfg_dir, 0o755)
        self.sl()
        self.assertEqual(os.stat(self.cfg_dir).st_mode & 0o777, 0o755)

    def test_settings_mode_is_preserved(self):
        self.write_settings(mode=0o600)
        self.sl()
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)

    def test_original_is_saved_to_config_keeping_other_keys(self):
        self.write_settings()
        self.write_config({"schema": 1, "hub": "hub-alias", "future": {"x": 1}})
        os.chmod(self.cfg_path, 0o640)
        self.sl()
        saved = json.loads(_read(self.cfg_path))
        self.assertEqual(saved, {"schema": 1, "hub": "hub-alias", "future": {"x": 1},
                                 "statusline_original": ORIGINAL})
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o640)
        self.assertEqual(self.make_config().statusline_original, ORIGINAL)

    def test_missing_config_is_created_private(self):
        self.write_settings()
        self.sl()
        self.assertEqual(json.loads(_read(self.cfg_path)),
                         {"schema": 1, "statusline_original": ORIGINAL})
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)

    def test_no_temp_files_left_behind(self):
        self.write_settings()
        self.sl()
        stray = [n for d in (self.claude, self.cfg_dir) for n in os.listdir(d)
                 if n.startswith(".grid-fleet.")]
        self.assertEqual(stray, [])

    def test_symlinked_settings_stay_a_symlink_and_the_target_is_updated(self):
        real = os.path.join(self.work, "dotfiles-settings.json")
        with open(real, "w") as fh:
            fh.write(dump(SETTINGS))
        os.symlink(real, self.settings)
        rc, _, err = self.sl()
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.islink(self.settings))
        self.assertIn("tap", json.loads(_read(real))["statusLine"]["command"])

    def test_timers_are_not_touched(self):
        self.write_settings()
        self.sl()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library")))

    def test_interval_does_not_apply(self):
        self.write_settings()
        rc, _, err = self.sl(interval=300)
        self.assertEqual(rc, install.EXIT_USAGE)
        self.assertEqual(_read(self.settings), dump(SETTINGS))

    def test_original_with_awkward_characters_is_wrapped_exactly(self):
        originals = [
            'echo "a b" \'c\' && echo $HOME | tr a-z A-Z',
            "it's a \"quoted\" `thing` ; rm -rf /tmp/synth # $(nope)",
            "cmd --flag=\"two words\" \\ trailing\\\\backslash",
            "café ✓ --ü",
            "line1\nline2",
            "  leading and trailing spaces  ",
            "--",
            "x -- y",
        ]
        for original in originals:
            data = dict(SETTINGS, statusLine={"type": "command", "command": original})
            self.write_settings(data)
            before = _readb(self.settings)
            rc, _, err = self.sl()
            self.assertEqual(rc, 0, (original, err))
            self.assertEqual(self.wrapped_words()[2:], ["tap", "--", "/bin/sh", "-c", original])
            self.assertEqual(self.make_config().statusline_original, original)
            rc, _, err = self.uninstall(only_statusline=True)
            self.assertEqual(rc, 0, (original, err))
            self.assertEqual(_readb(self.settings), before, original)  # byte-for-byte

    def test_wrapped_command_runs_the_original_through_the_real_tap(self):
        # End to end with the REAL tap code (not claude): the wrapped string, run by a shell
        # as Claude Code would, must behave exactly like the original.
        original = 'echo "a b" \'c\' $GF_SYNTH && echo end; exit 3'
        # Build the wrapper with the real interpreter/script so the shell can run it.
        real_py, real_script = sys.executable, install.default_script()
        wrapped = install.tap_command(real_py, real_script, original)
        env = dict(os.environ, GF_SYNTH="x", GRID_FLEET_STATE_DIR=self.state,
                   GRID_FLEET_CONFIG=self.cfg_path)
        tapped = subprocess.run(["/bin/sh", "-c", wrapped], input=b"{}", env=env,
                                stdout=subprocess.PIPE)
        direct = subprocess.run(["/bin/sh", "-c", original], input=b"{}", env=env,
                                stdout=subprocess.PIPE)
        self.assertEqual((tapped.stdout, tapped.returncode), (direct.stdout, direct.returncode))
        self.assertEqual(direct.stdout, b"a b c x\nend\n")
        self.assertEqual(direct.returncode, 3)


class TestDeclinedAndNonInteractive(StatuslineCase):
    """Scenario: Declined, plus the TTY requirement."""

    def assert_untouched(self, before):
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.cfg_path))
        self.assertEqual(self.runner.calls, [])

    def test_declined_leaves_settings_byte_identical(self):
        before = self.write_settings()
        for answer in ("n", "", "no", "yes please no"):
            rc, out, _ = self.sl(answer=answer)
            self.assertNotEqual(rc, 0)
            self.assertIn("declined", out)
            self.assert_untouched(before)

    def test_eof_at_the_prompt_counts_as_declined(self):
        before = self.write_settings()
        self.config = self.make_config()

        def eof(_text):
            raise EOFError

        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = install.install_main(self.config, self.args(statusline=True), runner=self.runner,
                                      env=self.env, platform="darwin", python=PY, script=SCRIPT,
                                      uid=4242, is_tty=lambda: True, prompt=eof)
        self.assertNotEqual(rc, 0)
        self.assert_untouched(before)

    def test_non_tty_prints_the_diff_refuses_and_changes_nothing(self):
        before = self.write_settings()
        rc, out, err = self.sl(tty=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("before:", out)
        self.assertIn("after:", out)
        self.assertIn("interactive", err)
        self.assertEqual(self.prompts, [], "must not even ask")
        self.assert_untouched(before)

    def test_y_and_yes_confirm(self):
        for answer in ("y", "YES"):
            self.write_settings()
            self.assertEqual(self.sl(answer=answer)[0], 0)
            self.uninstall(only_statusline=True)


class TestNoStatusline(StatuslineCase):
    """Scenario: No statusLine configured."""

    NOSL = {"model": "synth", "env": {"A": "1"}}

    def test_diff_shows_a_tap_with_no_original_and_still_needs_confirmation(self):
        before = self.write_settings(self.NOSL)
        rc, out, err = self.sl(tty=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("before: (no statusLine configured)", out)
        self.assertIn("after:  " + json.dumps("%s %s tap --" % (PY, SCRIPT)), out)
        self.assertEqual(_readb(self.settings), before)
        rc, out, _ = self.sl(answer="n")
        self.assertNotEqual(rc, 0)
        self.assertEqual(_readb(self.settings), before)

    def test_confirmed_adds_a_command_statusline_and_saves_null_original(self):
        self.write_settings(self.NOSL)
        rc, _, err = self.sl()
        self.assertEqual(rc, 0, err)
        data = self.settings_json()
        self.assertEqual(data["statusLine"],
                         {"type": "command", "command": "%s %s tap --" % (PY, SCRIPT)})
        self.assertEqual(list(data), ["model", "env", "statusLine"])
        self.assertIsNone(json.loads(_read(self.cfg_path))["statusline_original"])

    def test_null_statusline_is_treated_as_absent_in_place(self):
        self.write_settings({"a": 1, "statusLine": None, "b": 2})
        self.assertEqual(self.sl()[0], 0)
        self.assertEqual(list(self.settings_json()), ["a", "statusLine", "b"])

    def test_restore_removes_the_statusline_it_created(self):
        before = self.write_settings(self.NOSL)
        self.sl()
        rc, out, err = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("(statusLine removed)", out)
        self.assertEqual(_readb(self.settings), before)

    def test_restore_leaves_a_hand_extended_statusline_alone(self):
        self.write_settings(self.NOSL)
        self.sl()
        data = self.settings_json()
        data["statusLine"]["padding"] = 2  # the user added to our object
        self.write_settings(data)
        edited = _readb(self.settings)
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("by hand", out)
        self.assertEqual(_readb(self.settings), edited)


class TestIdempotent(StatuslineCase):
    def test_second_run_is_a_clean_noop_not_a_double_wrap(self):
        self.write_settings()
        self.assertEqual(self.sl()[0], 0)
        after_first = _readb(self.settings)
        cfg_first = _readb(self.cfg_path)
        backups = self.backups()
        rc, out, _ = self.sl(tty=False)  # even without a TTY: nothing to ask
        self.assertEqual(rc, 0)
        self.assertIn("already wrapped", out)
        self.assertEqual(self.prompts, [])
        self.assertEqual(_readb(self.settings), after_first)
        self.assertEqual(_readb(self.cfg_path), cfg_first)
        self.assertEqual(self.backups(), backups)
        self.assertEqual(self.settings_json()["statusLine"]["command"].count(" tap -- "), 1)

    def test_a_wrapper_with_other_paths_is_still_detected(self):
        other = "/elsewhere/python3 '/old clone/bin/grid-fleet' tap -- /bin/sh -c 'x'"
        before = self.write_settings(dict(SETTINGS, statusLine={"type": "command",
                                                                "command": other}))
        rc, out, _ = self.sl()
        self.assertEqual(rc, 0)
        self.assertIn("already wrapped", out)
        self.assertEqual(_readb(self.settings), before)

    def test_a_similar_looking_user_command_is_not_mistaken_for_a_wrapper(self):
        for cmd in ("my-tap -- thing", "python3 ./grid-fleet-notes tap -- x",
                    "echo 'unbalanced", "grid-fleet tap"):
            self.assertFalse(install.is_tap_command(cmd), cmd)
        self.assertTrue(install.is_tap_command(install.tap_command(PY, SCRIPT, "x y")))
        self.assertTrue(install.is_tap_command(install.tap_command(PY, SCRIPT, None)))


class TestRefusals(StatuslineCase):
    """Missing / invalid files and shapes we will not guess at: refuse, never clobber."""

    def assert_refused(self, expect_text, before=None):
        rc, out, err = self.sl()
        self.assertNotEqual(rc, 0)
        self.assertIn(expect_text, err)
        self.assertEqual(self.prompts, [], "refusal comes before any prompt")
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.cfg_path))
        if before is not None:
            self.assertEqual(_readb(self.settings), before)

    def test_missing_settings_file(self):
        self.assert_refused("not found")
        self.assertFalse(os.path.exists(self.settings), "must not create it")

    def test_invalid_json_is_left_untouched(self):
        for text in ("{not json", "", '{"a": 1,}', "﻿{}"):
            self.write_settings(text=text)
            before = _readb(self.settings)
            self.assert_refused("not valid JSON", before)

    def test_binary_garbage_is_left_untouched(self):
        with open(self.settings, "wb") as fh:
            fh.write(b"\xff\xfe\x00bad")
        self.assert_refused("not valid JSON", b"\xff\xfe\x00bad")

    def test_non_object_json(self):
        self.write_settings(text="[1, 2]\n")
        self.assert_refused("not a JSON object", b"[1, 2]\n")

    def test_unsupported_statusline_shapes(self):
        for sl in ("a string", {"type": "command"}, {"type": "other", "command": "x"},
                   {"type": "command", "command": 5}, ["x"]):
            before = self.write_settings(dict(SETTINGS, statusLine=sl))
            self.assert_refused("shape grid-fleet does not handle", before)

    def test_invalid_config_is_refused_before_the_prompt_and_settings_stay(self):
        before = self.write_settings()
        with open(self.cfg_path, "w") as fh:
            fh.write("{broken")
        # cli would exit on a broken config before install runs; the installer must also
        # refuse on its own when handed a Config that points at one.
        config = cfg.Config(config_path=self.cfg_path, state_dir=self.state,
                            claude_dir=self.claude)
        rc, out, err = self.run_install("launchd", config=config, statusline=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("config file is not valid JSON", err)
        self.assertEqual(self.prompts, [])
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.backups(), [])

    def test_relative_python_or_script_is_refused(self):
        self.write_settings()
        config = self.make_config()
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = install.install_main(config, self.args(statusline=True), runner=self.runner,
                                      env=self.env, platform="darwin", python="python3",
                                      script=SCRIPT, uid=1, is_tty=lambda: True,
                                      prompt=lambda _t: "y")
        self.assertEqual(rc, install.EXIT_USAGE)
        self.assertIn("absolute", err.getvalue())
        self.assertEqual(_read(self.settings), dump(SETTINGS))


class TestWriteSafety(StatuslineCase):
    def test_settings_changed_while_waiting_for_the_answer_aborts(self):
        self.write_settings()
        self.config = self.make_config()
        changed = dump(dict(SETTINGS, zeta=2))

        def prompt(_text):
            with open(self.settings, "w") as fh:  # Claude Code saved a setting meanwhile
                fh.write(changed)
            return "y"

        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = install.install_main(self.config, self.args(statusline=True), runner=self.runner,
                                      env=self.env, platform="darwin", python=PY, script=SCRIPT,
                                      uid=1, is_tty=lambda: True, prompt=prompt)
        self.assertNotEqual(rc, 0)
        self.assertIn("changed while waiting", err.getvalue())
        self.assertEqual(_read(self.settings), changed)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_failed_settings_write_leaves_the_file_intact(self):
        before = self.write_settings()
        orig = install._write_atomic

        def boom(path, text, mode=0o644):
            if path.endswith("settings.json") or os.path.realpath(path).endswith("settings.json"):
                raise OSError("synthetic write failure")
            return orig(path, text, mode)

        with mock.patch.object(install, "_write_atomic", boom):
            rc, _, err = self.sl()
        self.assertNotEqual(rc, 0)
        self.assertIn("synthetic write failure", err)
        self.assertEqual(_readb(self.settings), before)
        # The backup and the saved original exist; a re-run simply overwrites them.
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.sl()[0], 0)

    def test_failed_atomic_rename_leaves_old_file_and_no_temp(self):
        before = self.write_settings()
        with mock.patch.object(os, "replace", side_effect=OSError("synthetic rename failure")):
            rc, _, err = self.sl()
        self.assertNotEqual(rc, 0)
        self.assertEqual(_readb(self.settings), before)
        for d in (self.claude, self.cfg_dir):
            self.assertEqual([n for n in os.listdir(d) if n.startswith(".grid-fleet.")], [])

    def test_two_backups_in_the_same_second_do_not_overwrite(self):
        raw = self.write_settings()
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": "1700000000"}):
            a = install._backup(self.settings, raw)
            b = install._backup(self.settings, b"second")
        self.assertNotEqual(a, b)
        self.assertEqual(_readb(a), raw)
        self.assertIn("20231114T221320Z", a)


class TestRestoreOnUninstall(StatuslineCase):
    """Scenario: Uninstall restores statusLine.command to its saved original."""

    def test_uninstall_restores_the_original_byte_for_byte(self):
        before = self.write_settings()
        self.sl()
        self.assertNotEqual(_readb(self.settings), before)
        rc, out, err = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)
        self.assertIsNone(json.loads(_read(self.cfg_path))["statusline_original"])
        self.assertEqual(len(self.backups()), 2, "install backup + restore backup")

    def test_plain_uninstall_does_timers_and_statusline_with_one_confirmation(self):
        before = self.write_settings()
        self.run_install("launchd", apply=True)
        self.sl()
        self.runner.calls.clear()
        rc, out, err = self.uninstall()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse(os.path.lexists(self.plist))
        self.assertIn(["launchctl", "bootout", "gui/4242/io.grid-fleet.timer"], self.runner.calls)
        self.assertEqual(_readb(self.settings), before)
        self.assertIn("before:", out)  # the restore diff is shown before the question

    def test_statusline_only_uninstall_leaves_timers_alone(self):
        self.run_install("launchd", apply=True)
        self.write_settings()
        self.sl()
        self.runner.calls.clear()
        rc, _, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(os.path.isfile(self.plist))

    def test_declined_or_non_tty_uninstall_changes_nothing(self):
        self.write_settings()
        self.run_install("launchd", apply=True)
        self.sl()
        wrapped = _readb(self.settings)
        rc, out, _ = self.uninstall(answer="n")
        self.assertNotEqual(rc, 0)
        rc, out, err = self.uninstall(tty=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual(_readb(self.settings), wrapped)
        self.assertTrue(os.path.isfile(self.plist))

    def test_uninstall_without_a_wrapped_statusline_is_ok_and_silent(self):
        before = self.write_settings()
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0)
        self.assertIn("not wrapped", out)
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.backups(), [])

    def test_plain_uninstall_with_no_settings_file_is_unchanged_behaviour(self):
        rc, out, _ = self.uninstall()
        self.assertEqual(rc, 0)
        self.assertIn("nothing to remove", out)
        self.assertFalse(os.path.exists(self.settings))

    def test_lost_config_falls_back_to_the_original_inside_the_wrapper(self):
        before = self.write_settings()
        self.sl()
        os.remove(self.cfg_path)
        rc, _, err = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(_readb(self.settings), before)

    def test_invalid_settings_json_on_uninstall_is_untouched_and_nonzero(self):
        self.write_settings(text="{broken")
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT restored", out)
        self.assertEqual(_read(self.settings), "{broken")

    def test_refused_restore_still_removes_the_timers_but_exits_nonzero(self):
        self.write_settings(text="{broken")
        self.run_install("launchd", apply=True)
        rc, out, _ = self.uninstall()
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(self.plist))
        self.assertEqual(_read(self.settings), "{broken")

    def test_restore_keeps_unknown_keys_and_order(self):
        self.write_settings()
        self.sl()
        self.uninstall(only_statusline=True)
        self.assertEqual(self.settings_json(), SETTINGS)
        self.assertEqual(list(self.settings_json()), list(SETTINGS))

    def test_empty_string_original_round_trips(self):
        before = self.write_settings(dict(SETTINGS, statusLine={"type": "command", "command": ""}))
        self.assertEqual(self.sl()[0], 0)
        self.assertEqual(self.wrapped_words()[2:], ["tap", "--"])
        self.assertEqual(self.uninstall(only_statusline=True)[0], 0)
        self.assertEqual(_readb(self.settings), before)


class TestNeverSilent(StatuslineCase):
    """Scenario: Never silent. Nothing but `install --statusline` (and the restore on
    `--uninstall`) writes settings.json."""

    def test_other_install_modes_do_not_touch_settings(self):
        before = self.write_settings()
        self.run_install("launchd")                              # plan
        self.run_install("launchd", apply=True)                  # timers apply
        self.run_install("systemd", apply=True)
        self.run_install("launchd", apply=True, answer="n")
        self.run_install("launchd", tty=False, apply=True)
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_only_install_py_knows_about_settings_json(self):
        pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "grid_fleet")
        for name in sorted(os.listdir(pkg)):
            if name.endswith(".py") and name != "install.py":
                with open(os.path.join(pkg, name)) as fh:
                    self.assertNotIn("settings.json", fh.read(), name)


class TestTapCommandForm(unittest.TestCase):
    def test_form_with_and_without_original(self):
        self.assertEqual(install.tap_command("/p y", "/s/grid-fleet", "a && b"),
                         "'/p y' /s/grid-fleet tap -- /bin/sh -c 'a && b'")
        self.assertEqual(install.tap_command(PY, SCRIPT, None), "%s %s tap --" % (PY, SCRIPT))
        self.assertEqual(install.tap_command(PY, SCRIPT, ""), "%s %s tap --" % (PY, SCRIPT))

    def test_cli_splits_the_wrapper_at_the_first_double_dash(self):
        # The tap's own argv handling (cli.main) must hand the whole original over intact,
        # including an original that itself contains " -- ".
        from grid_fleet import cli
        original = "x -- y && echo 'q'"
        words = shlex.split(install.tap_command(PY, SCRIPT, original))[2:]
        seen = {}

        def fake_tap(config, args):
            seen["argv"] = args.original_command
            return 0

        # Point the config at a file that does not exist: never read the real one.
        with mock.patch("grid_fleet.limits.tap_main", fake_tap), \
                mock.patch.dict(os.environ, {"GRID_FLEET_CONFIG": "/nonexistent-synth/c.json"}):
            self.assertEqual(cli.main(words), 0)
        self.assertEqual(seen["argv"], ["/bin/sh", "-c", original])


def _stream(tty):
    """A text stream that reports `tty` from isatty(); StringIO is never a TTY itself."""
    import io

    class _S(io.StringIO):
        def isatty(self):
            return tty
    return _S()


class TestSharedConfigTwoClaudeDirs(StatuslineCase):
    """QA finding F1: one grid-fleet config, two Claude dirs. The config keeps a single
    `statusline_original` slot, so the original embedded in each dir's own wrapper (not the
    slot) must decide what that dir gets back."""

    A_CMD, B_CMD = "echo ORIGINAL-A", "echo ORIGINAL-B"

    def setUp(self):
        super().setUp()
        self.dir_b = os.path.join(self.work, "claude-b")
        os.makedirs(self.dir_b)
        self.settings_b = os.path.join(self.dir_b, "settings.json")
        self.write_settings(dict(SETTINGS, statusLine={"type": "command", "command": self.A_CMD}))
        with open(self.settings_b, "w") as fh:
            fh.write(dump(dict(SETTINGS, statusLine={"type": "command", "command": self.B_CMD})))
        self.before_a, self.before_b = _readb(self.settings), _readb(self.settings_b)

    def config_b(self):
        return cfg.load_config({"GRID_FLEET_CONFIG": self.cfg_path, "GRID_FLEET_STATE_DIR": self.state,
                                "GRID_FLEET_CLAUDE_DIR": self.dir_b, "HOME": self.home})

    def slot(self):
        return json.loads(_read(self.cfg_path)).get("statusline_original")

    def install_both(self):
        self.assertEqual(self.sl()[0], 0)
        self.assertEqual(self.run_install("launchd", config=self.config_b(), statusline=True)[0], 0)
        self.assertEqual(self.slot(), self.B_CMD, "last install owns the single slot")

    def uninstall_b(self):
        return self.run_install("launchd", config=self.config_b(), uninstall=True, statusline=True)

    def test_each_dir_gets_back_its_own_original(self):
        self.install_both()
        rc, out, err = self.uninstall(only_statusline=True)  # dir A; the slot holds B's value
        self.assertEqual(rc, 0, err)
        self.assertEqual(_readb(self.settings), self.before_a)  # NOT ORIGINAL-B
        # The disagreement is shown before the question, naming both values.
        self.assertIn("note:", out)
        self.assertIn(self.A_CMD, out)
        self.assertIn(self.B_CMD, out)
        # Restoring A must not wipe B's slot value ...
        self.assertEqual(self.slot(), self.B_CMD)
        # ... and B still restores correctly, then clears the slot it owns.
        rc, out, err = self.uninstall_b()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("differs", out)
        self.assertEqual(_readb(self.settings_b), self.before_b)
        self.assertIsNone(self.slot())

    def test_reverse_order_also_restores_correctly(self):
        self.install_both()
        self.assertEqual(self.uninstall_b()[0], 0)
        self.assertEqual(_readb(self.settings_b), self.before_b)
        self.assertEqual(self.uninstall(only_statusline=True)[0], 0)
        self.assertEqual(_readb(self.settings), self.before_a)

    def test_a_dir_without_an_original_is_not_given_another_dirs_slot(self):
        # B had no statusLine when wrapped; the slot then holds A's value.
        with open(self.settings_b, "w") as fh:
            fh.write(dump({"k": 1}))
        before_b = _readb(self.settings_b)
        self.assertEqual(self.run_install("launchd", config=self.config_b(), statusline=True)[0], 0)
        self.assertEqual(self.sl()[0], 0)
        self.assertEqual(self.slot(), self.A_CMD)
        self.assertEqual(self.uninstall_b()[0], 0)
        self.assertEqual(_readb(self.settings_b), before_b)  # statusLine removed, not ORIGINAL-A
        self.assertEqual(self.slot(), self.A_CMD, "A's saved value survives B's restore")

    def test_legacy_config_with_a_matching_slot_restores_without_a_note(self):
        # Configs written before this change hold the same single key; reading them is unchanged.
        self.assertEqual(self.sl()[0], 0)
        self.assertEqual(self.slot(), self.A_CMD)
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("differs", out)
        self.assertEqual(_readb(self.settings), self.before_a)

    def test_legacy_config_with_no_slot_restores_from_the_wrapper(self):
        self.assertEqual(self.sl()[0], 0)
        data = json.loads(_read(self.cfg_path))
        del data["statusline_original"]
        self.write_config(data)
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("differs", out)
        self.assertEqual(_readb(self.settings), self.before_a)


class TestLookalikeWrappers(StatuslineCase):
    """QA finding F2: a hand-edited or lookalike wrapper is never replaced by a stale saved
    value; it is refused untouched with both values named."""

    def wrap_then_edit(self, edit):
        """Install over `echo STALE`, then let `edit(command)` produce a hand-edited one."""
        self.write_settings(dict(SETTINGS, statusLine={"type": "command", "command": "echo STALE"}))
        self.assertEqual(self.sl()[0], 0)
        data = self.settings_json()
        data["statusLine"]["command"] = edit(data["statusLine"]["command"])
        return self.write_settings(data)

    def assert_refused_untouched(self, edited):
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT restored", out)
        self.assertIn("echo STALE", out)       # the saved value is named ...
        self.assertIn("by hand", out)          # ... and the way forward is explained
        self.assertEqual(self.prompts, [])
        self.assertEqual(_readb(self.settings), edited)
        self.assertEqual(len(self.backups()), 1, "only the install backup")
        self.assertEqual(json.loads(_read(self.cfg_path))["statusline_original"], "echo STALE")

    def test_our_wrapper_with_a_hand_appended_tail_is_refused(self):
        self.assert_refused_untouched(self.wrap_then_edit(lambda c: c + " && echo done"))

    def test_a_lookalike_tap_of_another_script_is_refused(self):
        self.assert_refused_untouched(
            self.wrap_then_edit(lambda c: "python3 /x/grid-fleet tap -- /my/other.sh"))

    def test_a_hand_edited_sh_wrapper_is_restored_to_what_it_embeds(self):
        # Parseable (our exact form) but the embedded command was changed by hand: the
        # wrapper decides, the stale slot is only mentioned.
        self.wrap_then_edit(lambda c: install.tap_command(PY, SCRIPT, "echo HAND-EDITED"))
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.settings_json()["statusLine"]["command"], "echo HAND-EDITED")
        self.assertIn("echo STALE", out)
        self.assertIn("echo HAND-EDITED", out)


class TestMissingWrapperScript(StatuslineCase):
    """QA finding F3: a wrapper whose script is gone is still a no-op, but says so."""

    def test_missing_script_warns_with_the_path_and_the_fix(self):
        gone = "/synth-gone/clone/bin/grid-fleet"
        before = self.write_settings(dict(SETTINGS, statusLine={
            "type": "command", "command": install.tap_command(PY, gone, "echo x")}))
        rc, out, err = self.sl()
        self.assertEqual(rc, 0)
        self.assertIn("already wrapped", out)
        self.assertIn(gone, err)
        self.assertIn("does not exist", err)
        self.assertIn("--statusline --uninstall", err)
        self.assertIn("then `grid-fleet install --statusline`", err)
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.prompts, [])

    def test_existing_script_gives_no_warning(self):
        script = os.path.join(self.work, "grid-fleet")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\n")
        self.write_settings(dict(SETTINGS, statusLine={
            "type": "command", "command": install.tap_command(PY, script, "echo x")}))
        rc, out, err = self.sl()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("already wrapped", out)


class TestUnwritableAndLossyRewrites(StatuslineCase):
    """QA findings F4 and F5: refuse cleanly instead of a traceback or a lossy rewrite."""

    # Odd spacing makes the layout unreproducible; "\\ud800" is a lone-surrogate JSON escape.
    LONE_SURROGATE = '{"statusLine":{"command":"x"},   "k":"\\ud800"}'

    def assert_clean_refusal(self, text, expect):
        before = self.write_settings(text=text)
        rc, out, err = self.sl()
        self.assertEqual(rc, 1, err)
        self.assertIn(expect, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(_readb(self.settings), before)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.cfg_path), "config not touched either")
        self.assertEqual(self.prompts, [], "refused before the question")

    def test_lone_surrogate_in_an_unreproducible_layout_is_refused_cleanly(self):
        self.assert_clean_refusal(self.LONE_SURROGATE, "UTF-8")

    def test_lone_surrogate_in_a_reproducible_layout_is_fine(self):
        # ensure_ascii layouts write the escape back as ASCII; nothing to refuse.
        text = dump({"statusLine": {"type": "command", "command": "x"}, "k": "\ud800"},
                    ascii_=True)
        self.write_settings(text=text)
        rc, _, err = self.sl()
        self.assertEqual(rc, 0, err)

    def test_valueerror_during_the_settings_write_is_caught(self):
        before = self.write_settings()
        orig = install._write_atomic

        def boom(path, text, mode=0o644):
            if os.path.realpath(path).endswith("settings.json"):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "synthetic")
            return orig(path, text, mode)

        with mock.patch.object(install, "_write_atomic", boom):
            rc, _, err = self.sl()
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertEqual(_readb(self.settings), before)
        self.assertFalse(os.path.exists(self.cfg_path), "config is saved only after settings")

    def test_failed_settings_write_does_not_touch_an_existing_config(self):
        self.write_settings()
        self.write_config({"schema": 1, "statusline_original": "echo KEEP"})
        before_cfg = _readb(self.cfg_path)
        with mock.patch.object(install, "_write_atomic", side_effect=OSError("synthetic")):
            self.assertEqual(self.sl()[0], 1)
        self.assertEqual(_readb(self.cfg_path), before_cfg)

    def test_unreproducible_note_lists_what_can_change(self):
        self.write_settings(text='{"statusLine":{"command":"x"},   "k":1}')
        _, out, _ = self.sl()
        for word in ("duplicate keys", "CRLF", "number"):
            self.assertIn(word, out)
        self.assertNotIn("same values and key order", out)

    def test_duplicate_keys_and_crlf_are_really_what_changes(self):
        self.write_settings(text='{"a": 1,\r\n "a": 2, "statusLine": {"command": "x"}}')
        self.assertEqual(self.sl()[0], 0)
        text = _read(self.settings)
        self.assertEqual(text.count('"a"'), 1)
        self.assertNotIn("\r", text)

    def test_a_number_that_would_become_infinity_is_refused_untouched(self):
        self.assert_clean_refusal('{"statusLine":{"command":"x"},   "n":1e999}', "valid JSON")

    def test_nan_in_a_reproducible_layout_is_kept_as_is(self):
        text = ('{\n  "n": NaN,\n  "statusLine": {\n    "type": "command",\n'
                '    "command": "x"\n  }\n}\n')
        self.write_settings(text=text)
        self.assertEqual(self.sl()[0], 0)
        self.assertIn('"n": NaN', _read(self.settings))

    def test_restore_also_refuses_a_lossy_nonfinite_rewrite(self):
        # A wrapped file whose layout is not reproducible and holds 1e999 (hand-edited in
        # after the install): the restore must refuse rather than write `Infinity`.
        wrapped = ('{"n":1e999,   "statusLine":{"type":"command","command":%s}}'
                   % json.dumps(install.tap_command(PY, SCRIPT, "x")))
        self.write_settings(text=wrapped)
        rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 1)
        self.assertIn("NOT restored", out)
        self.assertEqual(_read(self.settings), wrapped)


class TestDetectionStrictness(unittest.TestCase):
    """Mutation guards for is_tap_command: it needs `tap` immediately followed by `--`
    right after the script word, not merely `tap` somewhere."""

    def test_tap_elsewhere_in_the_words_is_not_a_wrapper(self):
        for cmd in ("python3 /x/grid-fleet collect tap -- x",   # "tap" in words, wrong place
                    "python3 /x/grid-fleet tap /y/z.sh arg",    # tap without --
                    "python3 /x/grid-fleet tap x --",           # -- not adjacent
                    "python3 /x/grid-fleet view tap"):
            self.assertFalse(install.is_tap_command(cmd), cmd)


class TestFailedRestoreAndPromptRace(StatuslineCase):
    def test_failed_restore_exits_1(self):
        self.write_settings()
        self.sl()
        wrapped = _readb(self.settings)
        orig = install._write_atomic

        def boom(path, text, mode=0o644):
            if os.path.realpath(path).endswith("settings.json"):
                raise OSError("synthetic write failure")
            return orig(path, text, mode)

        with mock.patch.object(install, "_write_atomic", boom):
            rc, out, _ = self.uninstall(only_statusline=True)
        self.assertEqual(rc, 1)
        self.assertEqual(rc, install.EXIT_REFUSED)
        self.assertIn("NOT restored", out)
        self.assertEqual(_readb(self.settings), wrapped)

    def test_restore_aborts_when_settings_change_during_the_prompt(self):
        import contextlib
        import io
        self.write_settings()
        self.sl()
        self.config = self.make_config()
        changed = dump(dict(self.settings_json(), zeta=99))

        def prompt(_text):
            with open(self.settings, "w") as fh:  # Claude Code saved a setting meanwhile
                fh.write(changed)
            return "y"

        n_backups = len(self.backups())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = install.install_main(self.config, self.args(statusline=True, uninstall=True),
                                      runner=self.runner, env=self.env, platform="darwin",
                                      python=PY, script=SCRIPT, uid=1, is_tty=lambda: True,
                                      prompt=prompt)
        self.assertNotEqual(rc, 0)
        self.assertIn("changed while waiting", out.getvalue())
        self.assertEqual(_read(self.settings), changed)
        self.assertEqual(len(self.backups()), n_backups)


class TestRealTtyCheck(StatuslineCase):
    """The default is_tty (install._tty) must need BOTH stdin and stdout to be terminals.
    Streams are injected; the helper itself is not replaced."""

    def run_with_streams(self, stdin_tty, stdout_tty):
        import contextlib
        self.write_settings()
        self.config = self.make_config()
        prompts, err = [], _stream(False)
        out = _stream(stdout_tty)
        with mock.patch.object(sys, "stdin", _stream(stdin_tty)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = install.install_main(self.config, self.args(statusline=True), runner=self.runner,
                                      env=self.env, platform="darwin", python=PY, script=SCRIPT,
                                      uid=1, prompt=lambda t: prompts.append(t) or "y")
        return rc, prompts, err.getvalue()

    def test_both_terminals_proceeds(self):
        rc, prompts, _ = self.run_with_streams(True, True)
        self.assertEqual((rc, len(prompts)), (0, 1))

    def test_piped_stdin_with_terminal_stdout_refuses(self):
        rc, prompts, err = self.run_with_streams(False, True)
        self.assertEqual((rc, prompts), (1, []))
        self.assertIn("interactive terminal", err)
        self.assertEqual(_read(self.settings), dump(SETTINGS))

    def test_terminal_stdin_with_piped_stdout_refuses(self):
        rc, prompts, err = self.run_with_streams(True, False)
        self.assertEqual((rc, prompts), (1, []))
        self.assertIn("interactive terminal", err)
        self.assertEqual(_read(self.settings), dump(SETTINGS))


class TestSystemdUnloadReload(StatuslineCase):
    def test_unload_of_a_loaded_timer_with_no_files_still_reloads_the_daemon(self):
        # T14: the timer is registered ("loaded") but its unit files were deleted by hand.
        # Unloading it must still be followed by daemon-reload.
        runner = FakeRunner(loaded=True)
        rc, out, err = self.run_install("systemd", runner=runner, uninstall=True)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.sd_dir))
        self.assertEqual(runner.mutating(),
                         [["systemctl", "--user", "disable", "--now", install.SYSTEMD_STEM + ".timer"],
                          ["systemctl", "--user", "daemon-reload"]])


if __name__ == "__main__":
    unittest.main()
