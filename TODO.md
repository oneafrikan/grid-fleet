# TODO

## State

Status at `47f418f` on `main`. Suite: 632 tests OK, 1 skipped (a version-dependent guard test, by design), on `/usr/bin/python3` 3.9.6 and Homebrew 3.14.

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [x] Groups 1-8 built and independently QA'd (`grid-qa-engineer`): tracer bullet, leaderboard and sparkline (6), limits tap, hub aggregation and view (7), timers and `install --statusline` (8)
- [x] Security review (`grid-security-reviewer`): GO to flip public after the doc items. Code findings S3 (unique remote tmp), S4 (backup and dir modes), S5 (hub caps), S6 (ssh hardening, alias charset), S8 (chunked transcript reads) fixed and merged. Doc items done: trust model and custom agent names in the README.
- [x] Real multi-host bring-up: six machines all report to the hub (3.4 ticked, see `docs/spikes.md`)
- [x] Group 5 (5.1, 5.2) ticked on the maintainer's sign-off
- [x] Spikes 2.1 (incl. `CLAUDE_CONFIG_DIR` sub-check) and 2.8 done, recorded in `docs/spikes.md`
- [x] Private remote exists and `main` is pushed
- [ ] In progress, not on `main` at `47f418f`: (a) `install` writes `interval_s`, `hub` and `account` to the config, and `--apply` refuses with no hub; (b) stale hosts' limits count in the limits strip. Both are maintainer decisions (below). README and `tasks.md` 9.1 need a follow-up once (a) and (b) land.
- [ ] HUMAN checkpoints pending: 8.1, 8.5, 4.5, 7.2, 2.2, then 9.2, 9.3
- [ ] 9.1 README written but unticked (see above); 9.4 not started

## How to run it

- Local try-out: `scripts/try-local.sh [--no-view]` (throwaway state dir; collect twice, push to hub `local`, view; cleans up after itself).
- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system 3.9); `python3 -m unittest discover -s tests` on Linux.
- Commands (`bin/grid-fleet --help`): `collect` writes the local snapshot, `push` delivers it to the hub, `view` is the dashboard (run on the hub). `install` prints the timer plan by default; `--apply`, `--statusline` and `--uninstall` need a TTY and a `y` (see README).
- Config: `~/.config/grid-fleet/config.json`, JSON, e.g. `{"schema":1,"host_name":"<name>","hub":"local"}`. At `47f418f` `install` does not write `hub`, `account` or `interval_s` (change in progress, decision B). Override the path with `GRID_FLEET_CONFIG`; the other `GRID_FLEET_*` overrides are listed in `grid_fleet/config.py`.
- Hub mode: `"hub": "local"` copies into the local inbox (no ssh; use on the hub itself); any other value is an ssh alias, used with `BatchMode`.

## Next steps (in order)

1. Land the two in-progress changes (installer config write, stale-host limits). Re-run the full suite on 3.9 after each. Then update the README `install`, Config and limits wording and tick 9.1.
2. HUMAN: 8.1 and 8.5 on real machines, then 4.5, 7.2 and 2.2.
3. 9.4: archive the change and record the verified Claude Code version (2.1.318 was observed; the tested list holds 2.1.305).
4. HUMAN: 9.3 public-readiness grep, then the public flip; 9.2 the-grid pointer.

Standing rules:

- Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`, before coding.
- Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## Maintainer decisions (DECIDED; implementation status noted)

- A. Stale hosts' limits: DECIDED, stale hosts' limits are INCLUDED in the limits strip. Implementation in progress. At `47f418f` the hub builds the strip from fresh hosts only (`grid_fleet/hub.py`, `build_limits`).
- B. Installer config: DECIDED, `install` writes `interval_s`, `hub` and `account` to the config, and `--apply` refuses with no hub. Implementation in progress. At `47f418f` it does neither (`grid_fleet/install.py` header: hub/account are not written; `--interval` only warns).

## HUMAN-only tasks

- 2.2 capture a real `waiting` row
- 4.5 view check in a real terminal: colours and live resize (text-capture layout check at 120x40 and 80x24 is done, see `docs/spikes.md`)
- 7.2 real fresh-session observation of missing `rate_limits` (the docs confirm the payload shape; the real observation is still wanted)
- 8.1 timers across sleep (macOS and Linux): laptops sleep, so verify the timers fire after wake or the host goes grey
- 8.5 real `install --apply` for the timers, and `install --statusline`: both need a TTY, and `--statusline` needs the maintainer's `y` on the exact diff, so an agent cannot run them. Run from a real terminal on one real machine; confirm the timer fires, the hub shows the host, and the original statusLine output is unchanged. The exact wrapped command is shown in that diff (see follow-up 26).
- 9.2 the-grid docs pointer: separate change on its own branch in https://github.com/oneafrikan/the-grid, HUMAN-reviewed; do not touch its in-flight worktrees
- 9.3 public-readiness gate: grep the tree for machine names, IPs, usernames, home paths, account names. Allowlist: the GitHub handle `oneafrikan` (README/TODO/tasks links) and the copyright holder name in `LICENSE`. Every other machine name, IP, username, home path or account name must have zero hits.

Done and removed from this list: 2.1 `CLAUDE_CONFIG_DIR` sub-check, 2.8 timing, Group 5 checkpoint.

## Known limits / follow-ups (1-16 from QA of groups 1-4, 17-25 from the README unit, 26-32 later; none blocking)

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
20. Hub account labels are cut to 32 ASCII characters, so near-identical long labels can merge into one limits block.
21. No warning when `limits.json` is corrupt or unreadable: `limits` is `null` and the view shows `unknown`. Consider a `limits file unreadable` warning.
22. Limits carry one `captured_at` for both windows, so the capture age can understate the age of a window that was absent from the latest payload. Consider per-window capture times if this matters.
23. The `.claude/` OpenSpec scaffold exists in git history (commit "openspec init"). Kept (maintainer decision); it is gitignored and untracked now.
24. `settings.json` backups from `install --statusline` and `--uninstall` accumulate unpruned next to the file. They can hold secrets (README Security model). S4: their mode is the original's mode capped at 0600.
25. The tap applies no timeout to the original statusLine command, by design: a hung original hangs the statusLine exactly as it would without the tap.
26. Shell-dialect risk: the wrapper runs the original under `/bin/sh -c`. The Claude Code statusLine docs say the command "runs in a shell" without naming which, and `grid_fleet/install.py` assumes `/bin/sh` (a code comment, not a documented fact). A bash-only original (for example `[[ ]]`) may behave differently under dash. Possible mitigation: run the original under `$SHELL`. The exact wrapped command is shown in the diff at 8.5, so check it there.
27. Group 6 QA leftovers:
    - The hub cuts agent labels to 24 characters BEFORE aggregation (`AGENT_TYPE_MAX` in `grid_fleet/hub.py`), so distinct types sharing a 24-character prefix merge. Fix: merge on the full cleaned label and clip only for display.
    - Test gaps: future-dated hour events in `by_day`; the per-host agent-row cap and by_day-row cap; the 1-cell bar minimum; output-column crowding.
28. S3 residual: a SIGKILLed or dropped upload leaves a unique dot-prefixed tmp file in the hub inbox that nothing cleans up. Readers ignore it.
29. S8 residual: one transcript line with no newline is held whole. There is no line-length cap (design decision: the chunked reader bounds reads, not line length).
30. A limits capture dated more than 5 minutes ahead of the hub clock is ignored (`MAX_CAPTURE_SKEW_S` in `grid_fleet/hub.py`), so a spoke with a badly wrong clock loses its limits display.
31. A host without read access to the private repo got an archive copy instead of a clone. It becomes a plain clone after the public flip.
32. Tap latency under saturated load is about 41-43 ms p95 added (`docs/spikes.md` 7.1b). Under the 50 ms bound. The `json` import is the remaining cost.

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
