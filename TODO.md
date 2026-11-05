# TODO

## State

Status at `8a680a0` on `main`. Suite: 696 tests OK, 1 skipped (a version-dependent guard test, by design), on `/usr/bin/python3` 3.9.6.

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [x] Groups 1-8 built and independently QA'd (`grid-qa-engineer`): tracer bullet, leaderboard and sparkline (6), limits tap, hub aggregation and view (7), timers and `install --statusline` (8)
- [x] Security review (`grid-security-reviewer`): GO to flip public after the doc items. Code findings S3 (unique remote tmp), S4 (backup and dir modes), S5 (hub caps), S6 (ssh hardening, alias charset), S8 (chunked transcript reads) fixed and merged. Doc items done: trust model and custom agent names in the README.
- [x] Real multi-host bring-up: six machines all report to the hub (3.4 ticked, see `docs/spikes.md`)
- [x] Group 5 (5.1, 5.2) ticked on the maintainer's sign-off
- [x] Spikes 2.1 (incl. `CLAUDE_CONFIG_DIR` sub-check), 2.8, 7.1 (pass) and the synthetic half of 7.2 done, recorded in `docs/spikes.md`
- [x] Private remote exists and `main` is pushed
- [x] Stale hosts' limits are included in the limits strip (merged)
- [x] `install` writes `interval_s`, `hub` and `account` to the config; `--apply` refuses with no hub (merged)
- [x] Group 6 QA leftovers closed: full-label agent aggregation, test gaps M13, H17, H18, V04, V12
- [x] 9.1 README ticked: checked against `--help`, `install` plan mode and the code at `8a680a0`
- [ ] Installer config write: independent QA in progress. Docs may need a small follow-up if it reports defects.
- [ ] HUMAN checkpoints pending: 2.2, 4.5, 7.2 (real observation), 8.1, 8.5; then gates 9.2, 9.3
- [ ] 9.4 not started (after the HUMAN items)

## How to run it

- Local try-out: `scripts/try-local.sh [--no-view]` (throwaway state dir; collect twice, push to hub `local`, view; cleans up after itself).
- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system 3.9); `python3 -m unittest discover -s tests` on Linux.
- Commands (`bin/grid-fleet --help`): `collect` writes the local snapshot, `push` delivers it to the hub, `view` is the dashboard (run on the hub). `install --hub <hub-alias> --interval 600` prints the plan (config block and timer files) by default; `--apply`, `--statusline` and `--uninstall` need a TTY and a `y` (see README).
- Config: `~/.config/grid-fleet/config.json`, JSON, e.g. `{"schema":1,"host_name":"<name>","hub":"local"}`. `install --apply` writes `interval_s`, `hub` and `account` into it. Override the path with `GRID_FLEET_CONFIG`; the other `GRID_FLEET_*` overrides are listed in `grid_fleet/config.py`.
- Hub mode: `"hub": "local"` copies into the local inbox (no ssh; use on the hub itself); any other value is an ssh alias, used with `BatchMode`.

## Next steps (in order)

1. Wait for the installer config-write QA. Fix docs only if it reports behaviour that differs from the README.
2. HUMAN: 8.1 and 8.5 on real machines (runbook below), then 4.5, 7.2 and 2.2.
3. 9.4: archive the change and record the verified Claude Code version (2.1.318 was observed; the tested list holds 2.1.305).
4. HUMAN: 9.3 public-readiness grep, then the public flip; 9.2 the-grid pointer.

Standing rules:

- Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`, before coding.
- Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## Maintainer decisions (DECIDED and implemented)

- A. Stale hosts' limits: DECIDED and DONE. Stale hosts' limits are included in the limits strip (`build_limits` in `grid_fleet/hub.py`). Totals, leaderboard and sparkline still exclude stale hosts.
- B. Installer config: DECIDED and DONE (QA in progress). `install` writes `interval_s`, `hub` and `account` to the config, and `--apply` refuses with no hub (`grid_fleet/install.py`).

## HUMAN-only tasks

- 2.2 capture a real `waiting` row: trigger a permission prompt in a throwaway interactive session, run `claude agents --json` read-only, record the row's keys (not values) and the wall time in `docs/spikes.md`.
- 4.5 view check in a real terminal: colours and live resize (the text-capture layout check at 120x40 and 80x24 is done, see `docs/spikes.md`).
- 7.2 real fresh-session observation of missing `rate_limits` (the docs confirm the payload shape; the real observation is still wanted). Covered by step (c) of the 8.5 runbook.
- 8.1 timers across sleep (macOS and Linux), see below.
- 8.5 real `install --apply` and `install --statusline` on one machine, see the runbook below. Both need a TTY, and `--statusline` needs the maintainer's `y` on the exact diff, so an agent cannot run them.
- 9.2 the-grid docs pointer: separate change on its own branch in https://github.com/oneafrikan/the-grid, HUMAN-reviewed; do not touch its in-flight worktrees.
- 9.3 public-readiness gate: grep the tree for machine names, IPs, usernames, home paths, account names. Allowlist: the GitHub handle `oneafrikan` (README/TODO/tasks links) and the copyright holder name in `LICENSE`. Every other machine name, IP, username, home path or account name must have zero hits.

Done and removed from this list: 2.1 `CLAUDE_CONFIG_DIR` sub-check, 2.8 timing, Group 5 checkpoint.

## 8.5 runbook (HUMAN)

Placeholders: `<hub-alias>` is the ssh alias of the hub from the spoke's `~/.ssh/config` (or `local` on the hub itself). `<state_dir>` defaults to `~/.local/state/grid-fleet`. Unit names below are from `grid_fleet/install.py`.

a. On the machine to onboard, print the plan:
   - `bin/grid-fleet install --hub <hub-alias> --interval 600`
   - Review the `config:` block (the before and after of `interval_s`, `hub`, `account`) and the timer plan (interpreter, script, `PATH`, files, commands). Nothing changes yet.
b. Install the timer:
   - `bin/grid-fleet install --hub <hub-alias> --interval 600 --apply`, then type `y`. It writes the config first, then loads the timer.
   - macOS: `launchctl print gui/$(id -u)/io.grid-fleet.timer`. The tick log is `<state_dir>/timer.log`.
   - Linux: `systemctl --user list-timers grid-fleet.timer` and `journalctl --user -u grid-fleet`. Headless box: run `loginctl enable-linger $USER` yourself (install only prints the note).
   - Run `bin/grid-fleet view` on the hub and wait one tick (macOS runs once at load, Linux about 30 s after the timer starts, then every interval). Confirm the host's age drops below the interval.
c. Capture limits:
   - In a REAL terminal: `bin/grid-fleet install --statusline`. Read the exact before and after of `statusLine.command`, then type `y`.
   - Start a new Claude Code session and confirm the statusLine looks unchanged.
   - After the session's first response: `cat <state_dir>/limits.json` shows percentages. This is also the real observation for 7.2. Before the first response the file may not exist; that is the case 7.2 wants recorded.
d. Roll back:
   - Timers: `bin/grid-fleet install --uninstall` (the config keys `interval_s`, `hub`, `account` stay).
   - StatusLine only: `bin/grid-fleet install --statusline --uninstall`.
   - Backups of the settings file sit next to it as `settings.json.grid-fleet-backup-<UTC time>` (for example `settings.json.grid-fleet-backup-20261104T101530Z`), in `~/.claude/` or `$CLAUDE_CONFIG_DIR/`. They are never pruned and can hold secrets: delete them once you are happy.

## 8.1 sleep spike (HUMAN)

- macOS: load a throwaway launchd agent with a 60 s `StartInterval` that appends timestamps to a temp log, sleep the laptop for 3+ minutes, wake it, read the log for a run within one interval of wake.
- Linux: a transient `systemd-run --user --on-unit-active=60 ...` timer, then reboot with and without linger and check that a run occurs.
- Why: laptops sleep; the timers must fire after wake or the host goes grey `unknown` on the hub.

## Known limits / follow-ups (1-16 from QA of groups 1-4, 17-25 from the README unit, 26-32 later, 33-34 from the final docs pass; none blocking)

1. A stored snapshot's `host` is not re-checked against config after a `host_name` rename within `interval_s`; the old `host` is delivered under the new filename (the view uses the filename, so no visible effect).
2. FIXED (S5): the hub clamps a snapshot's `interval_s` to 3600 (`MAX_INTERVAL_S` in `grid_fleet/hub.py`), so a host claiming a huge interval goes stale.
3. FIXED (S3): the remote tmp file is unique per push (`.<host>.json.$$.tmp`) and removed on exit. Residual: see follow-up 28.
4. The hub inbox path is derived from `XDG_STATE_HOME`/`$HOME` in the ssh non-interactive shell, while `view` uses `GRID_FLEET_STATE_DIR`/interactive XDG; push and view can disagree if the hub sets them differently.
5. FIXED (S6): the hub alias is limited to `[A-Za-z0-9._@:-]` with no leading `-`, and `push` passes `-o ClearAllForwardings=yes` and `--` before the alias (`grid_fleet/push.py`).
6. `tests/test_guard.py` scans `grid_fleet/` only, not `bin/grid-fleet`, and checks only `match`, `X | Y` annotations and imports.
7. The upload completeness check catches truncation, not every short stream that ends in a newline (tool-written snapshots are one line plus a newline, so the known gap cannot occur for them).
8. `claude` is not on the non-interactive ssh PATH on hosts that install it in `~/.local/bin`, so `sessions` is null with warning `claude agents unavailable`. Timers will hit the same thing. The installer (8.2) records the install-time PATH in the unit and has tests; verify on a real machine at 8.5. Config has no `claude` path key; only `GRID_FLEET_CLAUDE_BIN` or PATH can fix it.
9. Add the verified Claude Code version to the tested list at 9.4 (the list holds 2.1.305; 2.1.318 was observed on the reference laptop; other versions warn).
10. DONE: all six machines are onboarded as spokes. Per-host config lives outside the repo on each machine by design; do not record host details here.
11. DONE: security review gate (`grid-security-reviewer`): GO to flip public after the doc items, which are done.
12. DONE: QA re-gate (`grid-qa-engineer`) after Groups 6-8: PASS.
13. 8.3 `install --statusline` depended on the Group 7 tap (7.3), so Group 8 was split: timers first, statusLine after Group 7. Both are built.
14. A bad `GRID_FLEET_NOW` makes `collect` print a traceback (`ConfigError` uncaught); decide whether to handle it.
15. One test is skipped per run, by design: the guard tests skip on the Python version that cannot exercise them (`test_match_detected`: "match is a SyntaxError on 3.9"; `test_match_unparseable_on_39_is_reported`: "parses on this Python" on 3.10+). Seen on 3.9 and 3.14; 3.12 not run here.
16. Without an installed timer, every host goes grey `unknown` about 2 x interval after its last manual push. Expected, not a bug. The timers are built (8.2) but not yet verified across sleep on real machines (8.1, 8.5).
17. A restricted receiver (`grid-fleet receive --host NAME`) could back per-spoke forced-command ssh keys, so a spoke can only write its own row. Decided: not in v1; the README Security model documents the trust assumption instead.
18. Custom agent type names are sent to the hub as written. Decided: document it (README Privacy), do not filter.
19. A subagent whose meta file says `agentType` `main` or `unknown` merges into the `main` or `unknown` leaderboard bucket.
20. Hub account labels are cut to 32 ASCII characters, so near-identical long labels can merge into one limits block. `install --account` refuses labels the hub would rewrite; a hand-edited config can still hold one.
21. No warning when `limits.json` is corrupt or unreadable: `limits` is `null` and the view shows `unknown`. Consider a `limits file unreadable` warning.
22. Limits carry one `captured_at` for both windows, so the capture age can understate the age of a window that was absent from the latest payload. Consider per-window capture times if this matters.
23. The `.claude/` OpenSpec scaffold exists in git history (commit "openspec init"). Kept (maintainer decision); it is gitignored and untracked now.
24. `settings.json` backups from `install --statusline` and `--uninstall` accumulate unpruned next to the file. They can hold secrets (README Security model). S4: their mode is the original's mode capped at 0600.
25. The tap applies no timeout to the original statusLine command, by design: a hung original hangs the statusLine exactly as it would without the tap.
26. Shell-dialect risk: the wrapper runs the original under `/bin/sh -c`. The Claude Code statusLine docs say the command "runs in a shell" without naming which, and `grid_fleet/install.py` assumes `/bin/sh` (a code comment, not a documented fact). A bash-only original (for example `[[ ]]`) may behave differently under dash. Possible mitigation: run the original under `$SHELL`. The exact wrapped command is shown in the diff at 8.5, so check it there.
27. DONE: Group 6 QA leftovers:
    - The hub now aggregates agent types on the full cleaned label (64-character cap, `AGENT_TYPE_MAX` in `grid_fleet/hub.py`); the view clips for display. See follow-up 33.
    - Test gaps closed: future-dated hour events in `by_day` (M13), per-host agent-row and by_day-row caps (H17, H18), the 1-cell bar minimum and output-column crowding (V04, V12).
28. S3 residual: a SIGKILLed or dropped upload leaves a unique dot-prefixed tmp file in the hub inbox that nothing cleans up. Readers ignore it.
29. S8 residual: one transcript line with no newline is held whole. There is no line-length cap (design decision: the chunked reader bounds reads, not line length).
30. A limits capture dated more than 5 minutes ahead of the hub clock is ignored (`MAX_CAPTURE_SKEW_S` in `grid_fleet/hub.py`), so a spoke with a badly wrong clock loses its limits display.
31. A host without read access to the private repo got an archive copy instead of a clone. It becomes a plain clone after the public flip.
32. Tap latency under saturated load is about 41-43 ms p95 added (`docs/spikes.md` 7.1b). Under the 50 ms bound. The `json` import is the remaining cost.
33. The view clips the agent type column to at most 20 cells with an ellipsis (`BOARD_TYPE_MAX` in `grid_fleet/view.py`), so distinct long type names that share their first 19 characters look identical on screen even though the hub counts them separately. A view decision: widen the column, or disambiguate.
34. Two cosmetic code nits found in the final docs pass (docs left as is; route to the dev role):
    - The plan's `config:` block prints a note that begins `settings.json is not in a layout grid-fleet can reproduce` when the CONFIG file is rewritten in the fallback layout (`_REWRITE_NOTE` in `grid_fleet/install.py` is shared with the settings.json edit).
    - `bin/grid-fleet --help` shows an internal description ("Owner: skeleton unit (Group 1). LATER UNITS DO NOT EDIT THIS FILE.") instead of a user-facing one.

## Remote

- Remote created (private) and `main` is pushed.
- Flip to public only after the 9.3 grep passes. The security gate is GO (doc items done).

## Future decisions: outcomes roster (v2 idea, undecided)

An "outcomes roster": not a task list, a team-roster-style view of what is being built and where (outcome, repo@branch/worktree, machine, active sessions, state). Leaning: a v2 view over the same snapshots, with opt-in repo/branch fields (changes the privacy contract). Not in `fleet-tracer-bullet`.

Questions to answer before specifying it:

1. Is an "outcome" an OpenSpec change, a branch, or a free-text goal you type?
2. Should it be auto-derived, hand-declared, or the hybrid?
3. Should it be a tab in grid-fleet's TUI, or its own tool?
4. Should it wait until grid-fleet's tracer bullet is done?
