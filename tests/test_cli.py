"""cli.py: --version, every subcommand is real (none are stubs), no-args behaviour."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from grid_fleet import __version__, cli

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin", "grid-fleet")


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Point config/state at a temp dir so no test reads or writes real user state.
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        patcher = mock.patch.dict(os.environ, {
            "GRID_FLEET_CONFIG": self.cfg_path,
            "GRID_FLEET_STATE_DIR": os.path.join(self.tmp.name, "state"),
            "GRID_FLEET_CLAUDE_DIR": os.path.join(self.tmp.name, "claude"),
            # Nonexistent binary so no test ever runs the real `claude` from PATH.
            "GRID_FLEET_CLAUDE_BIN": os.path.join(self.tmp.name, "no-such-claude"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:  # argparse --version/--help/usage errors
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()


class TestCli(CliCase):
    def test_version(self):
        rc, out, _ = self.run_cli(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn(__version__, out)

    def test_tap_is_real_not_a_stub(self):
        # tap with no original command prints nothing and exits 0 (limits-capture spec).
        # Stdin is an empty in-memory buffer so the test never blocks on a terminal.
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""))):
            rc, out, _ = self.run_cli(["tap"])
        self.assertEqual((rc, out), (0, ""))

    def test_tap_command_split_verbatim(self):
        # The wrapped command (including its own flags) must reach tap_main untouched.
        seen = {}

        def fake_tap(config, args):
            seen["cmd"] = args.original_command
            return 0

        with mock.patch("grid_fleet.limits.tap_main", fake_tap):
            rc, _, _ = self.run_cli(["tap", "--", "sh", "-c", "echo hi", "--version"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["cmd"], ["sh", "-c", "echo hi", "--version"])

    def test_tap_without_command_gets_empty_list(self):
        seen = {}
        with mock.patch("grid_fleet.limits.tap_main",
                        lambda c, a: seen.setdefault("cmd", a.original_command) is None):
            self.run_cli(["tap"])
        self.assertEqual(seen["cmd"], [])

    def test_no_args_prints_usage_and_exits_2(self):
        rc, out, err = self.run_cli([])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_unknown_subcommand_is_usage_error(self):
        rc, _, err = self.run_cli(["frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice", err)

    def test_dispatch_passes_config_and_args(self):
        got = {}

        def fake(config, args):
            got["config"], got["args"] = config, args
            return 7

        with open(self.cfg_path, "w") as fh:
            fh.write('{"schema": 1, "account": "acct"}')
        with mock.patch("grid_fleet.snapshot.collect_main", fake):
            rc, _, _ = self.run_cli(["collect"])
        self.assertEqual(rc, 7)
        self.assertEqual(got["config"].account, "acct")
        self.assertEqual(got["args"].command, "collect")

    def test_add_arguments_hook(self):
        # A home module can declare its own flags without editing cli.py.
        def hook(parser):
            parser.add_argument("--once", action="store_true")

        got = {}
        with mock.patch("grid_fleet.view.add_arguments", hook, create=True), \
                mock.patch("grid_fleet.view.view_main",
                           lambda c, a: got.setdefault("once", a.once) and 0):
            self.run_cli(["view", "--once"])
        self.assertTrue(got["once"])

    def test_bad_config_exits_3_but_not_for_tap(self):
        with open(self.cfg_path, "w") as fh:
            fh.write('{"schema": 99}')
        for name in ("collect", "push", "view", "install"):
            rc, _, err = self.run_cli([name])
            self.assertEqual(rc, cli.EXIT_CONFIG, name)
            self.assertIn("config error", err)
        # tap must survive a broken config: it falls back to defaults and runs
        # (no original command here, so it exits 0 and prints nothing).
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""))):
            rc, out, err = self.run_cli(["tap"])
        self.assertEqual((rc, out, err), (0, "", ""))


class TestEntryScript(CliCase):
    def test_bin_is_executable_with_env_shebang(self):
        self.assertTrue(os.access(BIN, os.X_OK))
        with open(BIN) as fh:
            self.assertEqual(fh.readline().strip(), "#!/usr/bin/env python3")

    def test_bin_version_and_help(self):
        # Run under the same interpreter as the suite (system 3.9 on macOS).
        r = subprocess.run([sys.executable, BIN, "--version"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn(__version__, r.stdout)
        # --help is stable and side-effect free; no subcommand is a stub any more.
        r = subprocess.run([sys.executable, BIN, "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage:", r.stdout)

    def test_bin_via_symlink(self):
        # Install is "symlink bin/grid-fleet onto PATH"; the package must still be found.
        link = os.path.join(self.tmp.name, "gf-link")
        os.symlink(BIN, link)
        r = subprocess.run([sys.executable, link, "--version"], capture_output=True, text=True,
                           cwd=self.tmp.name)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestHubPaths(unittest.TestCase):
    def test_inbox_paths(self):
        from grid_fleet import hub
        self.assertEqual(hub.inbox_dir("/s"), "/s/inbox")
        self.assertEqual(hub.inbox_path("/s", "alpha"), "/s/inbox/alpha.json")
        self.assertEqual(hub.inbox_tmp_path("/s", "alpha"), "/s/inbox/.alpha.json.tmp")

    def test_unsafe_host_refused(self):
        from grid_fleet import hub
        for bad in ("../x", "a/b", ".hidden", "", "a b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hub.inbox_path("/s", bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                hub.inbox_tmp_path("/s", bad)


if __name__ == "__main__":
    unittest.main()
