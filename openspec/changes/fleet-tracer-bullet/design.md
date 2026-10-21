# Design: fleet-tracer-bullet

## Context

- Goal: one terminal view, on a hub of the user's choosing, of Claude Code activity across all their machines.
- Constraints: Python stdlib only; stock macOS Python 3.9.6 must run it; Linux too; public-bound repo with no personal data; usable without the-grid.
- Data sources (all local to each spoke, read-only):
  - `claude agents --json`: active sessions (verified on Claude Code 2.1.305).
  - `~/.claude/projects/<project>/<sessionId>.jsonl` and `.../<sessionId>/subagents/agent-<id>.{meta.json,jsonl}`: token usage and `agentType`. Undocumented layout.
  - statusLine stdin JSON: `rate_limits` (documented, Pro/Max only).

## Architecture

```
 spoke (each machine)                             hub (user's choice)
 +----------------------------+  ssh, one stream  +------------------------------+
 | timer -> collect           |  (no extra port)  | sshd                         |
 |   claude agents --json     | ----------------> | inbox/<host>.json  (atomic)  |
 |   transcript tail-sum      |                   |           |                  |
 |   limits.json (from tap)   |                   | grid-fleet view  (curses)    |
 | -> snapshot.json -> push   |                   |   reads inbox, appends       |
 +----------------------------+                   |   history/history-YYYY.jsonl |
        ^ statusLine tap                          +------------------------------+
        | (Claude Code -> tap -> original statusLine)
```

- Decided: push, not pull. Spokes are laptops that sleep and roam; a hub cannot reliably dial them. A spoke dials one fixed hub.
- Decided: transport is `ssh <alias> <posix sh script>` with the snapshot on stdin. No scp/sftp subsystem dependence, no rsync, no extra service. Hub identity is only an ssh host alias from the user's own ssh config, so jump hosts, tunnels and keys stay the user's business.
- Decided: a machine that is itself the hub sets `hub` to `"local"`; `push` then writes into the local inbox with the same atomic rename. No ssh to self.
- Decided: tool install is clone + symlink `bin/grid-fleet` onto PATH. Timers record the absolute `sys.executable` and absolute script path at install time.

## Code layout (planned)

```
bin/grid-fleet              entry; `#!/usr/bin/env python3`; calls grid_fleet.cli.main
grid_fleet/cli.py           argparse dispatch: collect | push | view | install (+ internal `tap`)
grid_fleet/config.py        load/validate config JSON; paths (XDG, env overrides)
grid_fleet/snapshot.py      snapshot build + schema constants
grid_fleet/sources_claude.py  agents --json, transcript scan, offset cache, version warning
grid_fleet/limits.py        tap (statusLine wrapper) + limits.json read
grid_fleet/push.py          ssh/local delivery
grid_fleet/hub.py           inbox read, staleness, history ingest
grid_fleet/view.py          curses UI
grid_fleet/install.py       launchd/systemd units, statusLine edit
tests/                      unittest; tests/helpers builds a synthetic ~/.claude tree
```

- Decided: `sources_claude.py` is the only module that knows Claude's layout. No plugin API; this is just where a second agent would go.
- Decided: tests use `unittest` (stdlib). Env overrides for tests: `GRID_FLEET_CONFIG`, `GRID_FLEET_STATE_DIR`, `GRID_FLEET_CLAUDE_DIR`, `GRID_FLEET_CLAUDE_BIN`, `GRID_FLEET_NOW` (epoch). No test reads the real `~/.claude` or runs real ssh.

## Snapshot (schema 1)

One JSON object per host per collection. A section that could not be collected is `null`, never zeros.

```json
{
  "schema": 1,
  "agent": "claude",
  "agent_version": "2.1.305",
  "host": "alpha",
  "account": "default",
  "collected_at": 1792580580,
  "interval_s": 600,
  "sessions": {"busy": 2, "waiting": 1, "idle": 0, "other": 0, "total": 3},
  "tokens": {
    "last_24h": {"output": 412000, "input": 91000, "cache_creation": 1200000, "cache_read": 55000000},
    "last_7d":  {"output": 3100000, "input": 640000, "cache_creation": 9000000, "cache_read": 420000000},
    "by_day": [
      {"day": "2026-10-15", "output": 380000, "input": 80000, "cache_creation": 1000000, "cache_read": 50000000}
    ]
  },
  "agents": {
    "last_7d": [
      {"type": "main", "invocations": 38, "output": 900000},
      {"type": "core-researcher", "invocations": 12, "output": 210000}
    ]
  },
  "limits": {
    "captured_at": 1792580400,
    "five_hour": {"used_pct": 42.0, "resets_at": 1792587600},
    "seven_day": {"used_pct": 18.0, "resets_at": 1793000000}
  },
  "warnings": ["claude 2.9.0 not in tested versions"]
}
```

- `schema` integer; the hub skips files with an unknown `schema`.
- `host`: config `host_name` or the short hostname; sanitised to `[A-Za-z0-9._-]`, max 64 chars, no leading dot. This string is used in a remote file name, so the charset is a security boundary.
- `account`: free-text label from config (default `"default"`). Hosts sharing one plan use the same label. It is not read from Claude.
- `sessions`: from `claude agents --json`, rows with `kind == "interactive"` and a live `pid` (`os.kill(pid, 0)`). `status` busy / waiting / idle; any other value counts in `other`. `null` if the command is absent, times out (10 s) or is not JSON.
- `tokens`: sum of `message.usage` fields over main and subagent transcripts, by event `timestamp`. `last_24h` and `last_7d` are rolling windows; `by_day` has 7 entries by UTC date. Headline is `output`; `input`, `cache_creation`, `cache_read` are the muted secondary line.
- `agents.last_7d`: `type` is `agentType` from `agent-<id>.meta.json`, or `main` for the session transcript itself. `invocations` = distinct transcripts (one subagent file, or one main session file) with at least one usage event in the window. `output` = output tokens in the window. Sorted by `invocations` desc.
- `limits`: copied from the local `limits.json` written by the tap; `null` when never captured. Each window is independently optional.
- `warnings`: short strings; no paths, no transcript text.
- Decided: when `privacy.include_session_details` is true, `sessions.items` is added: `[{"status","name","cwd"}]`. The v1 view does not render it. Default false; with default config no snapshot field contains a cwd, session name, project directory name or session id.
- Decided: invocations count transcripts with activity in the window, not spawn events, because spawn events are not reliably in the undocumented layout.

## Incremental token summing

- State file `offsets.json` in the spoke state dir:

```json
{"schema": 1, "files": {"<relpath under projects>": {"offset": 48211, "size": 48211, "agent": "core-researcher",
   "hours": {"2026-10-21T14": [412, 91, 1200, 55000]}}}}
```

- Per file: the byte offset consumed, the agent label, and per-UTC-hour sums `[output, input, cache_creation, cache_read]`. Keys are relative paths under the Claude projects dir; the cache never leaves the host.
- Each run: list `*.jsonl` whose mtime is within 8 days; for each, read from `offset` to EOF; parse only complete lines (stop at last `\n`); add `message.usage` into `hours`; advance `offset`.
- If a file is smaller than its stored offset, drop its entry and recount from 0 (truncated or rewritten). If a file disappears, its hour sums stay until older than 8 days, then the entry is pruned. This survives the 30-day log cleanup for the window we report.
- Window totals are the sum over files of `hours` inside the window. Decided: hourly buckets (not daily) so `last_24h` is exact; `by_day` is derived by summing hours per UTC date.
- Duplicate usage lines: assistant messages can be logged more than once (streaming chunks). Decided: consecutive lines with the same `message.id` collapse to the last one; verified or replaced by spike 2.3.
- The collector reads only `type`, `message.id`, `message.usage`, `timestamp` from each line, and `agentType` from meta files. It never logs or stores any other field.
- Fail soft: unreadable file, bad JSON line or missing usage skips that line/file and adds a count-only warning. Unknown layout (no projects dir, zero matching files while sessions are busy) yields `tokens: null` plus a warning. `claude --version` not in the tested list adds a version warning; collection still runs. Exit code is 0 unless the snapshot could not be written.

## Hub

Hub state dir: `$XDG_STATE_HOME/grid-fleet/` (default `~/.local/state/grid-fleet/`).

```
inbox/<host>.json              latest snapshot per host (atomic rename)
inbox/.<host>.json.tmp         in-flight upload; ignored by readers
history/history-YYYY.jsonl     appended snapshots (see below)
history/ingest.json            {"<host>": <last hour bucket ingested>}
history/.lock                  flock during ingest
```

- Upload script run by `push` on the hub (POSIX sh, one single-quoted `sh -c` argument so the user's login shell does not matter): `umask 077; d="${XDG_STATE_HOME:-$HOME/.local/state}/grid-fleet/inbox"; mkdir -p "$d" && cat > "$d/.H.json.tmp" && mv "$d/.H.json.tmp" "$d/H.json"` with `H` the sanitised host name.
- ssh options: `-o BatchMode=yes -o ConnectTimeout=10`. No prompts ever; failure exits non-zero, keeps the local snapshot, retries on the next timer tick.
- Readers ignore dotfiles, files over 256 KiB, invalid JSON and unknown `schema`; the footer says how many were skipped.

### Staleness

- Decided: staleness uses the inbox file mtime (hub clock, set by `mv`), not the spoke's `collected_at`. This removes clock skew between machines.
- A host is stale when `hub_now - mtime > 2 * interval_s` (interval from the snapshot). Stale hosts render grey `unknown` with their age; they are excluded from totals and the footer says `n of m hosts`.
- Decided: `push` re-collects instead of sending a snapshot older than `interval_s`, so a fresh mtime never carries old data.
- `view.expected_hosts` (optional) lists host names that must appear; one with no inbox file renders `unknown (never reported)`.
- Limits freshness: shown while `now < resets_at` of that window, annotated `as of <age>`; dim when age > 1 h; rendered `unknown` once `now >= resets_at` (the stored percentage no longer applies). Decided.
- Limits across hosts: per `account`, take the `limits` with the greatest `captured_at` among fresh-or-stale hosts; windows are chosen independently.

### History file

- Decided: JSONL, one record per host per hour, in `history-YYYY.jsonl` (year of `collected_at`, UTC). At about 9 MB per host-year this stays greppable without rotation tooling.
- Record = the snapshot with `sessions.items` and `warnings` removed, plus `"ingested_at"`.
- Ingest runs inside `view` on every inbox refresh, and as `grid-fleet view --ingest-only` (no TUI) for cron/timers, and from `push` in local-hub mode. Idempotent: a record is appended only if its hour bucket (`collected_at // 3600`) is greater than `ingest.json[host]`. `fcntl.flock` guards concurrent ingesters.
- The view does not read history in v1 (the 7-day series comes from the snapshot). History is a durable record for later renderers.

## Limits capture

- Source: statusLine stdin JSON `rate_limits.five_hour` / `seven_day` (`used_percentage`, `resets_at` epoch s). Present only for Pro/Max, only after the first API response in a session, each window independently. Account-wide, so any session can supply it.
- `tap` (internal subcommand, not one of the four user commands): `grid-fleet tap -- <original command...>`. It reads stdin once into memory, spawns the original command with the identical bytes on stdin, writes the original's stdout and exit code through unchanged, and, independent of the original's result, writes the slice to `limits.json` (mode 0600, tmp + rename) when `rate_limits` is present.

```json
{"schema": 1, "captured_at": 1792580400,
 "five_hour": {"used_pct": 42.0, "resets_at": 1792587600},
 "seven_day": {"used_pct": 18.0, "resets_at": 1793000000}}
```

- Merge rule: a window absent from the new payload keeps its previous stored value only while that value's `resets_at` is in the future; otherwise it is dropped.
- If the original command is absent, the tap prints nothing and exits 0.
- Decided: Python tap, slice-only. Rejected: a shell wrapper that stores the whole payload (faster, but the stored file would then contain cwd, session id and transcript path). Fallback if spike 7.1 fails the overhead bound: shell wrapper writing the full payload to a 0600 file that `collect` slices; trade privacy-on-disk for speed. That is a HUMAN call recorded then.
- `install` rewrites `statusLine.command` to `<python> <bin/grid-fleet> tap -- <original command>` only after printing the exact before/after and getting a yes (see install-timers spec). The original command string is saved in the config as `statusline_original` so uninstall restores it byte-for-byte.
- Orca: on the reference setup the existing statusLine is a script gated by env vars. The tap passes the environment through, so gating keeps working; spike 7.1 verifies.

## Config (`config.json`, schema 1)

Path: `$GRID_FLEET_CONFIG`, else `$XDG_CONFIG_HOME/grid-fleet/config.json`, else `~/.config/grid-fleet/config.json`. Unknown keys are ignored; unknown `schema` is an error.

```json
{
  "schema": 1,
  "host_name": "alpha",
  "account": "default",
  "interval_s": 600,
  "hub": "hub-alias",
  "privacy": {"include_session_details": false},
  "view": {"expected_hosts": ["alpha", "beta"], "refresh_s": 5, "ascii": false},
  "statusline_original": null
}
```

- `hub`: an ssh host alias, or `"local"`. Required for `push`; `collect` and `view` work without it.
- `interval_s`: one of 300, 600, 900 (default 600).
- `view.ascii`: force ASCII box and bar glyphs.
- All fields optional except `schema`. Defaults: `host_name` = sanitised short hostname, `account` = `"default"`.

## TUI layout (sketch, 80x24)

```
┌ grid-fleet ───────────────────────────────────────── 2026-10-21 14:03 ┐
│ LIMITS  account default                                  as of 2m ago │
│  5h  ████████░░░░░░░░░░░░  42%   resets 16:10  (in 2h07m)             │
│  7d  ███░░░░░░░░░░░░░░░░░  18%   resets Mon 09:00  (in 4d19h)         │
├ HOSTS ────────────────────────────────────────────────────────────────┤
│ host     busy wait   out 24h   out 7d    age                          │
│ alpha       2    1     412k      3.1M    3m                           │
│                       in+cache 24h 1.3M · 7d 9.6M  (muted)            │
│ beta     unknown                          stale 41m                   │
│ total       2    1     412k      3.1M    1 of 2 hosts fresh           │
├ OUTPUT TOKENS / DAY (7d) ────────┬ SUBAGENTS (7d, invocations) ───────┤
│ ▂ ▃ ▅ ▇ ▆ ▃ ▁                    │ main              ████████   38    │
│ 10-15 ........... 10-21          │ core-researcher   ███        12    │
│                                  │ grid-backend-dev  ██          7    │
├──────────────────────────────────┴────────────────────────────────────┤
│ q quit   r refresh                              2 files skipped       │
└───────────────────────────────────────────────────────────────────────┘
```

- Decided: minimum size 60x16; smaller shows a one-line "terminal too small" message. Redraw on SIGWINCH (`KEY_RESIZE`).
- Colours (16-colour palette, `NO_COLOR` honoured): limit bars green < 60 %, yellow 60-84 %, red >= 85 %; busy cyan; waiting yellow; stale/unknown grey (dim); in+cache lines dim.
- Glyphs: box drawing, `█▏▎▍▌▋▊▉` bars, `▁▂▃▄▅▆▇█` sparkline; ASCII fallback (`+-|#.`) when the locale is not UTF-8 or `view.ascii` is true. Braille is not used (font-dependent).
- Keys: `q` / Ctrl-C quit (terminal restored), `r` re-read inbox now. Auto-refresh every `refresh_s` (default 5) by comparing inbox mtimes.
- Token numbers use k / M / G with one decimal.
- Subagents panel: top 8 by invocations summed across fresh hosts; `main` included. Output tokens per type are a dim second column when width allows.
- No per-session list, no cwd, no names.

## Install flags

- `grid-fleet install [--hub ALIAS|local] [--interval 300|600|900] [--account LABEL]`: writes/updates config and prints the timer plan.
- `--apply`: perform the plan after an interactive y/N. No TTY means print and exit non-zero.
- `--statusline`: add the tap to `statusLine.command` (plan + confirm, same rules).
- `--uninstall`: remove timer files and restore the saved statusLine.
- Decided: the default is print-only, matching the-grid's "activation is printed, never run" habit, because a timer and a settings edit are both persistent changes.

## Prior art

- ccusage: per-machine log analysis (tokens, cost); no fleet, no live status.
- Claude-Code-Usage-Monitor: single-machine limit/usage monitor.
- herdr: federates agent status over SSH; no tokens or limits.
- Omarchy `omarchy.agents` widget: single-host; uses the undocumented OAuth usage endpoint.
- cotel and agents-observe: observability via hooks/telemetry; heavier infrastructure than a personal fleet needs.

## Rejected alternatives

- Hub pulls over ssh: needs every spoke reachable and awake; push tolerates sleeping laptops.
- Web server / HTTP receiver on the hub: opens a port and needs auth; ssh already exists.
- scp/rsync for delivery: depends on sftp subsystem or a non-stock binary; stdin over ssh is enough.
- Undocumented `GET /api/oauth/usage` for limits: needs the OAuth token (macOS Keychain read), 429-prone (anthropics/claude-code#30930), undocumented. The statusLine `rate_limits` field is documented and needs no credential. Not used in v1.
- Silently replacing the user's statusLine: breaks existing tooling. Tap chains instead and `install` confirms.
- Limits per host: the plan is per account; per-host rows would show the same number N times.
- Cost in currency: depends on a price table that changes; tokens are the unit.
- TOML config: `tomllib` is 3.11+; stock macOS has 3.9.
- Third-party TUI libraries (rich, textual): breaks "zero dependencies".
- SQLite history: stdlib has it, but JSONL is greppable and append-only.
- Plugin system for other agents: YAGNI; one source module per agent when needed.

## Risks

- Claude's transcript layout is undocumented. Mitigation: one source module, fail soft, version warning, fixtures pinned to the verified version.
- `claude agents --json` shape may differ (a `waiting` row has not been observed). Mitigation: unknown statuses count as `other`; spike 2.2.
- Timers under launchd/systemd run with a minimal environment. `ssh` may lack the agent socket or ProxyCommand helpers on PATH, and a passphrase-protected key fails under `BatchMode`. Mitigation: installer records the install-time PATH in the unit; push failures are logged, and the hub shows the host stale.
- Laptop sleep suppresses timer ticks; spike 8.1 documents what each OS does. Staleness (2 x interval) absorbs a single missed tick.
- Statusline taps run on every refresh in every session. Mitigation: overhead bound in spike 7.1; the tap never blocks on the network.
- `rate_limits` appear only after a session's first API response, so a freshly booted host reports `limits: null`. The hub takes limits from whichever host captured most recently.
- Account label is user-supplied; two hosts mislabelled show different accounts. Acceptable for v1.
- History growth is bounded by the hourly rule.

## Open questions

1. Hub that is not also a running view: nothing ingests history. Add `install --hub` that schedules `view --ingest-only`, or accept gaps? (Draft: accept gaps in v1.)
2. `privacy.include_session_details` has no v1 consumer. Keep the key (guards the privacy contract now) or cut until a drill-down exists?
3. Internal `tap` subcommand extends the four-command surface. Acceptable, or fold into `collect --tap`?
4. Is `CLAUDE_CONFIG_DIR` honoured by Claude Code for the projects dir? Unverified; collector honours it if set.
5. UTC vs local day for `by_day`: hosts in different timezones would disagree on local days. Draft: UTC.
6. Hourly history thinning: enough?
