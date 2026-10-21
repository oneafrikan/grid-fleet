## Why

Running Claude Code on several machines gives no single view of what the fleet is doing: how many sessions are busy or blocked waiting on input, how many tokens each machine burns, which subagent types do the work, and how close the Max account is to its 5-hour and 7-day limits. Existing tools cover one slice each (see `design.md` prior art): ccusage and Claude-Code-Usage-Monitor read one machine's logs, herdr shows agent status over SSH but no tokens, Omarchy's agents widget is single-host. Nothing combines per-host status, tokens, subagent mix and account limits for a multi-machine fleet, and none works without a larger toolkit.

## What Changes

- New Python (stdlib-only, 3.9-compatible) CLI `grid-fleet` with four commands: `collect`, `push`, `view`, `install`.
- `collect` writes one snapshot JSON per host: session counts (busy/waiting), output and input/cache tokens, 7-day subagent-type leaderboard (main session included), and the latest Max-plan limits if captured.
- `push` sends the snapshot to a hub over plain `ssh` (hub identity = an ssh host alias). The hub needs only `sshd`.
- `view` runs on the hub: a curses, btop-style dashboard reading snapshot files directly, with a limits strip shown once per account. It also appends snapshots to a history file.
- `install` prints (and, on confirmation, applies) a launchd agent or systemd user timer, and an opt-in statusLine wrapper that captures `rate_limits` without replacing the user's existing statusLine.
- Tracer-bullet order: collect on one host, push to a hub, minimal view, then limits strip, then installers.

## Capabilities

### New

- `snapshot-collect`: per-host snapshot of sessions, tokens, subagent usage and limits; incremental, fail-soft, privacy-default.
- `push-transport`: ssh push to a hub inbox, atomic delivery, hub-side history append.
- `hub-view-tui`: terminal dashboard, staleness rules, per-account limits.
- `limits-capture`: statusLine tap that records `rate_limits` and chains to the user's existing command.
- `install-timers`: launchd / systemd user timers and the confirmed statusLine change.

### Modified

None. No specs exist yet in `openspec/specs/`.

## Impact

- New repo content: `bin/grid-fleet`, `grid_fleet/` package, `tests/` with synthetic fixtures, docs. Nothing outside this repo is modified by this change.
- Reads (on each host, read-only): the output of `claude agents --json`; `~/.claude/projects/**/*.jsonl` and `subagents/agent-*.meta.json` for `usage`, `timestamp` and `agentType` only.
- Writes (on each host): a snapshot, an offset cache and a limits file under the XDG state dir; a config file under the XDG config dir. `install` (on confirmation) writes a timer unit and edits `~/.claude/settings.json`.
- Writes (on the hub): `<host>.json` files and a history file under the XDG state dir.
- Depends on the Claude Code on-disk layout, which is undocumented and may change between versions. The collector fails soft and warns.
- Follow-up outside this repo: a docs pointer from the-grid to grid-fleet (task 9.2; separate change on its own branch in the-grid).

## Non-goals

- Agents other than Claude Code, and any plugin system.
- HTML or web output, a web server on the hub, or any listening port. A future renderer over the same snapshot JSON is possible but not part of v1.
- Per-session drill-down (no session list, no cwd or session name display).
- Currency cost. Tokens only.
- The undocumented OAuth usage endpoint (`/api/oauth/usage`) and any credential or Keychain read.
- Sending notifications or alerts.
- Per-user rollout (which hub, which hosts); that lives in the user's own private config repo.
- Windows support.
- A pip/brew package. v1 install is clone and symlink.
