# Tasks

Default verify command: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system Python 3.9.6, not the Homebrew `python3`); `python3 -m unittest discover -s tests` on Linux. Python is 3.9, stdlib only. Tests use temp dirs and the `GRID_FLEET_*` env overrides from `design.md`; none may read the real `~/.claude`, run real ssh, or touch real settings. No machine names, IPs, accounts or home paths in any tracked file, fixtures included. Comment code generously. Do not re-decide anything marked `Decided:` in `design.md`.

Tracer-bullet order: 1-2 collect on one machine, 3 push to a hub, 4 minimal view, 5 checkpoint, then 6 leaderboard and sparkline, 7 limits strip, 8 installers, 9 docs and handover.

## 1. Skeleton and CLI

Files: `bin/grid-fleet`, `grid_fleet/__init__.py`, `grid_fleet/cli.py`, `grid_fleet/config.py`, `tests/helpers/claude_tree.py`, `tests/test_config.py`, `tests/test_cli.py`.

- [x] 1.1 `bin/grid-fleet` (`#!/usr/bin/env python3`, executable) calling `grid_fleet.cli.main`; argparse subcommands `collect`, `push`, `view`, `install` and internal `tap`; `--version`.
- [x] 1.2 `config.py`: load JSON per `design.md`, defaults, host-name sanitiser, XDG paths, `GRID_FLEET_*` overrides, error on unknown `schema`, ignore unknown keys.
- [x] 1.3 `tests/helpers/claude_tree.py`: builds a synthetic Claude dir (projects, main and subagent transcripts, meta files) and a fake `claude` executable emitting canned `agents --json`. Synthetic names only.
- [x] 1.4 Static guard test: no file under `grid_fleet/` uses `match` statements, `X | Y` annotations evaluated at runtime, or imports outside the stdlib (parse with `ast`).

Acceptance: config defaults, bad schema, host-name sanitising (`../x; rm -rf ~` becomes a safe name) tested; guard test passes under 3.9.
Verify: default verify command.

## 2. Collect: sessions and tokens (tracer bullet, step 1)

Files: `grid_fleet/snapshot.py`, `grid_fleet/sources_claude.py`, `tests/test_collect.py`, `docs/spikes.md`.

- [x] 2.1 SPIKE (do first; throwaway script in the scratchpad, results recorded in `docs/spikes.md` as numbers only). Measure a naive full scan of `~/.claude/projects` for files modified in the last 8 days on the reference machine: file count, total MB, cold seconds, and a second pass with the OS cache warm. Never print transcript content. Also check, in one line, whether `CLAUDE_CONFIG_DIR` relocates the projects dir (set it to a temp dir, start a throwaway session, see where the transcript lands) and record yes/no (the collector honours it when set either way; this records whether it works). Pass: the figures exist and justify the offset-cache design; the warm-run budget in the spec (2 s) is confirmed or revised in the spec delta. Fail: if even a hot-cache incremental design cannot get under 5 s, stop and bring the figures to the human. (done, see docs/spikes.md: measurements recorded; `CLAUDE_CONFIG_DIR` sub-check done, yes, it relocates the projects dir)
- [ ] 2.2 SPIKE: capture a real `waiting` row. HUMAN triggers a permission prompt in a throwaway interactive session; run `claude agents --json` read-only and record the row's keys (not cwd/name values) in `docs/spikes.md`. Pass: a row with `status` `waiting` observed and a synthetic fixture added that matches its shape. Fail: record the actual status string seen for a blocked session and change the spec's status mapping before continuing. Also record the wall time of `claude agents --json` (pass: under 5 s). (attempted 2026-11-05: the Claude Code session auto-allowed the file write, so no permission prompt appeared and no `waiting` row was captured; still open, lowest value; needs a trigger that always prompts, for example a different permission mode)
- [x] 2.3 SPIKE: duplicate usage lines. On three large sessions compute output-token sums (numbers only) under three rules: every line, consecutive-same-`message.id` collapse, and last-per-`message.id` globally. Pass: rule 2 equals rule 3 on all three; keep rule 2. Fail: switch design to per-file id sets and amend `design.md`.
- [x] 2.4 `sources_claude.py`: sessions from `claude agents --json` (interactive, live pid, 10 s timeout, fail soft), version warning, tested-versions list seeded with the verified version.
- [x] 2.5 Incremental transcript scanner with hourly buckets and `offsets.json` per `design.md`; main and subagent transcripts; truncation and disappearance rules; reads only `type`, `message.id`, `message.usage`, `timestamp`, and `agentType` from meta.
- [x] 2.6 `snapshot.py` + `collect` command: writes `snapshot.json` (atomic) to the state dir and prints a one-line summary; `agents` and `limits` sections are `null` until groups 6 and 7.
- [x] 2.7 Tests for every scenario in `specs/snapshot-collect/spec.md` except the leaderboard and limits ones, including: warm run reads only appended bytes (assert via a counting file wrapper), partial last line, truncation, lost cache, corrupt line, privacy (with any config, no synthetic cwd, session name, project directory name or session id appears in the snapshot), unavailable `claude`.
- [x] 2.8 Reference-machine check (HUMAN runs, agent records): cold and warm `grid-fleet collect` times on the real corpus. Pass: warm under 2 s (or the revised figure from 2.1). Record in `docs/spikes.md`. (done: cold 1.20 s, warm 0.16 s, pass; see docs/spikes.md)

Acceptance: `grid-fleet collect` on a synthetic tree emits a snapshot matching `design.md`'s schema; all listed scenarios pass.
Verify: default verify command.

## 3. Push to a hub

Files: `grid_fleet/push.py`, `grid_fleet/hub.py` (inbox path helpers only), `tests/test_push.py`, `tests/helpers/fake_ssh.py`.

- [x] 3.1 `push.py`: ssh delivery with the upload script from `design.md`, `BatchMode`/`ConnectTimeout`, local-hub mode, re-collect when the snapshot is older than `interval_s`, one-line failure log.
- [x] 3.2 Fake `ssh` executable for tests (runs the received script locally against a temp HOME); asserts the argv and options.
- [x] 3.3 Tests for every scenario in `specs/push-transport/spec.md`: success, unreachable, missing `hub`, interrupted upload (kill mid-stream leaves old file intact), local hub spawns no ssh, hostile host name.
- [x] 3.4 HUMAN: run `collect` then `push` from one real machine to a real hub alias; confirm `<host>.json` appears in the hub inbox. Record the result (no hostnames) in `docs/spikes.md`. (done 2026-10-21, see docs/spikes.md)

Acceptance: push delivers the snapshot through the fake ssh into a temp inbox atomically; failure paths exit non-zero and keep the local snapshot.
Verify: default verify command.

## 4. Minimal view (tracer bullet, step 3)

Files: `grid_fleet/hub.py` (inbox reader, staleness), `grid_fleet/view.py`, `tests/test_hub.py`, `tests/test_view_render.py`.

- [x] 4.1 `hub.py`: read inbox with the skip rules, compute staleness from file mtime, per-host and total aggregation excluding stale hosts, `expected_hosts`.
- [x] 4.2 `view.py`: render function separated from curses I/O (takes state + width/height, returns lines with attribute tags) so it is testable without a terminal. Hosts table (busy, waiting, output 24h/7d, muted in+cache line, age), total row, footer, `unknown` rendering, token formatter, ASCII fallback, `NO_COLOR`.
- [x] 4.3 Curses loop: refresh by mtime poll, `q`/`r`, resize, too-small message, terminal restored on exit and on exception.
- [x] 4.4 Tests on the render function for staleness, never-reported, null sections, totals excluding stale hosts, ASCII mode, narrow width; test that `view` opens no sockets and leaves inbox files untouched.
- [x] 4.5 HUMAN: run `grid-fleet view` on the hub against the real inbox; confirm the layout is readable at 80x24 and 120x40. Record screenshots-as-text in `docs/spikes.md`. (done 2026-11-05: the maintainer reviewed the hub view in a real terminal at 6 of 6 hosts with limits strip, sparkline and leaderboard, and found the colours and resize behaviour fine as is)

Acceptance: view renders hosts from a temp inbox; a 41-minute-old file shows grey `unknown` and is not in the total.
Verify: default verify command.

## 5. HUMAN: tracer-bullet checkpoint

- [x] 5.1 Two real machines: both collect and push to the hub, the hub view shows both with correct busy counts for a known number of open sessions, and a deliberately paused spoke turns grey `unknown` after 2 x interval. Pass: all three observed. Fail: file the gap as a new task before continuing. (maintainer sign-off)
- [x] 5.2 Decide whether to proceed to groups 6-9 or adjust scope. (maintainer sign-off: proceed)

## 6. Subagent leaderboard and sparkline

Files: `grid_fleet/sources_claude.py`, `grid_fleet/view.py`, `tests/test_collect.py`, `tests/test_view_render.py`.

- [x] 6.1 Collect `agents.last_7d` (types from meta files, `main`, `unknown`), sorted by invocations; fill `tokens.by_day`.
- [x] 6.2 View: sparkline panel and top-8 leaderboard summed across fresh hosts.
- [x] 6.3 Tests for the leaderboard scenarios in the specs (cross-host sum, main counted, sort order, missing meta file) and the `by_day` UTC-date derivation.

Acceptance: leaderboard scenarios in `specs/snapshot-collect/spec.md` and `specs/hub-view-tui/spec.md` pass.
Verify: default verify command.

## 7. Limits strip

Files: `grid_fleet/limits.py`, `grid_fleet/sources_claude.py`, `grid_fleet/view.py`, `tests/test_limits.py`, `docs/spikes.md`.

- [x] 7.1 SPIKE: tap overhead and chaining. Using a synthetic payload and a copy of a statusLine script gated by env vars (the pattern agent-hook tools use: it exits early unless certain variables are set), run directly (not by editing settings.json), time 50 invocations with and without a prototype tap and compare stdout and exit code byte-for-byte, with and without the gating variables set. The tap must pass the full environment through unchanged. Pass: p95 added latency under 50 ms and output identical in both env cases. Fail: record numbers and take the shell-wrapper fallback in `design.md` as a HUMAN decision.
- [x] 7.2 SPIKE: missing limits. (synthetic cases done; the Claude Code statusLine docs now confirm the payload shape: `rate_limits` only for subscribers after the first API response, each window independently absent; real observation done 2026-11-05: first real payload had `rate_limits.seven_day` only, no `five_hour`; partial path and drop of an expired window confirmed; no spec delta, see docs/spikes.md) With synthetic payloads (no `rate_limits`; only `five_hour`; only `seven_day`; both) and one HUMAN observation of a real fresh session, record what appears. Pass: each case maps to the spec's behaviour (file unchanged / partial merge / full write); any real-world payload that differs becomes a spec delta before 7.3.
- [x] 7.3 `limits.py`: `tap` (stdin once, spawn original with identical bytes, pass through stdout and exit code, atomic 0600 slice write, merge rule), reader used by collect.
- [x] 7.4 Collect fills `limits` from the limits file (`null` if absent); view renders per-account limits strip with the colour thresholds, capture age, expiry and `unknown` rules.
- [x] 7.5 Tests for every scenario in `specs/limits-capture/spec.md` and the limits scenarios in `specs/hub-view-tui/spec.md`, including concurrent taps and a credential/network audit test (grep the module source for forbidden calls).

Acceptance: tap is transparent in tests; limits appear once per account in the render output.
Verify: default verify command.

## 8. Installers

Files: `grid_fleet/install.py`, `tests/test_install.py`, `docs/spikes.md`.

- [ ] 8.1 SPIKE: timers across sleep. macOS (HUMAN): load a throwaway launchd agent with a 60 s interval writing timestamps to a temp log, sleep the laptop 3+ minutes, wake, read the log. Linux (HUMAN, a Linux box): same with `systemd-run --user --on-unit-active=60` and with `OnBootSec` after reboot with and without linger. Pass: a run occurs within one interval after wake/boot on both, or the actual behaviour is documented and `design.md` risks updated. Fail: choose a different scheduling primitive and bring it to the human.
- [x] 8.2 `install.py` plan/apply/uninstall for launchd plist and systemd user units, interval validation (300/600/900), absolute paths, install-time `PATH`, linger note, idempotence. Render functions are pure and tested; apply shells out only to `launchctl`/`systemctl` behind an injectable runner.
- [x] 8.3 `install --statusline`: exact before/after print, confirmation, backup, atomic write, original saved to config, restore on `--uninstall`; refuses on non-TTY.
- [x] 8.4 Tests for every scenario in `specs/install-timers/spec.md` against a temp HOME and a fake runner. No test touches the real `~/.claude/settings.json` or real launchd/systemd. (Timer scenarios in `tests/test_install.py`; statusLine scenarios in `tests/test_install_statusline.py`.)
- [x] 8.5 HUMAN: run `install` (plan), then `install --apply` on one real machine; confirm the timer fires and the hub shows it. Then `install --statusline` and confirm the original statusLine output is unchanged. (done 2026-11-05: plan, `--apply`, first tick delivered to the hub; `--statusline` diff reviewed, `y` typed, backup written, no visible change. All six machines then onboarded, six of six fresh. See docs/spikes.md)

Acceptance: plan output is deterministic; apply is idempotent; uninstall restores the original statusLine byte-for-byte in tests.
Verify: default verify command.

## 9. Docs and handover

Files: `README.md`, `CLAUDE.md`, `docs/architecture.md`, `docs/privacy.md`, `TODO.md`.

- [x] 9.1 README: install (clone + symlink), config example, quick start for spoke and hub, privacy section, status updated from "pre-alpha, spec approved, build not started". (done: README checked against `--help`, `install` plan mode and the code at 8a680a0, including the installer config write and the stale-host limits behaviour)
- [x] 9.2 the-grid pointer (HUMAN-reviewed; separate change on its own branch in the-grid, NOT in this repo): add a short docs entry in the-grid linking to grid-fleet as an optional, independent companion. The grid-fleet README already links the other way. Do not touch the-grid's in-flight worktrees; branch from its current default. (done 2026-11-05: pushed to the-grid main as 8028bb6, README `## Docs` section)
- [x] 9.3 HUMAN: public-readiness check. Grep the tree for machine names, IPs, usernames, home paths, account names; confirm none; confirm fixtures are synthetic. Pass: zero hits. Then create `oneafrikan/grid-fleet` (MIT) and push. A private remote first is acceptable; flip to public only after this grep passes. Only after groups 1-5 pass. (done 2026-11-05: a grep of the tracked tree and of every added line in the full git history found no machine names, hostnames, usernames, emails or home paths; benign hits only: the generic tool name `cloudflared access ssh` in `docs/spikes.md`, test fixtures, and one old `CLAUDE.md` line naming an ssh alias, removed from HEAD and still in history. Security review: GO. The repo is public.)
- [ ] 9.4 Archive this change (`openspec archive fleet-tracer-bullet`) once the tasks above are done, and record the verified Claude Code version in the tested list.

Acceptance: a stranger can clone, symlink, run `grid-fleet install` for a spoke and hub, and see a dashboard using only the README.
Verify: default verify command plus the grep in 9.3.
