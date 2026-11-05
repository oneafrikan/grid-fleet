# TODO

## State

Status at `2cc9a52` on `main`. The repo is public. Suite at that commit: 735 tests OK, 1 skipped (a version-dependent guard test, by design), on `/usr/bin/python3` 3.9.6 (run 2026-11-05). The 722 count at `8421f00` was also seen on Homebrew Python 3.14; the 735 run was 3.9 only.

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [x] Groups 1-8 built and independently QA'd (`grid-qa-engineer`): tracer bullet, leaderboard and sparkline (6), limits tap, hub aggregation and view (7), timers and `install --statusline` (8)
- [x] Security review (`grid-security-reviewer`): GO to flip public after the doc items. Code findings S3 (unique remote tmp), S4 (backup and dir modes), S5 (hub caps), S6 (ssh hardening, alias charset), S8 (chunked transcript reads) fixed and merged. Doc items done: trust model and custom agent names in the README.
- [x] Real multi-host bring-up: six machines all report to the hub (3.4 ticked, see `docs/spikes.md`)
- [x] Group 5 (5.1, 5.2) ticked on the maintainer's sign-off
- [x] Spikes 2.1 (incl. `CLAUDE_CONFIG_DIR` sub-check), 2.8, 7.1 (pass) and 7.2 (synthetic and real) done, recorded in `docs/spikes.md`
- [x] Repo is public (`oneafrikan/grid-fleet`, flipped by the maintainer 2026-11-05) and `main` is pushed
- [x] Stale hosts' limits are included in the limits strip (merged)
- [x] `install` writes `interval_s`, `hub` and `account` to the config; `--apply` refuses with no hub (merged)
- [x] Group 6 QA leftovers closed: full-label agent aggregation, test gaps M13, H17, H18, V04, V12
- [x] 9.1 README ticked: checked against `--help`, `install` plan mode and the code at `8a680a0`
- [x] 8.5 done 2026-11-05: install plan, `--apply` and `--statusline` on one laptop, then timers on all six machines; the hub view shows `6 of 6 hosts fresh` (see `docs/spikes.md`)
- [x] 7.2 real observation done 2026-11-05: first real payload had `seven_day` only (13%), no `five_hour`; matches the spec
- [x] Installer polish landed (commit `a5e3ade` and neighbours): `install` warns, in plan and apply, when `claude` is not on the `PATH` it records (follow-up 8); `--help` description fixed; hub value capped at 255 characters; QA test gaps closed
- [x] 4.5 done 2026-11-05: the maintainer reviewed the hub view in a real terminal (6 of 6 hosts, limits strip, sparkline, leaderboard); colours and resize behaviour fine as is
- [x] 9.3 public-readiness gate passed 2026-11-05: grep of the tracked tree and of every added line in the full git history found no machine names, hostnames, usernames, emails or home paths (benign hits only, see `tasks.md`); security review GO; repo flipped public
- [x] 9.2 the-grid pointer (done 2026-11-05: pushed to the-grid main as 8028bb6, README `## Docs` section)
- [x] Push retry merged (`2cc9a52`): `push` retries transient ssh transport failures within one tick (up to 3 attempts, 5 s then 15 s apart; only ssh exit 255 or the 60 s backstop timeout; see README). Independent QA of the retry is in progress.
- [x] Hub tunnel fixed 2026-11-05 (maintainer, on the hub: `protocol: http2` in the tunnel config, then a service restart). About 1 in 4 spoke connections had been failing; afterwards 12 of 12 test connections succeeded and all six hosts were fresh. Infrastructure, not a grid-fleet bug; record in `docs/spikes.md`.
- [ ] HUMAN checkpoints pending: 8.1 (timers across sleep and reboot), 2.2 (a real `waiting` row; attempted 2026-11-05, see below)
- [ ] Gates pending: QA of the push retry (in progress); 9.4 (archive; the tested list already holds 2.1.304, 2.1.305, 2.1.311 and 2.1.318)

## How to run it

- Local try-out: `scripts/try-local.sh [--no-view]` (throwaway state dir; collect twice, push to hub `local`, view; cleans up after itself).
- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system 3.9); `python3 -m unittest discover -s tests` on Linux.
- Commands (`bin/grid-fleet --help`): `collect` writes the local snapshot, `push` delivers it to the hub, `view` is the dashboard (run on the hub). `install --hub <hub-alias> --interval 600` prints the plan (config block and timer files) by default; `--apply`, `--statusline` and `--uninstall` need a TTY and a `y` (see README).
- Config: `~/.config/grid-fleet/config.json`, JSON, e.g. `{"schema":1,"host_name":"<name>","hub":"local"}`. `install --apply` writes `interval_s`, `hub` and `account` into it. Override the path with `GRID_FLEET_CONFIG`; the other `GRID_FLEET_*` overrides are listed in `grid_fleet/config.py`.
- Hub mode: `"hub": "local"` copies into the local inbox (no ssh; use on the hub itself); any other value is an ssh alias, used with `BatchMode`.

## Next steps (in order)

1. Land the independent QA verdict on the push retry (in progress), then act on any findings.
2. HUMAN: 8.1 timers across sleep and reboot (runbook below; waiting for the maintainer).
3. HUMAN, lowest value: 2.2 a real `waiting` row, with a trigger that always prompts.
4. 9.4: archive the change (the verified Claude Code versions are already in the tested list: 2.1.304, 2.1.305, 2.1.311 and 2.1.318).
5. Decide follow-up 36 (`GRID_FLEET_CLAUDE_BIN` is not recorded in the timer unit).

Standing rules:

- Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`, before coding.
- Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## Maintainer decisions (DECIDED and implemented)

- A. Stale hosts' limits: DECIDED and DONE. Stale hosts' limits are included in the limits strip (`build_limits` in `grid_fleet/hub.py`). Totals, leaderboard and sparkline still exclude stale hosts.
- B. Installer config: DECIDED and DONE (independently QA'd). `install` writes `interval_s`, `hub` and `account` to the config, and `--apply` refuses with no hub (`grid_fleet/install.py`).

## HUMAN-only tasks

- 8.1 timers across sleep (macOS and Linux), see below.
- 9.4 archive the change and record the verified Claude Code version (per `tasks.md`, once the tasks above are done).
- 2.2 capture a real `waiting` row: trigger a permission prompt in a throwaway interactive session, run `claude agents --json` read-only, record the row's keys (not values) and the wall time in `docs/spikes.md`.
  - Attempted 2026-11-05: the maintainer's Claude Code session auto-allowed the file write, so no prompt appeared and no `waiting` row was captured.
  - Still open, lowest value. It needs a trigger that always prompts, for example a different permission mode.

Done and removed from this list: 2.1 `CLAUDE_CONFIG_DIR` sub-check, 2.8 timing, Group 5 checkpoint, 7.2 real observation and 8.5 (all 2026-11-05), 4.5 colours and resize check, 9.3 public-readiness gate and the public flip (both 2026-11-05).

## 8.5 runbook (HUMAN, done 2026-11-05; kept for onboarding new machines)

Placeholders: `<hub-alias>` is the ssh alias of the hub from the spoke's `~/.ssh/config` (or `local` on the hub itself). `<state_dir>` defaults to `~/.local/state/grid-fleet`. Unit names below are from `grid_fleet/install.py`.

Before you start:

- macOS: run the installer with `/usr/bin/python3` (for example `/usr/bin/python3 bin/grid-fleet install ...`). The unit records the interpreter that ran it. Homebrew Python would record a Homebrew path that can vanish on a version bump.
- Over ssh: put the directory containing `claude` on `PATH` for the install command, for example `PATH=$HOME/.local/bin:$PATH`. The unit records the install-time `PATH`; if `claude` is missing from it, every tick reports `sessions: null` (this happened once, see follow-up 8).

a. On the machine to onboard, print the plan:
   - `bin/grid-fleet install --hub <hub-alias> --interval 600`
   - Review the `config:` block (the before and after of `interval_s`, `hub`, `account`) and the timer plan (interpreter, script, `PATH`, files, commands). Nothing changes yet.
b. Install the timer:
   - `bin/grid-fleet install --hub <hub-alias> --interval 600 --apply`, then type `y`. It writes the config first, then loads the timer.
   - macOS: `launchctl print gui/$(id -u)/io.grid-fleet.timer`. The tick log is `<state_dir>/timer.log`.
   - Linux: `systemctl --user list-timers grid-fleet.timer` and `journalctl --user -u grid-fleet`. Always-on or headless Linux host: the maintainer runs `loginctl enable-linger $USER` (install only prints the note; the timer does not run while logged out without it).
   - Run `bin/grid-fleet view` on the hub and wait one tick (macOS runs once at load, Linux about 30 s after the timer starts, then every interval). Confirm the host's age drops below the interval.
c. Capture limits:
   - In a REAL terminal: `bin/grid-fleet install --statusline`. Read the exact before and after of `statusLine.command`, then type `y`.
   - Start a new Claude Code session and confirm the statusLine looks unchanged.
   - After the session's first response: `cat <state_dir>/limits.json` shows percentages. Before the first response the file may not exist. A payload can carry only one window, and then the file holds only that window (the real 7.2 observation: `seven_day` only).
d. Roll back:
   - Timers: `bin/grid-fleet install --uninstall` (the config keys `interval_s`, `hub`, `account` stay).
   - StatusLine only: `bin/grid-fleet install --statusline --uninstall`.
   - Backups of the settings file sit next to it as `settings.json.grid-fleet-backup-<UTC time>` (for example `settings.json.grid-fleet-backup-20261104T101530Z`), in `~/.claude/` or `$CLAUDE_CONFIG_DIR/`. They are never pruned and can hold secrets: delete them once you are happy.

## 8.1 sleep spike (HUMAN)

- macOS: load a throwaway launchd agent with a 60 s `StartInterval` that appends timestamps to a temp log, sleep the laptop for 3+ minutes, wake it, read the log for a run within one interval of wake.
- Linux: a transient `systemd-run --user --on-unit-active=60 ...` timer, then reboot with and without linger and check that a run occurs.
- Why: laptops sleep; the timers must fire after wake or the host goes grey `unknown` on the hub.

## Known limits / follow-ups (1-16 from QA of groups 1-4, 17-25 from the README unit, 26-32 later, 33-34 from the final docs pass, 35 from the 8.5 rollout, 36 from the installer polish, 37-38 from the push-retry and tunnel incident, 39 from the idle column, 40-43 from QA of the push retry; none blocking)

1. A stored snapshot's `host` is not re-checked against config after a `host_name` rename within `interval_s`; the old `host` is delivered under the new filename (the view uses the filename, so no visible effect).
2. FIXED (S5): the hub clamps a snapshot's `interval_s` to 3600 (`MAX_INTERVAL_S` in `grid_fleet/hub.py`), so a host claiming a huge interval goes stale.
3. FIXED (S3): the remote tmp file is unique per push (`.<host>.json.$$.tmp`) and removed on exit. Residual: see follow-up 28.
4. The hub inbox path is derived from `XDG_STATE_HOME`/`$HOME` in the ssh non-interactive shell, while `view` uses `GRID_FLEET_STATE_DIR`/interactive XDG; push and view can disagree if the hub sets them differently.
5. FIXED (S6): the hub alias is limited to `[A-Za-z0-9._@:-]` with no leading `-`, and `push` passes `-o ClearAllForwardings=yes` and `--` before the alias (`grid_fleet/push.py`).
6. `tests/test_guard.py` scans `grid_fleet/` only, not `bin/grid-fleet`, and checks only `match`, `X | Y` annotations and imports.
7. The upload completeness check catches truncation, not every short stream that ends in a newline (tool-written snapshots are one line plus a newline, so the known gap cannot occur for them).
8. `claude` is not on the non-interactive ssh PATH on hosts that install it in `~/.local/bin`, so `sessions` is null with warning `claude agents unavailable`. BIT US ONCE at 8.5: a macOS timer installed over ssh recorded a PATH without `claude`, every tick reported `sessions: null`, and reinstalling with the directory on PATH fixed it. The installer (8.2) records the install-time PATH in the unit. An install-time warning (plan and apply) when `claude` is not on the recorded PATH has landed (`a5e3ade`). Config has no `claude` path key; only `GRID_FLEET_CLAUDE_BIN` or PATH can fix it (see follow-up 36).
9. DONE: the tested list holds 2.1.304, 2.1.305, 2.1.311 and 2.1.318 (verified on real hosts 2026-11-05); other versions warn. The archive step (9.4) is still open.
10. DONE: all six machines are onboarded as spokes. Per-host config lives outside the repo on each machine by design; do not record host details here.
11. DONE: security review gate (`grid-security-reviewer`): GO to flip public after the doc items, which are done.
12. DONE: QA re-gate (`grid-qa-engineer`) after Groups 6-8: PASS.
13. 8.3 `install --statusline` depended on the Group 7 tap (7.3), so Group 8 was split: timers first, statusLine after Group 7. Both are built.
14. A bad `GRID_FLEET_NOW` makes `collect` print a traceback (`ConfigError` uncaught); decide whether to handle it.
15. One test is skipped per run, by design: the guard tests skip on the Python version that cannot exercise them (`test_match_detected`: "match is a SyntaxError on 3.9"; `test_match_unparseable_on_39_is_reported`: "parses on this Python" on 3.10+). Seen on 3.9 and 3.14; 3.12 not run here.
16. Without an installed timer, every host goes grey `unknown` about 2 x interval after its last manual push. Expected, not a bug. The timers are built (8.2) and verified on real machines at 8.5 (six of six fresh), but not yet across sleep or reboot (8.1).
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
31. DONE: a host without read access to the then-private repo got an archive copy instead of a clone. The repo is public now, so that host can use a plain clone; the per-host conversion is tracked outside this repo.
32. Tap latency under saturated load is about 41-43 ms p95 added (`docs/spikes.md` 7.1b). Under the 50 ms bound. The `json` import is the remaining cost.
33. The view clips the agent type column to at most 20 cells with an ellipsis (`BOARD_TYPE_MAX` in `grid_fleet/view.py`), so distinct long type names that share their first 19 characters look identical on screen even though the hub counts them separately. A view decision: widen the column, or disambiguate.
34. Two cosmetic code nits found in the final docs pass (docs left as is; route to the dev role):
    - The plan's `config:` block prints a note that begins `settings.json is not in a layout grid-fleet can reproduce` when the CONFIG file is rewritten in the fallback layout (`_REWRITE_NOTE` in `grid_fleet/install.py` is shared with the settings.json edit).
    - FIXED: `bin/grid-fleet --help` showed an internal description instead of a user-facing one.
35. Idea, not a decision: the view could show a per-host reason when `sessions` is null (today it shows only `unknown`), for example `claude agents unavailable` from the snapshot warning. Motivated by the 8.5 PATH incident (follow-up 8).

36. `GRID_FLEET_CLAUDE_BIN` is honoured by the install-time `claude` warning but is NOT recorded in the timer unit (only `PATH` is). A user who relies on that variable gets no warning, yet their ticks will not see it. Fix options: record it in the unit, or add a `claude_bin` config key. Found by the installer polish agent.

37. Idea, not decided: surface "push failing" to the person watching. Today a host that fails every push just ages to grey `unknown` on the hub, with the reason only in its local timer log. Options: record the last push result in the snapshot (the hub only sees snapshots that arrived, so a failing host cannot report its own failure; this would show the previous failure on the next success), or a hub-side hint when a host is stale for longer than expected. Motivated by the tunnel incident (`docs/spikes.md`).
38. Describe only (QA of the push retry is checking this): with retries, the push part of one tick can take up to 200 s (3 x 60 s backstop + 5 s + 15 s). That is under the shortest interval `install` accepts (300 s). `push.py` has no lock or overlap guard of its own; whether launchd and systemd already refuse to start a tick while the previous one runs was not checked here. Act on QA's finding, not on this note. See follow-up 43.
39. The idle column is dropped at <63 cols; no other column-dropping rule exists.
40. Ctrl-C during a push retry wait prints a traceback (found by QA of the push retry).
41. A permanent ssh error (for example a bad alias) is now retried, so it costs about 20 s per tick (the 5 s and 15 s waits).
42. Push-retry test gaps from QA: m17 and m35 are uncovered, and the worst-case-duration test is tautological.
43. Unverified: the systemd overlap behaviour, that is, what happens when a tick is still running when the next one fires (extends follow-up 38).

## Remote

- `origin` is `oneafrikan/grid-fleet`, PUBLIC (flipped 2026-11-05 after the 9.3 grep passed and the security gate was GO). `main` is pushed.
- Do not change visibility or the remote without the maintainer.

## Future decisions: outcomes roster (v2 idea, undecided)

An "outcomes roster": not a task list, a team-roster-style view of what is being built and where (outcome, repo@branch/worktree, machine, active sessions, state). Leaning: a v2 view over the same snapshots, with opt-in repo/branch fields (changes the privacy contract). Not in `fleet-tracer-bullet`.

Questions to answer before specifying it:

1. Is an "outcome" an OpenSpec change, a branch, or a free-text goal you type?
2. Should it be auto-derived, hand-declared, or the hybrid?
3. Should it be a tab in grid-fleet's TUI, or its own tool?
4. Should it wait until grid-fleet's tracer bullet is done?
