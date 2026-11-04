"""Tests for the statusLine tap (Group 7): specs/limits-capture/spec.md.

The tap is exercised as a real subprocess (bin/grid-fleet tap -- ...) so stdin bytes,
environment, stdout and exit code are the real thing. Everything runs in temp dirs with
GRID_FLEET_* overrides and a synthetic "original statusLine" script; no test touches the
real ~/.claude, a real settings.json, the network or ssh.
These are the author's own self-checks, not independent verification.
"""

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from helpers.claude_tree import SYNTH_CWD, SYNTH_SESSION_NAME

from grid_fleet import limits

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin", "grid-fleet")
LIMITS_SRC = os.path.join(REPO, "grid_fleet", "limits.py")

NOW = 1792591380  # fixed clock via GRID_FLEET_NOW

# Synthetic original statusLine: prints a fingerprint of what it received (stdin hash,
# a gating env var), echoes a stderr line, and exits with $ORIG_RC (default 3, non-zero
# on purpose so exit-code passthrough is really exercised). Gated like the real pattern:
# with GATE unset it prints nothing and exits 0.
ORIGINAL = r"""
import hashlib, os, sys
data = sys.stdin.buffer.read()
if not os.environ.get("SYNTH_GATE"):
    sys.exit(0)
sys.stdout.write("stdin-sha=%s len=%d gate=%s\n" % (hashlib.sha256(data).hexdigest(), len(data), os.environ["SYNTH_GATE"]))
sys.stdout.write("env=%s\n" % hashlib.sha256(repr(sorted(os.environ.items())).encode()).hexdigest())
sys.stderr.write("synthetic stderr\n")
sys.exit(int(os.environ.get("ORIG_RC", "3")))
"""


def payload(five=None, seven=None, extra=None, rate_limits=True):
    """A synthetic statusLine payload. Includes identifying fields that must never be stored."""
    body = {
        "cwd": SYNTH_CWD,
        "session_id": SYNTH_SESSION_NAME,
        "transcript_path": "/synthetic/path/transcript-secret.jsonl",
        "model": {"id": "synthetic-model", "display_name": "Synthetic"},
        "workspace": {"current_dir": SYNTH_CWD},
    }
    if rate_limits:
        rl = {}
        if five is not None:
            rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1], "extra_in_window": "x"}
        if seven is not None:
            rl["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1], "extra_in_window": "x"}
        body["rate_limits"] = rl
    body.update(extra or {})
    return json.dumps(body).encode("utf-8")


class TapBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="gf-limits-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.state = os.path.join(self.work, "state")
        self.script = os.path.join(self.work, "original.py")
        with open(self.script, "w") as fh:
            fh.write(ORIGINAL)
        self.limits_file = os.path.join(self.state, "limits.json")

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "GRID_FLEET_STATE_DIR": self.state,
            # Nonexistent config file = defaults; never the user's real config.
            "GRID_FLEET_CONFIG": os.path.join(self.work, "no-config.json"),
            "GRID_FLEET_NOW": str(NOW),
        })
        env.update(extra)
        return env

    def tap(self, data, original=True, env=None, command=None):
        """Run `grid-fleet tap -- <original>` with `data` on stdin. Returns CompletedProcess."""
        argv = [sys.executable, BIN, "tap"]
        if command is not None:
            argv += ["--"] + command
        elif original:
            argv += ["--", sys.executable, self.script]
        return subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env or self.env(), cwd=self.work)

    def direct(self, data, env):
        return subprocess.run([sys.executable, self.script], input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, cwd=self.work)

    def read(self):
        with open(self.limits_file) as fh:
            return json.load(fh)

    def state_listing(self):
        return sorted(os.listdir(self.state)) if os.path.isdir(self.state) else []


class TestTransparentChaining(TapBase):
    def test_output_identical(self):
        # Scenario: Output identical (stdout, exit code, and the stdin bytes the original saw)
        env = self.env(SYNTH_GATE="on")
        data = payload(five=(42.0, NOW + 100), seven=(18.0, NOW + 1000))
        direct, tapped = self.direct(data, env), self.tap(data, env=env)
        self.assertEqual(tapped.stdout, direct.stdout)
        self.assertEqual(tapped.returncode, direct.returncode)
        self.assertEqual(tapped.returncode, 3)
        self.assertIn(hashlib.sha256(data).hexdigest().encode(), tapped.stdout)
        # stderr passes through too (inherited, not swallowed).
        self.assertEqual(tapped.stderr, direct.stderr)

    def test_stdin_bytes_identical_for_non_json_and_non_utf8(self):
        env = self.env(SYNTH_GATE="on")
        for data in (b"\xff\xfe\x00not json at all\n", b"", b"{\"rate_limits\": ", b"\r\n\r\n" * 1000):
            direct, tapped = self.direct(data, env), self.tap(data, env=env)
            self.assertEqual(tapped.stdout, direct.stdout, data[:20])
            self.assertEqual(tapped.returncode, direct.returncode)
            # Unparseable payload: tolerated, and nothing is written.
            self.assertEqual(self.state_listing(), [])

    def test_exit_code_zero_and_other_codes_pass_through(self):
        for rc in ("0", "1", "7", "255"):
            env = self.env(SYNTH_GATE="on", ORIG_RC=rc)
            self.assertEqual(self.tap(payload(), env=env).returncode, int(rc))

    def test_environment_preserved(self):
        # Scenario: Environment preserved. The original sees the same environment whether or
        # not the tap is in front of it (its fingerprint hashes the whole environment), and
        # the gating var gates identically in both cases.
        for gate in ("", "on"):
            env = self.env(SYNTH_GATE=gate, SYNTH_ODD="v a lüe")
            direct, tapped = self.direct(payload(), env), self.tap(payload(), env=env)
            self.assertEqual(tapped.stdout, direct.stdout, "gate=%r" % gate)
            self.assertEqual(tapped.returncode, direct.returncode)
        self.assertEqual(self.tap(payload(), env=self.env(SYNTH_GATE="")).stdout, b"")

    def test_original_command_flags_are_passed_verbatim(self):
        r = self.tap(b"hello", command=[sys.executable, "-c", "import sys; print(sys.argv[1:]); sys.exit(0)",
                                        "--flag", "-x", "a b"])
        self.assertEqual(r.stdout.decode().strip(), "['--flag', '-x', 'a b']")

    def test_no_original_command(self):
        # Scenario: No original command -> prints nothing, exits 0 (and still captures).
        r = self.tap(payload(five=(1.0, NOW + 10)), original=False)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertIn("five_hour", self.read())

    def test_original_that_cannot_start(self):
        r = self.tap(payload(five=(1.0, NOW + 10)), command=[os.path.join(self.work, "no-such-command")])
        self.assertEqual((r.returncode, r.stdout), (127, b""))
        self.assertNotIn(self.work.encode(), r.stderr)  # no paths in our own message
        self.assertIn("five_hour", self.read())  # capture is independent of the original

    def test_original_that_ignores_stdin(self):
        r = self.tap(payload(five=(1.0, NOW + 10)) * 5000, command=["true"])
        self.assertEqual(r.returncode, 0)

    def test_tap_failure_cannot_break_the_statusline(self):
        # Scenario: Tap failure cannot break the statusLine. The state "dir" is under a
        # regular file, so creating it fails.
        blocker = os.path.join(self.work, "blocker")
        with open(blocker, "w") as fh:
            fh.write("x")
        env = self.env(SYNTH_GATE="on", GRID_FLEET_STATE_DIR=os.path.join(blocker, "state"))
        data = payload(five=(42.0, NOW + 100))
        direct, tapped = self.direct(data, env), self.tap(data, env=env)
        self.assertEqual(tapped.stdout, direct.stdout)
        self.assertEqual(tapped.returncode, 3)

    def test_unwritable_existing_file_still_passes_through(self):
        # The write fails at the rename step rather than at mkdir: the target is a directory.
        os.makedirs(self.limits_file)
        env = self.env(SYNTH_GATE="on")
        r = self.tap(payload(five=(42.0, NOW + 100)), env=env)
        self.assertEqual(r.returncode, 3)
        self.assertTrue(r.stdout.startswith(b"stdin-sha="))
        self.assertEqual([n for n in self.state_listing() if n.endswith(".tmp")], [])

    def test_broken_config_does_not_break_the_tap(self):
        bad = os.path.join(self.work, "bad.json")
        with open(bad, "w") as fh:
            fh.write('{"schema": 99}')
        env = self.env(SYNTH_GATE="on", GRID_FLEET_CONFIG=bad)
        r = self.tap(payload(five=(42.0, NOW + 100)), env=env)
        self.assertEqual(r.returncode, 3)
        # Falls back to the env-resolved state dir, not the current directory.
        self.assertIn("five_hour", self.read())
        self.assertFalse(os.path.exists(os.path.join(self.work, "limits.json")))


class TestSliceCapture(TapBase):
    def test_both_windows_present(self):
        # Scenario: Both windows present -> both windows, nothing else from the payload.
        self.tap(payload(five=(42.5, NOW + 100), seven=(18, NOW + 1000)))
        self.assertEqual(self.read(), {
            "schema": 1, "captured_at": NOW,
            "five_hour": {"used_pct": 42.5, "resets_at": NOW + 100},
            "seven_day": {"used_pct": 18, "resets_at": NOW + 1000},
        })

    def test_file_mode_is_0600_and_dir_0700(self):
        self.tap(payload(five=(1.0, NOW + 100)))
        self.assertEqual(stat.S_IMODE(os.stat(self.limits_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        # Rewrite keeps 0600 as well.
        self.tap(payload(five=(2.0, NOW + 100)))
        self.assertEqual(stat.S_IMODE(os.stat(self.limits_file).st_mode), 0o600)

    def test_only_five_hour_fresh_file(self):
        self.tap(payload(five=(42.0, NOW + 100)))
        body = self.read()
        self.assertEqual(set(body), {"schema", "captured_at", "five_hour"})

    def test_only_seven_day_fresh_file(self):
        self.tap(payload(seven=(9.0, NOW + 100)))
        self.assertEqual(set(self.read()), {"schema", "captured_at", "seven_day"})

    def test_absent_window_keeps_previous_while_in_future(self):
        # Scenario: One window absent (previous value still valid -> kept)
        self.tap(payload(five=(10.0, NOW + 100), seven=(20.0, NOW + 5000)))
        self.tap(payload(five=(11.0, NOW + 100)), env=self.env(GRID_FLEET_NOW=str(NOW + 50)))
        body = self.read()
        self.assertEqual(body["five_hour"]["used_pct"], 11.0)
        self.assertEqual(body["seven_day"], {"used_pct": 20.0, "resets_at": NOW + 5000})
        # Known limitation, pinned: the single file-level captured_at moves to the new write.
        self.assertEqual(body["captured_at"], NOW + 50)

    def test_absent_window_keeps_previous_other_direction(self):
        self.tap(payload(five=(10.0, NOW + 5000), seven=(20.0, NOW + 9000)))
        self.tap(payload(seven=(21.0, NOW + 9000)))
        body = self.read()
        self.assertEqual(body["five_hour"], {"used_pct": 10.0, "resets_at": NOW + 5000})
        self.assertEqual(body["seven_day"]["used_pct"], 21.0)

    def test_expired_window_is_not_carried_forward(self):
        # Scenario: One window absent (previous resets_at has passed -> omitted)
        self.tap(payload(five=(10.0, NOW + 100), seven=(20.0, NOW + 500)))
        later = self.env(GRID_FLEET_NOW=str(NOW + 600))  # seven_day's resets_at is now in the past
        self.tap(payload(five=(12.0, NOW + 9000)), env=later)
        self.assertEqual(set(self.read()), {"schema", "captured_at", "five_hour"})

    def test_window_resetting_exactly_now_is_not_carried(self):
        # resets_at == now is "no longer in the future".
        self.tap(payload(five=(10.0, NOW + 100), seven=(20.0, NOW + 500)))
        self.tap(payload(five=(12.0, NOW + 9000)), env=self.env(GRID_FLEET_NOW=str(NOW + 500)))
        self.assertNotIn("seven_day", self.read())

    def test_new_value_replaces_old_even_if_old_is_still_future(self):
        self.tap(payload(five=(10.0, NOW + 100)))
        self.tap(payload(five=(55.0, NOW + 200)))
        self.assertEqual(self.read()["five_hour"], {"used_pct": 55.0, "resets_at": NOW + 200})

    def test_no_rate_limits_creates_nothing(self):
        # Scenario: Fresh session
        self.tap(payload(rate_limits=False))
        self.assertEqual(self.state_listing(), [])

    def test_no_rate_limits_leaves_existing_file_byte_identical(self):
        self.tap(payload(five=(10.0, NOW + 100), seven=(20.0, NOW + 500)))
        with open(self.limits_file, "rb") as fh:
            before = fh.read()
        mtime = os.stat(self.limits_file).st_mtime_ns
        for data in (payload(rate_limits=False), payload(), b"not json", b"[]", b"null",
                     json.dumps({"rate_limits": None}).encode(),
                     json.dumps({"rate_limits": "x"}).encode(),
                     json.dumps({"rate_limits": {"five_hour": {"used_percentage": "42", "resets_at": 1}}}).encode(),
                     json.dumps({"rate_limits": {"five_hour": {"used_percentage": 1}}}).encode(),
                     json.dumps({"rate_limits": {"five_hour": None, "seven_day": []}}).encode()):
            self.tap(data, env=self.env(GRID_FLEET_NOW=str(NOW + 5)))
            with open(self.limits_file, "rb") as fh:
                self.assertEqual(fh.read(), before, data[:40])
            self.assertEqual(os.stat(self.limits_file).st_mtime_ns, mtime)

    def test_corrupt_previous_file_is_replaced_not_fatal(self):
        os.makedirs(self.state)
        with open(self.limits_file, "w") as fh:
            fh.write("{broken")
        r = self.tap(payload(five=(10.0, NOW + 100)), command=["true"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read()["five_hour"]["used_pct"], 10.0)

    def test_concurrent_taps_leave_valid_json_and_no_stray_files(self):
        # Scenario: Concurrent sessions (30 taps started together)
        data_a = payload(five=(10.0, NOW + 100), seven=(20.0, NOW + 5000))
        data_b = payload(five=(11.0, NOW + 100))
        files = {}
        for name, data in (("a", data_a), ("b", data_b)):
            files[name] = os.path.join(self.work, "payload-%s.json" % name)
            with open(files[name], "wb") as fh:
                fh.write(data)
        # Launch all 30 before waiting on any, so they genuinely overlap.
        procs = []
        for i in range(30):
            with open(files["a" if i % 2 else "b"], "rb") as stdin:
                procs.append(subprocess.Popen(
                    [sys.executable, BIN, "tap", "--", "true"], stdin=stdin,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), cwd=self.work))
        for p in procs:
            p.communicate()
            self.assertEqual(p.returncode, 0)
        body = self.read()  # raises if not complete valid JSON
        self.assertIn(body["five_hour"]["used_pct"], (10.0, 11.0))
        self.assertEqual(self.state_listing(), ["limits.json"])


class TestHostileNumbers(TapBase):
    """F2: a value too big for a float must read as "not a number", never raise.

    math.isfinite(10**400) raises OverflowError; unguarded, one hostile used_percentage
    dropped the whole capture (including a valid seven_day) and, from a hand-edited
    limits.json, crashed `collect` with a traceback.
    """

    HUGE = 10 ** 400

    def test_is_number_rejects_huge_ints(self):
        self.assertFalse(limits._is_number(self.HUGE))
        self.assertFalse(limits._is_number(-self.HUGE))
        self.assertTrue(limits._is_number(10 ** 15))

    def test_huge_value_in_one_window_does_not_lose_the_other(self):
        # json.dumps writes the big int as plain digits, which json.loads reads back as an int.
        data = payload(five=(self.HUGE, NOW + 100), seven=(18.0, NOW + 1000))
        self.tap(data)
        body = self.read()
        self.assertNotIn("five_hour", body)
        self.assertEqual(body["seven_day"], {"used_pct": 18.0, "resets_at": NOW + 1000})

    def test_huge_resets_at_is_rejected_too(self):
        self.tap(payload(five=(1.0, self.HUGE), seven=(18.0, NOW + 1000)))
        self.assertEqual(set(self.read()), {"schema", "captured_at", "seven_day"})

    def test_hand_edited_file_with_huge_ints_reads_as_none_or_drops_the_window(self):
        from grid_fleet import config as cfg
        config = cfg.Config(state_dir=self.state)
        os.makedirs(self.state)
        cases = [
            # captured_at unusable -> the whole file is unusable
            ({"schema": 1, "captured_at": self.HUGE, "five_hour": {"used_pct": 1, "resets_at": NOW + 9}}, None),
            # one window unusable -> only that window drops
            ({"schema": 1, "captured_at": NOW, "five_hour": {"used_pct": self.HUGE, "resets_at": NOW + 9},
              "seven_day": {"used_pct": 2, "resets_at": NOW + 9}},
             {"captured_at": NOW, "seven_day": {"used_pct": 2, "resets_at": NOW + 9}}),
            # the only window unusable -> no information -> None
            ({"schema": 1, "captured_at": NOW, "five_hour": {"used_pct": 1, "resets_at": -self.HUGE}}, None),
        ]
        for body, expected in cases:
            with open(self.limits_file, "w") as fh:
                json.dump(body, fh)
            self.assertEqual(limits.read_limits(config), expected, body)


class TestPrivacy(TapBase):
    def test_no_identifying_payload_field_reaches_the_file(self):
        self.tap(payload(five=(42.0, NOW + 100), seven=(18.0, NOW + 1000)))
        with open(self.limits_file) as fh:
            text = fh.read()
        for secret in (SYNTH_CWD, SYNTH_SESSION_NAME, "transcript-secret", "synthetic-model",
                       "Synthetic", "extra_in_window", "workspace", "model"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(self.read()), {"schema", "captured_at", "five_hour", "seven_day"})
        for win in ("five_hour", "seven_day"):
            self.assertEqual(set(self.read()[win]), {"used_pct", "resets_at"})

    def test_unknown_top_level_and_rate_limits_fields_are_dropped(self):
        data = json.dumps({
            "unknown_top": "leak-1",
            "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": NOW + 10, "plan": "leak-2"},
                            "monthly": {"used_percentage": 1, "resets_at": 1}, "who": "leak-3"},
        }).encode()
        self.tap(data)
        with open(self.limits_file) as fh:
            text = fh.read()
        for leak in ("leak-1", "leak-2", "leak-3", "monthly"):
            self.assertNotIn(leak, text)


class TestReader(TapBase):
    """limits.read_limits: what collect gets. (Snapshot-level scenarios: test_collect.py.)"""

    def config(self):
        from grid_fleet import config as cfg
        return cfg.load_config(self.env())

    def test_round_trip_from_the_tap(self):
        self.tap(payload(five=(42.0, NOW + 100), seven=(18.0, NOW + 1000)))
        self.assertEqual(limits.read_limits(self.config()), {
            "captured_at": NOW,
            "five_hour": {"used_pct": 42.0, "resets_at": NOW + 100},
            "seven_day": {"used_pct": 18.0, "resets_at": NOW + 1000},
        })

    def test_absent_file_is_none(self):
        self.assertIsNone(limits.read_limits(self.config()))

    def test_default_config_without_state_dir_uses_environment(self):
        # cli falls back to Config() when config.json is broken; state_dir is then "".
        from grid_fleet import config as cfg
        self.tap(payload(five=(1.0, NOW + 100)))
        old = os.environ.get("GRID_FLEET_STATE_DIR")
        os.environ["GRID_FLEET_STATE_DIR"] = self.state
        try:
            self.assertIsNotNone(limits.read_limits(cfg.Config()))
        finally:
            if old is None:
                del os.environ["GRID_FLEET_STATE_DIR"]
            else:
                os.environ["GRID_FLEET_STATE_DIR"] = old


class TestNoCredentialsNoNetwork(unittest.TestCase):
    """Scenario: Code audit. Static: parse limits.py with ast (the Python equivalent of grep)."""

    FORBIDDEN_MODULES = {
        "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "xmlrpc", "requests", "httpx",
        "aiohttp", "keyring", "getpass", "netrc", "asyncio", "webbrowser", "grid_fleet.push",
    }
    # Calls that would read a credential or open a connection, matched by called name.
    FORBIDDEN_CALLS = {"urlopen", "create_connection", "connect", "getpass", "get_password",
                       "gethostbyname", "getaddrinfo", "request", "Request", "system", "Popen", "check_output"}
    # Substrings no literal in the module may contain (docstrings excluded; the module
    # doc legitimately talks about privacy).
    FORBIDDEN_LITERALS = ("credential", "keychain", "oauth", "anthropic", "http", ".claude",
                          "settings.json", "token", "security")

    def setUp(self):
        with open(LIMITS_SRC) as fh:
            self.tree = ast.parse(fh.read())

    def _imports(self):
        names = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Import):
                names.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                names.add(base)
                names.update(base + "." + a.name for a in n.names)
        return names

    def test_no_network_or_credential_imports(self):
        for name in self._imports():
            top = name.split(".")[0]
            self.assertNotIn(top, self.FORBIDDEN_MODULES, name)
            self.assertNotIn(name, self.FORBIDDEN_MODULES, name)

    def test_no_network_or_credential_calls(self):
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                self.assertNotIn(name, self.FORBIDDEN_CALLS, "line %d" % n.lineno)

    def test_no_credential_paths_or_endpoints_in_literals(self):
        docstrings = set()
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                body = n.body
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
                low = n.value.lower()
                for bad in self.FORBIDDEN_LITERALS:
                    self.assertNotIn(bad, low, "literal %r at line %d" % (n.value, n.lineno))

    def test_audit_catches_a_violation(self):
        # Self-test of the audit so it cannot pass vacuously.
        tree = ast.parse("import socket\nimport urllib.request\nx = urllib.request.urlopen('u')")
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[0] for a in n.names)
        self.assertTrue(names & self.FORBIDDEN_MODULES)

    def test_tap_never_opens_a_socket_at_runtime(self):
        # Dynamic backstop (in-process): any socket connect during capture would raise.
        import socket
        from unittest import mock
        work = tempfile.mkdtemp(prefix="gf-limits-net-")
        self.addCleanup(shutil.rmtree, work, True)
        from grid_fleet import config as cfg
        config = cfg.Config(state_dir=os.path.join(work, "state"))
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")), \
                mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)}):
            self.assertTrue(limits.capture(config, payload(five=(1.0, NOW + 10))))
            self.assertIsNotNone(limits.read_limits(config))


if __name__ == "__main__":
    unittest.main()
