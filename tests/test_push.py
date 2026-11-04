"""Tests for `push` (Group 3): one test group per scenario in specs/push-transport/spec.md.

Real ssh is never run. tests/helpers/fake_ssh.py puts an executable `ssh` first on PATH that
runs the received remote command locally against a temp HOME, so the "hub inbox" is a temp
dir. HOME is also pointed at a temp dir for every test, so nothing can reach a real ~/.ssh.
The synthetic Claude tree and GRID_FLEET_NOW keep collection off the real ~/.claude and the
real clock.

These are the author's own self-checks, not independent verification.
"""

import ast
import contextlib
import dataclasses
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

from helpers.claude_tree import ClaudeTree, agent_row, usage_event
from helpers.fake_ssh import FakeSsh
from helpers.synthetic_snapshot import COLLECTED_AT, make_snapshot

from grid_fleet import config as cfg
from grid_fleet import hub, push, snapshot

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin", "grid-fleet")

# Fixed clock shared with the other test modules' style: 2026-10-21T14:03:00Z.
NOW = 1792591380
INTERVAL = 600
ALIAS = "hub-alias"


class PushBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="gf-push-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.home = os.path.join(self.work, "home")  # a HOME with no .ssh in it
        os.makedirs(self.home)
        self.state = os.path.join(self.work, "state")
        self.tree = ClaudeTree()
        self.addCleanup(self.tree.cleanup)
        self.ssh = FakeSsh()
        self.addCleanup(self.ssh.cleanup)
        env = self.ssh.env({"GRID_FLEET_NOW": str(NOW), "HOME": self.home})
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Belt and braces: the ssh a push would run must be the fake, never a real one.
        self.assertEqual(shutil.which("ssh"), os.path.join(self.ssh.bin_dir, "ssh"))
        self.config = self.make_config({"hub": ALIAS})

    # -- helpers -----------------------------------------------------------
    def config_env(self, config_json):
        body = {"schema": 1, "host_name": "alpha", "interval_s": INTERVAL}
        body.update(config_json)
        cfg_path = os.path.join(self.work, "config-%d.json" % len(os.listdir(self.work)))
        with open(cfg_path, "w") as fh:
            json.dump(body, fh)
        return self.tree.env({"GRID_FLEET_STATE_DIR": self.state, "GRID_FLEET_CONFIG": cfg_path})

    def make_config(self, config_json=None):
        return cfg.load_config(self.config_env(config_json or {}))

    def snap_path(self):
        return snapshot.snapshot_path(self.config)

    def store_snapshot(self, age=60, indent=None, **overrides):
        """Write a stored snapshot.json `age` seconds old; return its exact bytes.

        `indent` pretty-prints it (inner newlines), only to give truncation tests
        somewhere to cut. Real snapshots are one line + a trailing newline.
        """
        snap = make_snapshot("alpha", collected_at=NOW - age, interval_s=INTERVAL, **overrides)
        os.makedirs(self.state, exist_ok=True)
        data = (json.dumps(snap, indent=indent) + "\n").encode("utf-8")
        with open(self.snap_path(), "wb") as fh:
            fh.write(data)
        return data

    def local_snapshot(self):
        with open(self.snap_path(), "rb") as fh:
            return fh.read()

    def run_push(self, config=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = push.push_main(config or self.config, None)
        return rc, out.getvalue(), err.getvalue()

    def seed_hub(self, host, content):
        """Pre-existing good <host>.json on the fake hub (the 'previous file')."""
        os.makedirs(self.ssh.inbox_dir(), exist_ok=True)
        with open(os.path.join(self.ssh.inbox_dir(), host + ".json"), "wb") as fh:
            fh.write(content)


class TestPushOverSsh(PushBase):
    def test_successful_push(self):
        data = self.store_snapshot()
        rc, out, err = self.run_push()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ssh.read_inbox("alpha"), data)
        self.assertEqual(self.ssh.inbox_files(), ["alpha.json"])  # no tmp left behind
        self.assertEqual(len(self.ssh.calls()), 1)
        # umask 077 in the upload script: the hub copy is owner-only.
        mode = os.stat(os.path.join(self.ssh.inbox_dir(), "alpha.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # A fresh stored snapshot is sent as is: no needless re-collect.
        self.assertEqual(self.tree.calls(), [])
        # Success output is at most one short line.
        self.assertLessEqual(len(out.strip().splitlines()), 1)

    def test_non_interactive_ssh(self):
        self.store_snapshot()
        self.run_push()
        (argv,) = self.ssh.calls()
        # Exact shape: options, "--", alias, ONE remote-command word. ClearAllForwardings
        # stops a user's ssh config from opening port/agent/X11 forwards for this one-shot
        # upload; "--" means the alias can never be parsed as an option.
        self.assertEqual(argv[:6], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "-o", "ClearAllForwardings=yes"])
        self.assertEqual(argv[6], "--")
        self.assertEqual(argv[7], ALIAS)
        self.assertEqual(len(argv), 9)
        argv = argv[:5] + [argv[8]]  # the checks below index the remote-command word as argv[5]
        self.assertTrue(argv[5].startswith("sh -c '"))
        self.assertTrue(argv[5].endswith("'"))
        self.assertNotIn("\n", argv[5])
        # The upload script from design.md, with H = the host name: a per-push unique tmp
        # file (PID in the name) removed on exit, completeness check (last byte must be a
        # newline), then the atomic rename.
        self.assertIn('t="$d/.alpha.json.$$.tmp"; c() { rm -f "$t"; }; trap c EXIT; '
                      'mkdir -p "$d" && cat > "$t" && '
                      '[ "$(tail -c 1 "$t" | wc -l)" -eq 1 ] && '
                      'mv "$t" "$d/alpha.json"', argv[5])
        self.assertIn("umask 077", argv[5])

    def test_hub_unreachable(self):
        data = self.store_snapshot()
        self.ssh.set_mode("fail", code=255)
        rc, out, err = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.local_snapshot(), data)  # local snapshot kept
        self.assertEqual(len(err.strip().splitlines()), 1, err)  # one-line reason
        self.assertIn("255", err)
        self.assertEqual(len(self.ssh.calls()), 1)  # no retry loop
        self.assertEqual(self.ssh.inbox_files(), [])
        self.assertEqual(out, "")

    def test_failure_log_line_is_reason_class_and_exit_code_only(self):
        self.store_snapshot()
        self.ssh.set_mode("fail", code=255)
        _, out, err = self.run_push()
        # Exactly this shape: nothing from ssh's own output follows the exit code.
        self.assertEqual(err, "grid-fleet push: push failed (ssh exit 255); local snapshot kept\n")
        self.assertEqual(out, "")

    def test_ssh_stderr_never_reaches_push_output(self):
        # The fake's stderr line names "hub-alias" (a synthetic host). It must not be echoed on
        # stdout or stderr: ssh output can carry host names, addresses and key paths, and this
        # line lands in timer logs.
        self.store_snapshot()
        self.ssh.set_mode("fail", code=255)
        rc, out, err = self.run_push()
        self.assertNotEqual(rc, 0)
        for text in (out, err):
            self.assertNotIn("hub-alias", text)
            self.assertNotIn("synthetic failure", text)
            self.assertNotIn("could not reach", text)
        # Same through the real CLI in a child process (what a timer would log).
        env = dict(os.environ)
        env.update(self.config_env({"hub": ALIAS}))
        r = subprocess.run([sys.executable, BIN, "push"], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertNotIn("hub-alias", combined)
        self.assertNotIn("synthetic failure", combined)

    def test_other_exit_codes_keep_a_short_one_line_log(self):
        self.store_snapshot()
        self.ssh.set_mode("fail", code=7)
        _, _, err = self.run_push()
        self.assertIn("ssh exit 7", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertLess(len(err), 200)

    def test_ssh_timeout_is_a_failure_not_a_hang(self):
        data = self.store_snapshot()
        self.ssh.set_mode("hang")
        # 3 s, not 1: under load the fake ssh script can need >1 s just to start and record its
        # argv, which made `calls()` empty and the test flaky.
        with mock.patch.object(push, "SSH_TIMEOUT_S", 3):
            rc, _, err = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertIn("timeout", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(len(self.ssh.calls()), 1)
        self.assertEqual(self.local_snapshot(), data)

    def test_ssh_binary_missing_is_a_failure(self):
        data = self.store_snapshot()
        empty = os.path.join(self.work, "empty-path")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            rc, _, err = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("cannot run ssh", err)
        self.assertEqual(self.local_snapshot(), data)

    def test_end_to_end_through_the_cli(self):
        # bin/grid-fleet push in a child process: cli wiring, exit code, env overrides.
        data = self.store_snapshot()
        env = dict(os.environ)
        env.update(self.config_env({"hub": ALIAS}))
        r = subprocess.run([sys.executable, BIN, "push"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ssh.read_inbox("alpha"), data)


class TestHubNotConfigured(PushBase):
    def assert_nothing_ran(self):
        self.assertEqual(self.ssh.calls(), [])  # ssh never invoked
        self.assertEqual(self.tree.calls(), [])  # claude never invoked: nothing collected
        self.assertFalse(os.path.exists(self.snap_path()))
        self.assertFalse(os.path.exists(hub.inbox_dir(self.state)))

    def test_missing_hub_names_the_key(self):
        config = self.make_config({})  # no `hub`
        self.assertIsNone(config.hub)
        rc, out, err = self.run_push(config)
        self.assertNotEqual(rc, 0)
        self.assertIn("hub", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(out, "")
        self.assert_nothing_ran()

    def test_hostile_hub_aliases_are_refused(self):
        # Option injection into ssh, and whitespace/control characters.
        for bad in ("-oProxyCommand=touch /nope", "-J", "", "a b", "a\tb", "a\nb", "a\x00b", " hub", "hub ", "hub\n",
                    # S6: outside the conservative charset [A-Za-z0-9._@:-]
                    "a;b", "a'b", 'a"b', "a$(id)", "a`id`", "a|b", "a&b", "a/b", "a\\b", "a%h",
                    "a=b", "a,b", "a*", "~", "h\u00e9", "a\x7fb", "-", "--", "-x"):
            config = dataclasses.replace(self.config, hub=bad)
            rc, _, err = self.run_push(config)
            self.assertNotEqual(rc, 0, repr(bad))
            self.assertIn("hub", err, repr(bad))
            self.assertEqual(len(err.strip().splitlines()), 1, repr(bad))
            self.assert_nothing_ran()

    def test_plain_aliases_are_accepted(self):
        for ok in ("hub-alias", "user@host.example", "h_1", "local", "a.b.example", "10.0.0.5",
                   "fe80::1", "User_1@host-2.example", "a-", "x"):
            self.assertIsNone(push.validate_hub(ok), ok)


class TestAtomicDelivery(PushBase):
    """Spec 'Atomic delivery' / 'Interrupted upload'."""

    OLD = b'{"schema":1,"previous":"good file"}\n'

    def test_hub_dying_mid_transfer_leaves_previous_file(self):
        # The remote command is killed while its `cat` is still reading: it never reaches `mv`.
        # The previous file is intact; partial bytes exist only in the dot-prefixed temp file.
        data = self.store_snapshot()
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("kill_remote", after=10)
        rc, _, err = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.ssh.read_inbox("alpha"), self.OLD)
        # SIGKILL skips the script's exit trap, so the partial upload stays behind, but only
        # in a unique dot-prefixed file readers ignore.
        tmps = [n for n in self.ssh.inbox_files() if n != "alpha.json"]
        self.assertEqual(len(tmps), 1, tmps)
        self.assertRegex(tmps[0], r"^\.alpha\.json\.[0-9]+\.tmp$")
        with open(os.path.join(self.ssh.inbox_dir(), tmps[0]), "rb") as fh:
            partial = fh.read()
        self.assertEqual(partial, data[:10])
        self.assertEqual(len(self.ssh.calls()), 1)

    # --- client drops mid-stream ------------------------------------------------------
    # When the CLIENT connection drops, sshd closes the remote command's stdin, so `cat` sees a
    # clean EOF after the partial bytes and exits 0. The upload script therefore renames only if
    # the tmp file ends in a newline (snapshots are JSON + "\n"). "die" mode in the fake ssh
    # models exactly this: the remote command gets the first `after` bytes, then EOF.

    def test_snapshot_bytes_pushed_end_in_newline(self):
        # The completeness check relies on this. Collect for real (no stored snapshot) and
        # assert on what is on disk, what the hub received and what push hands to ssh.
        self.tree.add_session("proj-a", "sess-1", [usage_event(NOW - 60, output=5)])
        self.assertTrue(push.fresh_snapshot_bytes(self.config).endswith(b"\n"))
        self.assertTrue(self.local_snapshot().endswith(b"\n"))
        self.assertEqual(self.run_push()[0], 0)
        self.assertTrue(self.ssh.read_inbox("alpha").endswith(b"\n"))

    def test_stored_snapshot_without_trailing_newline_is_recollected(self):
        # A fresh-looking stored file lacking the final newline would be refused by the hub on
        # every tick; push re-collects instead of sending it.
        data = self.store_snapshot()
        with open(self.snap_path(), "wb") as fh:
            fh.write(data.rstrip(b"\n"))
        sent = push.fresh_snapshot_bytes(self.config)
        self.assertTrue(sent.endswith(b"\n"))
        self.assertEqual(sent, self.local_snapshot())

    def test_complete_stream_still_lands(self):
        # Client dies only AFTER the whole stream was written: the hub got every byte, so the
        # file is accepted and complete (push still reports failure: ssh exit 255).
        data = self.store_snapshot()
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=len(data))
        self.run_push()
        self.assertEqual(self.ssh.read_inbox("alpha"), data)
        self.assertEqual(self.ssh.inbox_files(), ["alpha.json"])

    def test_client_dropping_mid_stream_leaves_previous_file(self):
        data = self.store_snapshot()
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=10)
        rc, _, _ = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.ssh.read_inbox("alpha"), self.OLD)
        # The script ran to its end, so its exit trap removed the partial tmp file.
        self.assertEqual(self.ssh.inbox_files(), ["alpha.json"])

    def test_zero_byte_stream_is_not_renamed(self):
        self.store_snapshot()
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=0)
        self.run_push()
        self.assertEqual(self.ssh.read_inbox("alpha"), self.OLD)
        self.assertEqual(self.ssh.inbox_files(), ["alpha.json"])  # empty tmp cleaned up

    def test_zero_byte_stream_with_no_previous_file_creates_no_final_file(self):
        self.store_snapshot()
        self.ssh.set_mode("die", after=0)
        self.run_push()
        self.assertIsNone(self.ssh.read_inbox("alpha"))

    def test_stream_truncated_exactly_before_the_final_newline_is_not_renamed(self):
        # Every byte except the closing "\n": a complete-looking JSON document, still refused.
        data = self.store_snapshot()
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=len(data) - 1)
        self.run_push()
        self.assertEqual(self.ssh.read_inbox("alpha"), self.OLD)

    def test_stream_truncated_before_an_inner_newline_is_not_renamed(self):
        data = self.store_snapshot(indent=1)
        cut = data.index(b"\n")  # cut just BEFORE the first newline: last byte is not "\n"
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=cut)
        self.run_push()
        self.assertEqual(self.ssh.read_inbox("alpha"), self.OLD)

    def test_stream_truncated_after_an_inner_newline_is_accepted_known_limit(self):
        # KNOWN RESIDUAL LIMIT, documented on purpose. The check is "the last byte is a
        # newline", so a stream cut exactly AFTER an inner newline looks complete to the hub.
        # Real snapshots are one line plus the closing "\n", so for them only the full stream
        # ends in a newline; the pretty-printed fixture here has inner newlines purely to show
        # the gap. The check catches truncation, not every short stream.
        data = self.store_snapshot(indent=1)
        cut = data.index(b"\n") + 1
        self.assertLess(cut, len(data))
        self.seed_hub("alpha", self.OLD)
        self.ssh.set_mode("die", after=cut)
        self.run_push()
        self.assertEqual(self.ssh.read_inbox("alpha"), data[:cut])  # truncated file accepted

    def test_partial_data_only_in_dot_prefixed_tmp_name(self):
        # Whatever happens, the in-flight name is a dot-file readers ignore.
        self.assertIn('.alpha.json.$$.tmp', push.remote_command("alpha"))
        self.assertEqual(os.path.basename(hub.inbox_tmp_path("/s", "alpha")), ".alpha.json.tmp")


class TestOverlappingPushes(PushBase):
    """S3: two pushes of one host overlap (manual + timer). Neither may corrupt the other.

    With a fixed tmp name both uploads shared one file: the second `cat >` truncated it and
    the first kept writing at its old offset, so the renamed result was a mix (or NUL bytes
    that still ended in a newline). The tmp name is now unique per push.
    """

    def start_remote(self, host="alpha"):
        """Start the real upload script (what sshd would run) against the fake hub's HOME."""
        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        env["HOME"] = self.ssh.hub_home
        return subprocess.Popen(["sh", "-c", push.remote_command(host)], stdin=subprocess.PIPE, env=env)

    def wait_for_tmp_bytes(self):
        """Block until some in-flight dot-file in the hub inbox holds bytes."""
        deadline = time.time() + 10
        while time.time() < deadline:
            for name in self.ssh.inbox_files():
                if name.startswith(".") and os.path.getsize(os.path.join(self.ssh.inbox_dir(), name)) > 0:
                    return
            time.sleep(0.02)
        self.fail("upload never started writing its tmp file")

    @staticmethod
    def doc(tag, size):
        # Valid single-line JSON + "\n" of a chosen size, distinguishable by tag.
        return (json.dumps({"schema": 1, "tag": tag, "pad": "x" * size}) + "\n").encode("utf-8")

    def test_overlapping_uploads_never_mix(self):
        a, b = self.doc("A", 4000), self.doc("B", 1500)
        first = self.start_remote()
        first.stdin.write(a[:1000])
        first.stdin.flush()
        self.wait_for_tmp_bytes()  # A is mid-stream with its tmp open
        second = self.start_remote()  # B runs start to finish while A is still open
        second.communicate(b)
        first.communicate(a[1000:])  # A finishes last
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        final = self.ssh.read_inbox("alpha")
        self.assertIn(final, (a, b))  # exactly one upload's bytes, never a mix, no NUL padding
        self.assertEqual(json.loads(final.decode("utf-8"))["schema"], 1)
        self.assertEqual(self.ssh.inbox_files(), ["alpha.json"])  # no tmp left behind

    def test_each_upload_uses_its_own_tmp_name(self):
        first, second = self.start_remote(), self.start_remote()
        first.stdin.write(b"1")
        first.stdin.flush()
        second.stdin.write(b"2")
        second.stdin.flush()
        deadline = time.time() + 10
        while time.time() < deadline and len([n for n in self.ssh.inbox_files() if n.startswith(".")]) < 2:
            time.sleep(0.02)
        tmps = [n for n in self.ssh.inbox_files() if n.startswith(".")]
        first.communicate()
        second.communicate()
        self.assertEqual(len(tmps), 2, tmps)  # two in-flight uploads, two distinct dot-files
        for name in tmps:
            self.assertRegex(name, r"^\.alpha\.json\.[^/]+\.tmp$")

    def test_failed_upload_removes_its_tmp_file(self):
        # Incomplete stream (no trailing newline) fails the completeness check: nothing renamed
        # and the tmp is cleaned up rather than left behind.
        proc = self.start_remote()
        proc.communicate(b'{"schema":1')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.ssh.inbox_files(), [])

    def test_local_overlapping_deliveries_never_mix(self):
        config = dataclasses.replace(self.config, hub="local")
        a, b = self.doc("A", 4000), self.doc("B", 1500)
        real_replace = os.replace
        nested = []

        def replace_after_other_delivery(src, dst):
            # A is fully written to ITS tmp; a second delivery runs completely before A renames.
            if not nested:
                nested.append(True)
                push.deliver_local(config, b)
            real_replace(src, dst)

        with mock.patch("grid_fleet.push.os.replace", replace_after_other_delivery):
            push.deliver_local(config, a)
        with open(hub.inbox_path(self.state, "alpha"), "rb") as fh:
            self.assertEqual(fh.read(), a)  # the later rename wins whole
        self.assertEqual(os.listdir(hub.inbox_dir(self.state)), ["alpha.json"])

    def test_local_tmp_is_unique_owner_only_dotfile(self):
        config = dataclasses.replace(self.config, hub="local")
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((src, os.stat(src).st_mode & 0o777))
            real_replace(src, dst)

        with mock.patch("grid_fleet.push.os.replace", spy):
            push.deliver_local(config, self.doc("A", 10))
            push.deliver_local(config, self.doc("B", 10))
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0][0], seen[1][0])  # a new tmp name every delivery
        for path, mode in seen:
            self.assertTrue(os.path.basename(path).startswith(".alpha.json."), path)  # readers ignore it
            self.assertEqual(mode, 0o600)

    def test_local_write_failure_removes_tmp(self):
        config = dataclasses.replace(self.config, hub="local")
        os.makedirs(hub.inbox_dir(self.state))
        with mock.patch("grid_fleet.push.os.fdopen", side_effect=OSError(28, "synthetic")):
            with self.assertRaises(OSError):
                push.deliver_local(config, b"x\n")
        self.assertEqual(os.listdir(hub.inbox_dir(self.state)), [])


class TestLocalHub(PushBase):
    def test_hub_is_this_machine(self):
        data = self.store_snapshot()
        config = dataclasses.replace(self.config, hub="local")
        rc, out, err = self.run_push(config)
        self.assertEqual((rc, err), (0, ""))
        path = hub.inbox_path(self.state, "alpha")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(self.ssh.calls(), [])  # no ssh process started
        self.assertEqual(sorted(os.listdir(hub.inbox_dir(self.state))), ["alpha.json"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(hub.inbox_dir(self.state)).st_mode & 0o777, 0o700)

    def test_local_write_is_atomic(self):
        # If the final rename fails, the previous file is untouched and no tmp is left over.
        self.store_snapshot()
        config = dataclasses.replace(self.config, hub="local")
        os.makedirs(hub.inbox_dir(self.state))
        old = b'{"schema":1,"previous":"good file"}\n'
        with open(hub.inbox_path(self.state, "alpha"), "wb") as fh:
            fh.write(old)
        with mock.patch("grid_fleet.push.os.replace", side_effect=OSError(5, "synthetic")):
            rc, _, err = self.run_push(config)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(err.strip().splitlines()), 1)
        with open(hub.inbox_path(self.state, "alpha"), "rb") as fh:
            self.assertEqual(fh.read(), old)
        self.assertEqual(os.listdir(hub.inbox_dir(self.state)), ["alpha.json"])

    def test_local_replaces_previous_file(self):
        data = self.store_snapshot()
        config = dataclasses.replace(self.config, hub="local")
        os.makedirs(hub.inbox_dir(self.state))
        with open(hub.inbox_path(self.state, "alpha"), "wb") as fh:
            fh.write(b"old")
        self.assertEqual(self.run_push(config)[0], 0)
        with open(hub.inbox_path(self.state, "alpha"), "rb") as fh:
            self.assertEqual(fh.read(), data)


class TestFreshDataOnly(PushBase):
    def seed_claude(self):
        self.tree.add_session("proj-a", "sess-1", [usage_event(NOW - 60, output=123)])
        self.tree.set_agents([agent_row(status="busy")])
        for root, _d, files in os.walk(self.tree.projects_dir):
            for f in files:
                self.tree.set_mtime(os.path.join(root, f), NOW)

    def test_old_local_snapshot_is_recollected(self):
        self.seed_claude()
        self.store_snapshot(age=INTERVAL + 1, warnings=["stale-marker"])
        rc, _, err = self.run_push()
        self.assertEqual((rc, err), (0, ""))
        delivered = self.ssh.read_inbox("alpha")
        snap = json.loads(delivered.decode("utf-8"))
        self.assertEqual(snap["collected_at"], NOW)  # new, not NOW - 601
        self.assertNotIn("stale-marker", snap["warnings"])
        self.assertEqual(snap["tokens"]["last_24h"]["output"], 123)
        self.assertEqual(snap["sessions"]["busy"], 1)
        self.assertEqual(delivered, self.local_snapshot())  # hub copy == refreshed local file
        self.assertNotEqual(self.tree.calls(), [])  # the collector really ran

    def test_snapshot_exactly_interval_old_is_still_fresh(self):
        # Spec: "older than interval_s" is stale; equal is not.
        data = self.store_snapshot(age=INTERVAL)
        self.assertEqual(self.run_push()[0], 0)
        self.assertEqual(self.ssh.read_inbox("alpha"), data)
        self.assertEqual(self.tree.calls(), [])

    def test_missing_corrupt_or_future_snapshot_is_recollected(self):
        self.seed_claude()
        # missing
        self.assertEqual(self.run_push()[0], 0)
        self.assertEqual(json.loads(self.ssh.read_inbox("alpha").decode())["collected_at"], NOW)
        # corrupt
        with open(self.snap_path(), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.run_push()[0], 0)
        self.assertEqual(json.loads(self.local_snapshot().decode())["collected_at"], NOW)
        # dated in the future (clock jump): not trusted either
        self.store_snapshot(age=-3600, warnings=["future-marker"])
        self.assertEqual(self.run_push()[0], 0)
        self.assertNotIn(b"future-marker", self.ssh.read_inbox("alpha"))

    def test_recollect_failure_is_reported_and_nothing_is_sent(self):
        # State dir cannot be written -> cannot produce a fresh snapshot -> send nothing.
        os.makedirs(self.state)
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        if os.access(self.state, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        rc, _, err = self.run_push()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.ssh.calls(), [])


class TestHostNameSafety(PushBase):
    HOSTILE = "../x; rm -rf ~"
    SAFE = "_x__rm_-rf__"  # sanitiser: bad chars -> "_", leading dots stripped

    def test_config_sanitises_hostile_host_name(self):
        config = self.make_config({"hub": ALIAS, "host_name": self.HOSTILE})
        self.assertEqual(config.host_name, self.SAFE)

    def test_hostile_name_over_ssh_writes_only_inside_inbox(self):
        # Build the Config by hand with the RAW name: push must sanitise on its own,
        # not trust that config loading already did.
        config = dataclasses.replace(self.config, host_name=self.HOSTILE)
        self.store_snapshot()
        rc, _, err = self.run_push(config)
        self.assertEqual((rc, err), (0, ""))
        (argv,) = self.ssh.calls()
        self.assertNotIn("rm -rf", argv[-1])
        self.assertNotIn("..", argv[-1])
        self.assertNotIn("~", argv[-1])
        self.assertEqual(self.ssh.inbox_files(), [self.SAFE + ".json"])
        # Nothing else was created anywhere under the hub's HOME (e.g. no <inbox>/../x.json).
        written = []
        for root, _d, files in os.walk(self.ssh.hub_home):
            written += [os.path.join(root, f) for f in files]
        self.assertEqual(written, [os.path.join(self.ssh.inbox_dir(), self.SAFE + ".json")])
        self.assertFalse(os.path.exists(os.path.join(self.work, "x")))

    def test_hostile_name_local_writes_only_inside_inbox(self):
        config = dataclasses.replace(self.config, host_name=self.HOSTILE, hub="local")
        self.store_snapshot()
        self.assertEqual(self.run_push(config)[0], 0)
        self.assertEqual(os.listdir(hub.inbox_dir(self.state)), [self.SAFE + ".json"])
        # state dir holds only the snapshot and the inbox: nothing escaped upward.
        self.assertEqual(sorted(os.listdir(self.state)), ["inbox", "snapshot.json"])
        self.assertFalse(os.path.exists(os.path.join(self.work, "x.json")))

    def test_remote_command_contains_only_safe_host_characters(self):
        for raw in (self.HOSTILE, "a'b", 'a"b', "$(id)", "a b", "`id`", "a\nb", "", "...", "ok-host_1.example"):
            cmd = push.remote_command(raw)
            # Exactly the two host slots (tmp name, mv target), each filled with the same
            # safe token.
            host = cfg.sanitize_host_name(raw)
            self.assertRegex(host, r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
            self.assertEqual(cmd.count(host + ".json"), 2, (raw, cmd))
            self.assertEqual(cmd.count("'"), 2)  # only the wrapping quotes


class TestNoCredentials(PushBase):
    MARKERS = re.compile(
        r"BEGIN [A-Z ]*(PRIVATE|OPENSSH)|PRIVATE KEY|ssh-(rsa|ed25519)|password|passphrase|\btoken\b",
        re.IGNORECASE,
    )

    def test_no_credential_material_in_any_file_after_use(self):
        # Normal use: collect, push over (fake) ssh, push to a local hub.
        self.tree.add_session("proj-a", "sess-1", [usage_event(NOW - 60, output=5)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.collect_main(self.config, None), 0)
        self.assertEqual(self.run_push()[0], 0)
        self.assertEqual(self.run_push(dataclasses.replace(self.config, hub="local"))[0], 0)
        checked = 0
        for root_dir in (self.work,):
            for root, _d, files in os.walk(root_dir):
                if root.startswith(self.tree.root) or root.startswith(self.ssh.bin_dir):
                    continue  # test fixtures (fake claude/ssh scripts), not tool output
                for f in files:
                    path = os.path.join(root, f)
                    with open(path, "rb") as fh:
                        text = fh.read().decode("utf-8", "replace")
                    self.assertIsNone(self.MARKERS.search(text), path)
                    checked += 1
        self.assertGreaterEqual(checked, 4)  # config, snapshot, local inbox, hub inbox

    def test_push_never_opens_anything_under_dot_ssh(self):
        self.store_snapshot()
        opened = []
        real_open, real_os_open = open, os.open

        def spy_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        def spy_os_open(path, *a, **kw):
            opened.append(str(path))
            return real_os_open(path, *a, **kw)

        with mock.patch("builtins.open", spy_open), mock.patch("os.open", spy_os_open):
            self.assertEqual(self.run_push()[0], 0)
            self.assertEqual(self.run_push(dataclasses.replace(self.config, hub="local"))[0], 0)
        self.assertTrue(opened)
        self.assertEqual([p for p in opened if ".ssh" in p], [])

    def test_module_source_never_references_ssh_key_material(self):
        with open(push.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        # Docstrings may mention ~/.ssh to say we do not read it; code strings may not.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        for lit in literals:
            for needle in (".ssh", "id_rsa", "id_ed25519", "known_hosts", "identity", "SSH_AUTH_SOCK", "askpass"):
                self.assertNotIn(needle.lower(), lit.lower(), lit)
        # And it starts exactly one kind of subprocess: the single subprocess.run (ssh).
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name) and n.value.id == "subprocess" and n.attr == "run"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(1 for lit in literals if lit == "ssh"), 2)  # argv[0], success message


if __name__ == "__main__":
    unittest.main()
