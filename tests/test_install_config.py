"""Tests for the config keys `install` writes (`interval_s`, `hub`, `account`) and the
refusal to install without a hub. Spec: specs/install-timers/spec.md, requirements
"Config written by install" and "Hub required to apply".

Nothing real is touched: HOME, the config file and the state dir are temp dirs, the
scheduler is the FakeRunner from test_install (no launchd, no systemd, no ssh), and the
platform / uid / tty / prompt are injected. The one subprocess test only runs refusing
paths and the read-only plan, which never reach a scheduler. All values are synthetic.

These are the author's own self-checks, not independent verification.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from grid_fleet import cli, install
from grid_fleet import config as cfg

from test_install import SYNTH_PATH, FakeRunner, InstallCase, _read, _readb

HOSTILE_HUBS = ["-oProxyCommand=x", "a b", "a;b", "$(x)", "a/b", "a\nb", "", "`x`", "a'b"]
HOSTILE_ACCOUNTS = ["", " lead", "trail ", "a\nb", "a\tb", "café", "x" * 33, "\x1b[31m"]


class ConfigCase(InstallCase):
    """InstallCase plus helpers for a config file at self.cfg_path (dir NOT yet created)."""

    def setUp(self):
        super().setUp()
        self.cfg_dir = os.path.dirname(self.cfg_path)
        # What cli would hand over for "no config file": all defaults, no hub.
        self.nohub = cfg.Config(host_name="synth-host", config_path=self.cfg_path,
                                state_dir=self.state)

    def write_config(self, data=None, text=None, mode=0o600):
        os.makedirs(self.cfg_dir, exist_ok=True)
        with open(self.cfg_path, "w") as fh:
            fh.write(text if text is not None else json.dumps(data, indent=2) + "\n")
        os.chmod(self.cfg_path, mode)

    def config_json(self):
        return json.loads(_read(self.cfg_path))

    def loaded_config(self):
        """The Config as cli would build it from the file now (separate process in real life)."""
        return cfg.load_config({"GRID_FLEET_CONFIG": self.cfg_path,
                                "GRID_FLEET_STATE_DIR": self.state, "HOME": self.home})

    def mode(self, path):
        return os.stat(path).st_mode & 0o777


class TestFlagValidation(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser("install").parse_args(["install"] + list(argv))

    def test_hostile_hub_values_are_refused_by_the_push_validator(self):
        for bad in HOSTILE_HUBS:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                self.parse("--hub=" + bad)  # `=` form so "-o..." reaches the validator
            self.assertEqual(cm.exception.code, 2, repr(bad))
            self.assertIn("invalid hub", err.getvalue(), repr(bad))

    def test_local_and_plain_aliases_are_accepted(self):
        for ok in ("local", "hub-alias", "user@host.example:22"):
            self.assertEqual(self.parse("--hub", ok).hub, ok)

    def test_hub_validation_is_the_one_push_uses(self):
        # Same function object, so the two can never drift apart.
        from grid_fleet import push
        for value in HOSTILE_HUBS + ["local", "hub-alias"]:
            ok = False
            try:
                with contextlib.redirect_stderr(io.StringIO()):  # argparse prints its error
                    self.parse("--hub=" + value)
                ok = True
            except SystemExit:
                pass
            self.assertEqual(ok, push.validate_hub(value) is None, repr(value))

    def test_hostile_accounts_are_refused(self):
        for bad in HOSTILE_ACCOUNTS:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                self.parse("--account", bad)
            self.assertEqual(cm.exception.code, 2, repr(bad))

    def test_plain_accounts_are_accepted(self):
        for ok in ("default", "team-a", "Team A", "x" * 32):
            self.assertEqual(self.parse("--account", ok).account, ok)

    def test_defaults_are_none_so_config_values_survive(self):
        ns = self.parse()
        self.assertIsNone(ns.hub)
        self.assertIsNone(ns.account)
        self.assertIsNone(ns.interval)


class TestProgrammaticValidation(ConfigCase):
    """install_main re-checks the values: programmatic callers bypass argparse."""

    def test_hostile_hub_flag_is_usage_error_and_writes_nothing(self):
        rc, _, err = self.run_install(config=self.nohub, apply=True, hub="-oProxyCommand=x")
        self.assertEqual(rc, install.EXIT_USAGE)
        self.assertIn("--hub", err)
        self.assertEqual(self.tree(), [self.home])
        self.assertEqual(self.runner.calls, [])

    def test_hostile_account_flag_is_usage_error_and_writes_nothing(self):
        rc, _, err = self.run_install(config=self.nohub, apply=True, hub="h", account="a\nb")
        self.assertEqual(rc, install.EXIT_USAGE)
        self.assertEqual(self.tree(), [self.home])


class TestPlanShowsConfig(ConfigCase):
    def test_plan_shows_before_after_of_the_three_keys_and_changes_nothing(self):
        rc, out, err = self.run_install(config=self.nohub, hub="hub-alias", interval=900,
                                        account="team-a")
        self.assertEqual(rc, 0, err)
        self.assertIn("(will be created)", out)
        self.assertIn('interval_s (absent) -> 900', out)
        self.assertIn('hub        (absent) -> "hub-alias"', out)
        self.assertIn('account    (absent) -> "team-a"', out)
        self.assertEqual(self.tree(), [self.home], "plan writes nothing, not even the config dir")
        self.assertEqual(self.runner.calls, [])

    def test_plan_for_existing_config_shows_old_to_new(self):
        self.write_config({"schema": 1, "interval_s": 600, "hub": "old-hub", "account": "default"})
        rc, out, _ = self.run_install(config=self.loaded_config(), hub="new-hub", interval=300)
        self.assertEqual(rc, 0)
        self.assertIn("(will be updated)", out)
        self.assertIn('interval_s 600 -> 300', out)
        self.assertIn('hub        "old-hub" -> "new-hub"', out)
        self.assertIn('account    "default" (unchanged)', out)

    def test_old_interval_mismatch_warning_is_gone(self):
        # apply now writes interval_s, so the two cannot disagree; the plan shows the change.
        self.write_config({"schema": 1, "interval_s": 600, "hub": "h"})
        _, out, _ = self.run_install(config=self.loaded_config(), interval=900)
        self.assertNotIn("differs", out)
        self.assertNotIn("warning", out)
        self.assertIn("interval_s 600 -> 900", out)

    def test_plan_without_hub_warns_and_still_prints(self):
        rc, out, _ = self.run_install(config=self.nohub)
        self.assertEqual(rc, 0)
        self.assertIn("warning: no hub", out)
        self.assertIn("files:", out)

    def test_plan_is_deterministic(self):
        a = self.run_install(config=self.nohub, hub="h")[1]
        self.assertEqual(a, self.run_install(config=self.nohub, hub="h")[1])


class TestConfigCreated(ConfigCase):
    def test_created_with_schema_host_name_and_the_three_keys(self):
        rc, out, err = self.run_install(config=self.nohub, apply=True, hub="hub-alias",
                                        interval=300, account="team-a")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.config_json(), {"schema": 1, "host_name": "synth-host",
                                              "interval_s": 300, "hub": "hub-alias",
                                              "account": "team-a"})
        self.assertEqual(list(self.config_json()),
                         ["schema", "host_name", "interval_s", "hub", "account"])
        self.assertIn("config written", out)

    def test_defaults_when_only_hub_is_given(self):
        self.run_install(config=self.nohub, apply=True, hub="local")
        data = self.config_json()
        self.assertEqual((data["interval_s"], data["hub"], data["account"]),
                         (600, "local", "default"))

    def test_file_is_0600_and_the_dir_we_created_is_0700(self):
        self.run_install(config=self.nohub, apply=True, hub="h")
        self.assertEqual(self.mode(self.cfg_path), 0o600)
        self.assertEqual(self.mode(self.cfg_dir), 0o700)

    def test_existing_config_dir_mode_is_never_changed(self):
        os.makedirs(self.cfg_dir)
        os.chmod(self.cfg_dir, 0o755)
        self.run_install(config=self.nohub, apply=True, hub="h")
        self.assertEqual(self.mode(self.cfg_dir), 0o755)
        self.assertEqual(self.mode(self.cfg_path), 0o600)

    def test_created_file_loads_back_through_load_config(self):
        self.run_install(config=self.nohub, apply=True, hub="hub-alias", interval=900,
                         account="team-a")
        loaded = self.loaded_config()
        self.assertEqual((loaded.interval_s, loaded.hub, loaded.account, loaded.host_name),
                         (900, "hub-alias", "team-a", "synth-host"))

    def test_no_temp_file_left_behind(self):
        self.run_install(config=self.nohub, apply=True, hub="h")
        self.assertEqual(os.listdir(self.cfg_dir), ["config.json"])


class TestConfigUpdated(ConfigCase):
    EXISTING = {"schema": 1, "zeta": {"k": [1, 2]}, "hub": "old-hub",
                "view": {"ascii": True, "refresh_s": 9}, "futureKey": None,
                "statusline_original": "synth-cmd --x"}

    def test_updates_in_place_preserving_unknown_keys_and_order(self):
        self.write_config(self.EXISTING)
        rc, _, err = self.run_install(config=self.loaded_config(), apply=True, hub="new-hub",
                                      interval=300, account="team-a")
        self.assertEqual(rc, 0, err)
        data = self.config_json()
        # hub keeps its slot; the two new keys append in order interval_s, account.
        self.assertEqual(list(data), ["schema", "zeta", "hub", "view", "futureKey",
                                      "statusline_original", "interval_s", "account"])
        expected = dict(self.EXISTING, hub="new-hub", interval_s=300, account="team-a")
        self.assertEqual(data, expected)

    def test_existing_file_layout_and_mode_are_kept(self):
        self.write_config(self.EXISTING, mode=0o640)
        self.run_install(config=self.loaded_config(), apply=True, hub="new-hub")
        self.assertEqual(self.mode(self.cfg_path), 0o640)
        text = _read(self.cfg_path)
        self.assertEqual(text, json.dumps(json.loads(text), indent=2) + "\n")

    def test_host_name_is_not_added_to_an_existing_file(self):
        self.write_config({"schema": 1})
        self.run_install(config=self.loaded_config(), apply=True, hub="h")
        self.assertNotIn("host_name", self.config_json())

    def test_existing_host_name_is_untouched(self):
        self.write_config({"schema": 1, "host_name": "keep-me"})
        self.run_install(config=self.loaded_config(), apply=True, hub="h")
        self.assertEqual(self.config_json()["host_name"], "keep-me")

    def test_hub_from_config_is_used_when_no_flag(self):
        self.write_config({"schema": 1, "hub": "cfg-hub"})
        rc, _, err = self.run_install(config=self.loaded_config(), apply=True, interval=900)
        self.assertEqual(rc, 0, err)
        data = self.config_json()
        self.assertEqual((data["hub"], data["interval_s"], data["account"]),
                         ("cfg-hub", 900, "default"))

    def test_interval_written_matches_the_timer(self):
        # The point of the change: timer interval and config interval_s cannot disagree.
        self.write_config({"schema": 1, "interval_s": 600, "hub": "h"})
        self.run_install("launchd", config=self.loaded_config(), apply=True, interval=900)
        self.assertEqual(self.config_json()["interval_s"], 900)
        import plistlib
        self.assertEqual(plistlib.loads(_readb(self.plist))["StartInterval"], 900)
        self.assertEqual(self.loaded_config().interval_s, 900)

    def test_symlinked_config_stays_a_symlink(self):
        real = os.path.join(self.work, "dotfiles-config.json")
        with open(real, "w") as fh:
            json.dump({"schema": 1}, fh)
        os.makedirs(self.cfg_dir)
        os.symlink(real, self.cfg_path)
        self.run_install(config=self.loaded_config(), apply=True, hub="h")
        self.assertTrue(os.path.islink(self.cfg_path))
        self.assertEqual(json.loads(_read(real))["hub"], "h")


class TestConfigRefusedUntouched(ConfigCase):
    def check_refused(self, text, mode_args):
        self.write_config(text=text)
        before = _readb(self.cfg_path)
        # Config built by hand: cli's load_config would already have refused this file.
        conf = cfg.Config(hub="h", config_path=self.cfg_path, state_dir=self.state)
        rc, out, err = self.run_install(config=conf, **mode_args)
        self.assertEqual(rc, install.EXIT_REFUSED, (text, mode_args))
        self.assertTrue(err.strip())
        self.assertEqual(_readb(self.cfg_path), before, "config must be byte-identical")
        self.assertEqual(self.runner.calls, [], "scheduler untouched")
        self.assertEqual(self.prompts, [])
        self.assertFalse(os.path.lexists(self.plist))

    def test_invalid_json_refused_in_apply_and_plan(self):
        for args in ({"apply": True}, {}):
            self.check_refused("{broken", args)

    def test_not_an_object_refused(self):
        self.check_refused("[1, 2]\n", {"apply": True})

    def test_unknown_schema_refused(self):
        for text in ('{"schema": 2}\n', '{"schema": true}\n', '{"hub": "h"}\n'):
            self.check_refused(text, {"apply": True})

    def test_invalid_json_message_names_the_file(self):
        self.write_config(text="{broken")
        conf = cfg.Config(hub="h", config_path=self.cfg_path, state_dir=self.state)
        _, _, err = self.run_install(config=conf, apply=True)
        self.assertIn(self.cfg_path, err)


class TestAtomicWrite(ConfigCase):
    def test_failed_write_leaves_the_old_file_and_no_temp_and_no_timer(self):
        self.write_config({"schema": 1, "hub": "old-hub"})
        before = _readb(self.cfg_path)
        orig = os.replace

        def boom(src, dst):
            raise OSError("synthetic rename failure")

        os.replace = boom
        try:
            rc, _, err = self.run_install(config=self.loaded_config(), apply=True, hub="new-hub")
        finally:
            os.replace = orig
        self.assertNotEqual(rc, 0)
        self.assertIn("synthetic rename failure", err)
        self.assertEqual(_readb(self.cfg_path), before)
        self.assertEqual(os.listdir(self.cfg_dir), ["config.json"], "no temp file left")
        self.assertEqual(self.runner.mutating(), [], "config failed first: timer never loaded")
        self.assertFalse(os.path.lexists(self.plist))

    def test_config_changed_while_waiting_for_the_y_is_not_overwritten(self):
        self.write_config({"schema": 1, "hub": "old-hub"})
        conf = self.loaded_config()
        racing = '{"schema": 1, "hub": "edited-meanwhile"}\n'

        def answer(_text):
            with open(self.cfg_path, "w") as fh:
                fh.write(racing)
            return "y"

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = install.install_main(
                conf, self.args(apply=True, hub="new-hub"), runner=self.runner, env=self.env,
                platform="darwin", python="/opt/synth/bin/python3",
                script="/srv/synth/grid-fleet/bin/grid-fleet", uid=4242,
                is_tty=lambda: True, prompt=answer)
        self.assertEqual(rc, install.EXIT_REFUSED)
        self.assertEqual(_read(self.cfg_path), racing)
        self.assertIn("changed while waiting", err.getvalue())
        self.assertEqual(self.runner.mutating(), [])


class TestRefuseWithoutHub(ConfigCase):
    def test_apply_without_hub_exits_1_naming_flag_and_key_and_writes_nothing(self):
        for tty in (True, False):  # the refusal comes before the TTY check
            rc, out, err = self.run_install(config=self.nohub, apply=True, tty=tty)
            self.assertEqual(rc, 1, tty)
            self.assertIn("--hub", err)
            self.assertIn("`hub`", err)
            self.assertIn(self.cfg_path, err)
            self.assertIn("files:", out, "the plan still prints")
            self.assertIn("warning: no hub", out)
            self.assertEqual(self.tree(), [self.home], "nothing written, no dirs made")
            self.assertEqual(self.runner.calls, [], "scheduler not even queried")
            self.assertEqual(self.prompts, [], "no confirmation asked")

    def test_other_flags_do_not_make_up_for_a_missing_hub(self):
        rc, _, err = self.run_install(config=self.nohub, apply=True, interval=900,
                                      account="team-a")
        self.assertEqual(rc, 1)
        self.assertEqual(self.tree(), [self.home])

    def test_unusable_hub_already_in_config_is_refused_too(self):
        bad = cfg.Config(hub="-oProxyCommand=x", config_path=self.cfg_path, state_dir=self.state)
        rc, out, err = self.run_install(config=bad, apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("not usable", err)
        self.assertIn("not usable", out)  # the plan warns as well
        self.assertEqual(self.tree(), [self.home])
        self.assertEqual(self.runner.calls, [])

    def test_hub_flag_fixes_an_unusable_config_hub(self):
        bad = cfg.Config(hub="-oProxyCommand=x", config_path=self.cfg_path, state_dir=self.state)
        rc, _, err = self.run_install(config=bad, apply=True, hub="good-hub")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.config_json()["hub"], "good-hub")

    def test_plan_only_without_hub_still_exits_zero(self):
        self.assertEqual(self.run_install(config=self.nohub)[0], 0)

    def test_cli_process_refuses_without_hub_under_a_temp_home(self):
        # The real entry point, stdin from /dev/null (no TTY, cannot confirm). Refusal and
        # the plan never reach launchctl/systemctl, so nothing real is involved.
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "bin", "grid-fleet")
        # PYTHONDONTWRITEBYTECODE: Apple's python otherwise writes its bytecode cache
        # under $HOME/Library/Caches, which would look like install writing into HOME.
        env = {"HOME": self.home, "PATH": SYNTH_PATH, "PYTHONDONTWRITEBYTECODE": "1",
               "GRID_FLEET_CONFIG": self.cfg_path, "GRID_FLEET_STATE_DIR": self.state,
               "GRID_FLEET_CLAUDE_DIR": os.path.join(self.work, "claude")}
        for argv in (["install", "--apply"], ["install", "--apply", "--interval", "300"]):
            r = subprocess.run([sys.executable, script] + argv, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("--hub", r.stderr)
            self.assertIn("warning: no hub", r.stdout)
            self.assertEqual(self.tree(), [self.home])


class TestApplyTwice(ConfigCase):
    def test_second_identical_apply_is_a_clean_noop_including_config(self):
        for kind in ("launchd", "systemd"):
            self.runner = FakeRunner()
            args = dict(config=self.nohub, apply=True, hub="hub-alias", interval=300,
                        account="team-a")
            self.assertEqual(self.run_install(kind, **args)[0], 0)
            before = {p: _readb(p) for p in self.tree() if os.path.isfile(p)}
            self.runner.calls.clear()
            rc, out, err = self.run_install(kind, **dict(args, config=self.loaded_config()))
            self.assertEqual(rc, 0, err)
            self.assertIn("nothing to change", out)
            self.assertEqual(self.prompts, [])
            self.assertEqual(self.runner.mutating(), [])
            self.assertEqual({p: _readb(p) for p in self.tree() if os.path.isfile(p)}, before)
            self.assertNotIn("config written", out)
            # reset for the next platform
            os.remove(self.cfg_path)

    def test_flags_omitted_the_second_time_still_noop(self):
        self.run_install(config=self.nohub, apply=True, hub="hub-alias", interval=900)
        self.runner.calls.clear()
        rc, out, _ = self.run_install(config=self.loaded_config(), apply=True)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to change", out)
        self.assertEqual(self.runner.mutating(), [])

    def test_config_only_change_updates_config_without_touching_the_loaded_timer(self):
        self.run_install(config=self.nohub, apply=True, hub="hub-alias")
        self.runner.calls.clear()
        rc, out, err = self.run_install(config=self.loaded_config(), apply=True, hub="other-hub")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.prompts), 1, "a real change is still confirmed")
        self.assertEqual(self.config_json()["hub"], "other-hub")
        self.assertEqual(self.runner.mutating(), [], "timer files unchanged and loaded")
        self.assertIn("config written", out)


class TestOrder(ConfigCase):
    def test_config_is_written_before_the_scheduler_is_called(self):
        seen = []
        outer = self

        class Spy(FakeRunner):
            def __call__(self, argv):
                if list(argv)[:2] == ["launchctl", "bootstrap"]:
                    # The first tick can fire the moment this succeeds: the config must
                    # already hold the hub by now.
                    seen.append(json.loads(_read(outer.cfg_path)) if os.path.exists(outer.cfg_path)
                                else None)
                return super().__call__(argv)

        self.run_install("launchd", config=self.nohub, runner=Spy(), apply=True, hub="hub-alias",
                         interval=900)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], "config must exist before bootstrap")
        self.assertEqual((seen[0]["hub"], seen[0]["interval_s"]), ("hub-alias", 900))

    def test_same_order_on_systemd(self):
        seen = []
        outer = self

        class Spy(FakeRunner):
            def __call__(self, argv):
                if "enable" in list(argv):
                    seen.append(os.path.exists(outer.cfg_path))
                return super().__call__(argv)

        self.run_install("systemd", config=self.nohub, runner=Spy(), apply=True, hub="h")
        self.assertEqual(seen, [True])

    def test_declined_confirmation_writes_no_config(self):
        rc, out, _ = self.run_install(config=self.nohub, apply=True, hub="h", answer="n")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.tree(), [self.home])
        self.assertEqual(self.runner.mutating(), [])

    def test_foreign_unit_file_blocks_before_any_config_write(self):
        os.makedirs(os.path.dirname(self.plist))
        with open(self.plist, "w") as fh:
            fh.write("not ours")
        rc, _, _ = self.run_install(config=self.nohub, apply=True, hub="h")
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(self.cfg_path))


class TestUninstallAndStatuslineLeaveConfigAlone(ConfigCase):
    def test_uninstall_leaves_hub_account_interval_in_the_config(self):
        self.run_install(config=self.nohub, apply=True, hub="hub-alias", interval=300,
                         account="team-a")
        before = _readb(self.cfg_path)
        rc, out, err = self.run_install(config=self.loaded_config(), uninstall=True)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.lexists(self.plist), "timer files are gone")
        self.assertEqual(_readb(self.cfg_path), before, "config is byte-identical")

    def test_uninstall_and_statusline_refuse_hub_and_account_flags(self):
        for extra in ({"uninstall": True}, {"statusline": True}):
            for flag in ({"hub": "h"}, {"account": "a"}):
                rc, _, err = self.run_install(config=self.nohub, **dict(extra, **flag))
                self.assertEqual(rc, install.EXIT_USAGE, (extra, flag))
                self.assertIn("--hub and --account", err)
        self.assertEqual(self.tree(), [self.home])

    def test_statusline_path_does_not_write_these_keys(self):
        claude = os.path.join(self.work, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "synth"}}, fh, indent=2)
            fh.write("\n")
        conf = cfg.Config(host_name="synth-host", config_path=self.cfg_path,
                          state_dir=self.state, claude_dir=claude)
        rc, _, err = self.run_install(config=conf, statusline=True)
        self.assertEqual(rc, 0, err)
        data = self.config_json()
        for key in ("interval_s", "hub", "account"):
            self.assertNotIn(key, data)
        self.assertEqual(data["statusline_original"], "synth")


if __name__ == "__main__":
    unittest.main()
