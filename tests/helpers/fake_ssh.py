"""Fake `ssh` executable for push tests. Never touches the network or ~/.ssh.

Real ssh is never run by any test: the tests put this fake's directory FIRST on
PATH, so `subprocess.run(["ssh", ...])` in grid_fleet/push.py finds it. The fake
behaves like `ssh <options> <alias> <remote command>`:

  * it records its argv (one file per invocation),
  * it takes the LAST argument (the remote command) and runs it with `sh -c`,
    the way sshd hands a command to the hub's shell, against a temp HOME so the
    hub inbox (`$HOME/.local/state/grid-fleet/inbox`) lands in a temp dir,
  * it feeds the remote command the stdin it received.

Quick start
-----------
    from helpers.fake_ssh import FakeSsh

    class T(unittest.TestCase):
        def setUp(self):
            self.ssh = FakeSsh()                 # temp dir, removed by cleanup
            self.addCleanup(self.ssh.cleanup)
            patcher = mock.patch.dict(os.environ, self.ssh.env())   # PATH = fake first
            patcher.start(); self.addCleanup(patcher.stop)

        def test_x(self):
            ...run push...
            self.assertEqual(len(self.ssh.calls()), 1)
            argv = self.ssh.calls()[0]           # ['-o', 'BatchMode=yes', ..., 'hub-alias', "sh -c '...'"]
            self.ssh.read_inbox("alpha")         # bytes the hub received, or None

API
---
FakeSsh(root=None)
    .bin_dir                       directory holding the executable `ssh`
    .hub_home                      the temp HOME the remote command runs under
    .env(extra=None) -> dict       {"PATH": "<bin_dir>:<current PATH>"} (+ extra); use with
                                   mock.patch.dict(os.environ, ...) or a child's env
    .set_mode(mode, **opts)        see Modes; takes effect on the next invocation
    .calls() -> List[List[str]]    argv (WITHOUT the program name) of every invocation, in order
    .inbox_dir() -> str            $hub_home/.local/state/grid-fleet/inbox
    .read_inbox(host) -> Optional[bytes]    contents of inbox/<host>.json, None if absent
    .inbox_files() -> List[str]    sorted names in the inbox, dot-files included
    .cleanup()

Modes (set_mode)
----------------
    "ok"      (default) run the remote command with the full stdin; exit with its status.
    "fail"    write nothing, run nothing; stderr gets one synthetic line; exit `code`
              (set_mode("fail", code=255), default 255). Models an unreachable hub,
              refused auth, BatchMode refusing to prompt.
    "die"     die mid-stream: read only the first `after` bytes of stdin
              (set_mode("die", after=10)), run the remote command on JUST those bytes
              (its stdin then hits EOF, as sshd gives the remote command when the client
              connection drops), and exit 255. This is what a dropped connection does to
              the remote side: the remote command sees a clean, short stream.
    "kill_remote"
              like "die", but the REMOTE side is killed (SIGKILL) while it still has the
              partial stream, so the remote command never reaches its `mv`. Models sshd or
              the hub going away mid-transfer. Exit 255.
    "hang"    sleep far longer than any test timeout (for the push timeout path).

set_sequence(["fail", "ok"], code=255, after=10) picks a mode PER INVOCATION (the Nth ssh run
uses the Nth entry; the last entry repeats). `code` / `after` apply to every entry. Used by
the retry tests ("fails with 255, then succeeds"). set_mode() clears any sequence.

The remote command runs with HOME=hub_home and XDG_STATE_HOME unset, so it exercises the
`${XDG_STATE_HOME:-$HOME/.local/state}` fallback of the real upload script.
"""

import os
import shutil
import stat
import tempfile
from typing import List, Optional

_FAKE_SSH = """#!/bin/sh
D='%(data)s'
HUBHOME='%(hub_home)s'

# --- record argv: one file per call, one argument per line --------------------
n=$(ls "$D" | grep -c '^call-')
n=$((n + 1))
printf '%%s\\n' "$@" > "$D/call-$n.argv"

# --- the remote command is the last argument (what sshd would hand to a shell) --
for last in "$@"; do :; done

mode=$(cat "$D/mode" 2>/dev/null)
# set_sequence(): invocation N uses line N of $D/seq; past the end the last line repeats.
if [ -f "$D/seq" ]; then
  mode=$(sed -n "${n}p" "$D/seq")
  [ -n "$mode" ] || mode=$(tail -n 1 "$D/seq")
fi
case "$mode" in
  fail)
    echo "ssh: synthetic failure: could not reach hub-alias" >&2
    exit "$(cat "$D/code" 2>/dev/null || echo 255)" ;;
  hang)
    exec sleep 60 ;;
  die)
    # Keep only the first N bytes of stdin; the remote command then sees EOF.
    head -c "$(cat "$D/after")" > "$D/partial"
    env -u XDG_STATE_HOME HOME="$HUBHOME" sh -c "$last" < "$D/partial"
    exit 255 ;;
  kill_remote)
    head -c "$(cat "$D/after")" > "$D/partial"
    # Feed the partial stream through a pipe that stays OPEN (sleep holds it), so the
    # remote `cat` is still waiting for more input when it is killed: no EOF, no `mv`.
    ( cat "$D/partial"; sleep 3 ) 2>/dev/null | env -u XDG_STATE_HOME HOME="$HUBHOME" sh -c "$last" &
    rpid=$!
    sleep 1
    # Kill the whole remote pipeline (shell + cat) like a dropped sshd session would.
    pkill -KILL -P "$rpid" 2>/dev/null
    kill -KILL "$rpid" 2>/dev/null
    wait "$rpid" 2>/dev/null
    exit 255 ;;
  *)
    env -u XDG_STATE_HOME HOME="$HUBHOME" sh -c "$last"
    exit $? ;;
esac
"""


_MODES = ("ok", "fail", "die", "kill_remote", "hang")


class FakeSsh:
    def __init__(self, root: Optional[str] = None):
        self._owns_root = root is None
        self.root = root or tempfile.mkdtemp(prefix="gf-fakessh-")
        self.bin_dir = os.path.join(self.root, "bin")
        self.hub_home = os.path.join(self.root, "hubhome")
        self._data = os.path.join(self.root, "data")
        for d in (self.bin_dir, self.hub_home, self._data):
            os.makedirs(d)
        path = os.path.join(self.bin_dir, "ssh")
        with open(path, "w") as fh:
            fh.write(_FAKE_SSH % {"data": self._data, "hub_home": self.hub_home})
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.set_mode("ok")

    # -- environment -------------------------------------------------------
    def env(self, extra: Optional[dict] = None) -> dict:
        e = {"PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", "")}
        e.update(extra or {})
        return e

    # -- behaviour ---------------------------------------------------------
    def set_mode(self, mode: str, code: int = 255, after: int = 10) -> None:
        assert mode in _MODES, mode
        self._clear_sequence()
        self._write(mode, code, after)

    def set_sequence(self, modes: List[str], code: int = 255, after: int = 10) -> None:
        """Per-invocation modes: the Nth ssh run uses modes[N-1]; the last entry repeats."""
        assert modes and all(m in _MODES for m in modes), modes
        self._write(modes[-1], code, after)
        with open(os.path.join(self._data, "seq"), "w") as fh:
            fh.write("\n".join(modes) + "\n")

    def _clear_sequence(self) -> None:
        try:
            os.unlink(os.path.join(self._data, "seq"))
        except FileNotFoundError:
            pass

    def _write(self, mode: str, code: int, after: int) -> None:
        for name, value in (("mode", mode), ("code", code), ("after", after)):
            with open(os.path.join(self._data, name), "w") as fh:
                fh.write(str(value))

    # -- observation -------------------------------------------------------
    def calls(self) -> List[List[str]]:
        out = []
        names = sorted(
            (n for n in os.listdir(self._data) if n.startswith("call-")),
            key=lambda n: int(n[len("call-"):].split(".")[0]),
        )
        for name in names:
            with open(os.path.join(self._data, name)) as fh:
                out.append(fh.read().splitlines())
        return out

    def inbox_dir(self) -> str:
        return os.path.join(self.hub_home, ".local", "state", "grid-fleet", "inbox")

    def inbox_files(self) -> List[str]:
        try:
            return sorted(os.listdir(self.inbox_dir()))
        except FileNotFoundError:
            return []

    def read_inbox(self, host: str) -> Optional[bytes]:
        try:
            with open(os.path.join(self.inbox_dir(), host + ".json"), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def cleanup(self) -> None:
        if self._owns_root:
            shutil.rmtree(self.root, ignore_errors=True)
