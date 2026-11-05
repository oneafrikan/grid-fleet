# grid-fleet

A btop-style terminal dashboard for your Claude Code sessions across all your machines.

**Status: pre-alpha. Groups 1-8 are built and independently QA'd: the tracer bullet (collect, push, view), the subagent leaderboard and sparkline, the limits strip, and the timer and statusLine installers. The security review gate is GO. Still pending: real-machine verification of the timers across sleep and of `install --apply` and `install --statusline` (tasks 8.1, 8.5), and the public-readiness gate (9.3).**
The plan is in [`openspec/changes/fleet-tracer-bullet/`](openspec/changes/fleet-tracer-bullet/).

## What it is

Running Claude Code on several machines gives no single view of the fleet. grid-fleet shows, in one terminal:

- Sessions running on each machine, split busy / waiting.
- Token usage: output tokens as the headline, input and cache tokens as a muted second line.
- Which subagent types do the work over the last 7 days (the main session counts as an agent).
- Your Max-plan 5-hour and 7-day limits, shown once per account, with the age of the capture.

A host that has not reported recently shows as grey `unknown`, never as zero. It shows "now" only: no history is stored in v1.

Synthetic example (80x24, invented host names and numbers):

```
┌ grid-fleet ──────────────────────────────────────────────── 2026-10-21 11:06 ┐
│ LIMITS  account default                                                      │
│ 5h  ████████▍░░░░░░░░░░░ 42%      resets 13:13 (in 2h07m)  as of 5m ago      │
│ 7d  ███▋░░░░░░░░░░░░░░░░ 18%      resets Mon 06:06 (in 4d19h)  as of 5m ago  │
├ HOSTS ───────────────────────────────────────────────────────────────────────┤
│ host     busy    wait  out 24h   out 7d  age                                 │
│ alpha       2       1     412k     3.1M  3m                                  │
│   in+cache 24h 56.3M · 7d 429.6M                                             │
│ beta  unknown                            stale 41m                           │
│ gamma unknown (never reported)                                               │
│ total       2       1     412k     3.1M                                      │
├ OUTPUT TOKENS / DAY (7d) ────┬─ SUBAGENTS (7d, invocations) ─────────────────┤
│ ▅ ▅ ▆ █ ▇ ▄ ▃  max 700k      │  1 main             ███████████     38   900k │
│ 10-15 ............ 10-21     │  2 core-researcher  ███             12   210k │
│                              │  3 grid-backend-dev ██               7   150k │
├──────────────────────────────┴───────────────────────────────────────────────┤
│ q quit   r refresh                                        1 of 3 hosts fresh │
└──────────────────────────────────────────────────────────────────────────────┘
```

- `beta` is stale (its file is older than 2 x its interval) and is left out of the totals, the leaderboard and the sparkline. Its limits are still used: the limits strip shows the newest capture per window per account, whichever host made it, labelled `as of <age> ago`.
- `gamma` is listed in `view.expected_hosts` but has never reported.

## Install

Requirements:

- Python 3.9 or newer, standard library only. Stock macOS Python 3.9 works.
- macOS or Linux. Windows is not supported.
- Claude Code (the only supported agent in v1).
- `ssh` on each spoke, and `sshd` plus a POSIX shell on the hub. Nothing else is installed on the hub.

Clone and symlink (on every spoke, and on the hub for `view`):

```
git clone <repo-url> ~/grid-fleet
mkdir -p ~/.local/bin
ln -s ~/grid-fleet/bin/grid-fleet ~/.local/bin/grid-fleet
grid-fleet --version
```

- `~/.local/bin` must be on `PATH`.
- The symlink may point anywhere on `PATH`; the entry script resolves it back to the clone.

Try it without any setup (throwaway state dir, no ssh, removes its temp dir on exit):

```
scripts/try-local.sh [--no-view]
```

- Runs `collect` twice, pushes to the hub `local`, lists the inbox, then opens the view if a terminal is attached.
- Reads your real Claude directory read-only. Writes nothing outside its temp dir.

## Config

Path: `~/.config/grid-fleet/config.json` (`$XDG_CONFIG_HOME/grid-fleet/config.json` if set, or `$GRID_FLEET_CONFIG`). JSON, all keys optional except `schema`. Unknown keys are ignored; an unknown `schema` is an error.

```json
{
  "schema": 1,
  "host_name": "alpha",
  "account": "default",
  "hub": "fleet-hub",
  "interval_s": 600,
  "view": {"expected_hosts": ["alpha", "beta"], "refresh_s": 5, "ascii": false}
}
```

- `host_name`: how this machine appears. Default is the short hostname. Characters outside `A-Za-z0-9._-` become `_`; at most 64 characters.
- `account`: label for the Claude plan. Hosts sharing one plan use the same label. The label is yours; it is not read from Claude. The hub shortens labels to 32 ASCII characters; `install --account` refuses anything longer or non-ASCII.
- `hub`: an ssh host alias from the spoke's own `~/.ssh/config`, or `"local"` on the hub itself. Required for `push`; `collect` and `view` work without it.
- `interval_s`: one of 300, 600, 900 (default 600). The hub calls a host stale after twice this.
- `view.expected_hosts`: host names that must appear; one with no snapshot shows `unknown (never reported)`.
- `view.refresh_s`: seconds between inbox checks (default 5).
- `view.ascii`: force ASCII glyphs.
- `statusline_original`: written by `install --statusline`. Do not edit it by hand.
- `install --apply` writes `interval_s`, `hub` and `account` (see `grid-fleet install`). You can still edit the file by hand. Other keys are left as they are, and `--uninstall` leaves these three in place.

## Quick start

### Spoke (any machine that runs Claude Code)

1. Install (above).
2. `grid-fleet install --hub <hub-alias> --interval 600` prints the plan: the config keys it would write and the timer files it would create. It changes nothing.
3. Read the plan, then run the same command with `--apply` and answer `y`. It writes the config, then installs the timer.
4. Optional check by hand: `grid-fleet collect` writes a snapshot and prints a one-line summary; `grid-fleet push` sends it to the hub.
5. Optional: `grid-fleet install --statusline` to capture Max-plan limits.

- The alias must work without a prompt: `push` runs `ssh` with `BatchMode=yes` and `ConnectTimeout=10`. Use a key that needs no passphrase prompt (agent or unprotected key).
- A failed push exits non-zero, logs a one-line reason and keeps the local snapshot. The next tick retries.

### Hub (the machine you watch from)

1. Install (above). The hub needs `sshd` only; spokes write into `~/.local/state/grid-fleet/inbox/<host>.json` (`$XDG_STATE_HOME/grid-fleet/inbox/` if set).
2. Run `grid-fleet view`.
3. If the hub is also a spoke, set `"hub": "local"` in its config. `push` then writes the inbox directly: no ssh to itself.

- The ssh upload writes to `$XDG_STATE_HOME/grid-fleet/inbox` (default `~/.local/state/grid-fleet/inbox`) as seen by a non-interactive ssh shell. `view` also honours `GRID_FLEET_STATE_DIR`. Both must resolve to the same directory, so leave `GRID_FLEET_STATE_DIR` unset on the hub unless you know the ssh shell sees the same path.
- `view` is read-only, opens no port and contacts no other machine.

### Keys

- `q` or Ctrl-C: quit (terminal restored).
- `r`: re-read the inbox now.
- The view redraws on resize. Below 60x16 it shows "terminal too small". `NO_COLOR` is honoured. ASCII glyphs are used when the locale is not UTF-8 or `view.ascii` is true.

## `grid-fleet install`

```
grid-fleet install [--hub ALIAS] [--account LABEL] [--interval 300|600|900] [--apply] [--statusline] [--uninstall]
```

- Default: prints the plan (config changes, files, commands, interpreter, script, install-time `PATH`, log location) and changes nothing.
- `--hub ALIAS`: the hub's ssh host alias, or `local`. Written to the config as `hub`. Without it, the `hub` already in the config is used.
- `--account LABEL`: the account label (printable ASCII, at most 32 characters). Written as `account`. Without it, the config's `account` is used, else `default`.
- `--interval`: timer interval, written as `interval_s`. Without it, the config's `interval_s` is used, else 600. 120 and other values are rejected.
- `--hub` and `--account` are rejected (exit 2) with `--statusline` or `--uninstall`. `--interval` is rejected with `--statusline`.
- The plan has a `config:` block listing the before and after of `interval_s`, `hub` and `account`, and `all other config keys: unchanged`.
  - Without a usable hub the plan prints a warning and no `config:` block.
- `--apply`: needs a TTY and a `y` at the prompt. With no TTY it prints the plan, exits non-zero and changes nothing. A second run with the same inputs reports `nothing to change`.
  - With no hub known (neither `--hub` nor a stored `hub`) it exits 1 and changes nothing, before the prompt: a timer with no hub would fail every push.
  - Writes the config first, then installs the timer, so the first tick sees the right hub and interval.
  - The config write is atomic and keeps every other key. A new config file is created with `schema`, `host_name`, and the three keys (mode 0600); a config directory it has to create is mode 0700. `host_name` is added only when the file is created.
  - A config with invalid JSON, a non-object top level or an unknown `schema` is left untouched and install exits 1, plan mode included.
  - macOS: a user LaunchAgent, `~/Library/LaunchAgents/io.grid-fleet.timer.plist`.
  - Linux: a user systemd `grid-fleet.service` and `grid-fleet.timer` under `~/.config/systemd/user/`.
  - Each tick runs `collect` then `push`, with absolute interpreter and script paths and the `PATH` captured at install time.
  - No root. Linux headless boxes: install prints a `loginctl enable-linger` note and never runs it.
  - It only overwrites or removes files that carry its generated-file marker, and refuses to touch a symlink or a foreign file at its path.
- `--statusline`: wraps `statusLine.command` in `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) with the limits tap.
  - Prints the exact before and after of `statusLine.command` and what else is written.
  - Needs a TTY and a `y`. With no TTY it prints the diff, exits non-zero and changes nothing. Declining changes nothing.
  - Writes a byte-for-byte backup `settings.json.grid-fleet-backup-<UTC time>` first, then rewrites the file atomically. All other settings are kept.
  - Saves the original command in the grid-fleet config as `statusline_original`.
  - With no statusLine configured, it shows a tap with no original command and still asks.
  - It is the only code path that writes `settings.json`. No other command touches it.
- `--uninstall`: unloads and removes the timer files grid-fleet created and restores `statusLine.command` from `statusline_original`. TTY and `y` required. It leaves `interval_s`, `hub` and `account` in the config.
- `--statusline --uninstall`: restores only the statusLine.
- Exit codes: 0 ok or plan printed, 1 refused or failed, 2 bad usage (such as an invalid interval).

Limits capture:

- The tap passes its stdin to your original statusLine command unchanged and returns its stdout and exit code unchanged. Your environment is passed through.
- It records only `captured_at`, and per window `used_pct` and `resets_at`, to `limits.json` in the state dir (mode 0600). It never stores the rest of the payload.
- `rate_limits` appear only for Pro/Max, and only after a session's first API response. Until then the view shows `unknown` for that window. It does not show 0%.

## Environment overrides

- `GRID_FLEET_CONFIG`: config file path.
- `GRID_FLEET_STATE_DIR`: state dir (`snapshot.json`, `offsets.json`, `limits.json`, `inbox/`). Default `$XDG_STATE_HOME/grid-fleet` or `~/.local/state/grid-fleet`.
- `GRID_FLEET_CLAUDE_DIR`: Claude directory that holds `projects/`. Falls back to `CLAUDE_CONFIG_DIR`, then `~/.claude`.
- `GRID_FLEET_CLAUDE_BIN`: the `claude` executable. Default `claude` on `PATH`.
- `GRID_FLEET_NOW`: fixed epoch seconds for the clock (tests).
- `NO_COLOR`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` are honoured.

## Architecture

- Every machine ("spoke") runs `collect` then `push` on a 5, 10 or 15 minute timer.
- `collect` writes one snapshot JSON, summing token usage incrementally from local Claude logs.
- `push` sends it over plain `ssh` (stdin to a small POSIX sh script) to a hub, identified by an ssh host alias. The hub renames the upload into place only if it arrived complete.
- The hub needs only `sshd`: no server, no database, no open port.
- `view` runs on the hub, reads the inbox and draws the dashboard with curses.
- Limits come from the optional statusLine tap, which chains to your existing statusLine.

Details: [`design.md`](openspec/changes/fleet-tracer-bullet/design.md), [`tasks.md`](openspec/changes/fleet-tracer-bullet/tasks.md).

## Claude Code versions

- The collector depends on Claude Code's on-disk layout, which is undocumented.
- Tested version list: 2.1.305 (the version the layout and `claude agents --json` were verified on).
- Any other version still collects. The snapshot gets a warning `claude <version> not in tested versions`. The warning is in `snapshot.json` and the pushed snapshot; the view does not display it.
- If `claude` is not found (it is often missing from the PATH of a non-interactive shell), `sessions` is `unknown` and the snapshot carries a warning. Put `claude` on the `PATH` before running `install`, or set `GRID_FLEET_CLAUDE_BIN`.

## Privacy

What leaves each machine, in every snapshot pushed to the hub:

- Host name and account label (both set by you).
- Session counts: busy, waiting, idle, other, total.
- Token totals: output, input, cache-creation and cache-read, for 24 hours, 7 days and per UTC day.
- Subagent type names with invocation counts and output tokens, for the last 7 days. That includes the built-in type names AND any custom agent type names you have defined. A custom name can carry project or customer wording. grid-fleet does not filter these. If that matters to you, rename or avoid sensitive agent names.
- Limits: used percentage and reset time for the 5-hour and 7-day windows, and when they were captured.
- The Claude Code version, the collection time and interval, and short warning strings that contain no paths and no transcript text.

What never leaves the machine, in any snapshot, whatever the config:

- Working directories.
- Session names.
- Project directory names.
- Session ids.
- Transcript text.

- There is no opt-in in v1 to send more.
- Transcripts are read for `type`, `message.id`, `message.usage` and `timestamp` only; subagent meta files for `agentType` only. Nothing else is stored or logged.
- Local-only files: `offsets.json` is keyed by transcript paths under your Claude projects directory, so it does contain project directory names and session ids. It stays in the local state dir and is never sent.
- Nothing is sent anywhere except to the hub you configure.

## Security model

Documented, not enforced, in v1.

- Spokes push over ssh. The hub account runs an upload command that the spoke chooses.
  - Use a DEDICATED, UNPRIVILEGED account on the hub for this, not your normal login.
  - Use a separate ssh key per spoke, so one machine can be revoked without touching the rest.
- The hub trusts the file name, not the sender. Any spoke can write any host's row, including `beta`'s. Treat every spoke as trusted.
- The hub only reads its own inbox and opens no port. `view` is read-only.
- `install` writes user-level timers only. No root.
- `install --statusline` edits `statusLine.command` only after you confirm the exact diff. `settings.json` can hold secrets, and `install` leaves a byte-for-byte backup of it next to the original. Protect (or delete) those backups.
- The tap makes no network calls and reads no credentials, tokens or Keychain. It runs on every statusLine refresh and never blocks on anything remote.
- `push` relies on your own ssh configuration and agent. grid-fleet stores no keys, passwords or tokens.
- Standard library only, no third-party dependencies.

## Development

- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (stock 3.9, not a Homebrew `python3`); `python3 -m unittest discover -s tests` on Linux.
- `CLAUDE.md` holds the constraints for working on the code.

## Related

- [the-grid](https://github.com/oneafrikan/the-grid): a related agent toolkit. grid-fleet is independent of it and does not depend on it.

## Acknowledgements

The statusLine tap design (chain to the existing statusLine command, pass the environment and exit code through unchanged) was inspired by how [Orca](https://www.onorca.dev/)'s statusLine hook works.

## Licence

MIT. See `LICENSE`.
