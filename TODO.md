# TODO

## State

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [x] Groups 1-4 built and independently QA'd (253 tests, 1 skipped, on `main`)
- [x] Real multi-host bring-up done (3.4 ticked, see `docs/spikes.md`)
- [x] Private remote exists and `main` is pushed
- [ ] Group 5 (5.1, 5.2) not formally ticked; 5.1 mostly observed (several hosts, correct busy counts, grey `unknown` after 2 x interval seen)
- [ ] Groups 6-9 not started

## How to run it

- Local try-out: `scripts/try-local.sh [--no-view]` (throwaway state dir; collect twice, push to hub `local`, view; cleans up after itself).
- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system 3.9); `python3 -m unittest discover -s tests` on Linux.
- Commands (`bin/grid-fleet --help`): `collect` writes the local snapshot, `push` delivers it to the hub, `view` is the dashboard (run on the hub). `install` is a stub until Group 8.
- Config: `~/.config/grid-fleet/config.json`, JSON, e.g. `{"schema":1,"host_name":"<name>","hub":"local"}`. Override the path with `GRID_FLEET_CONFIG`; the other `GRID_FLEET_*` overrides are listed in `grid_fleet/config.py`.
- Hub mode: `"hub": "local"` copies into the local inbox (no ssh; use on the hub itself); any other value is an ssh alias, used with `BatchMode`.

## Next steps (in order)

PROPOSED order, pending the maintainer's confirmation:

1. Close 5.1 and 5.2 (record the observed result, decide to proceed).
2. Group 8, timers only: 8.1, 8.2, 8.4, 8.5. Record the install-time `PATH` (follow-up 8).
3. Group 7, limits strip.
4. Then 8.3 `install --statusline` (needs the 7.3 tap, follow-up 13).
5. Group 6, leaderboard and sparkline.
6. Security review gate (follow-up 11).
7. QA re-gate (follow-up 12).
8. Group 9, docs and handover. 9.3 public flip stays HUMAN.

Standing rules:

- Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`, before coding.
- Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## HUMAN-only tasks

- 2.2 capture a real `waiting` row
- 2.1 `CLAUDE_CONFIG_DIR` sub-check: set it to a temp dir, start a throwaway session, record yes/no in `docs/spikes.md`
- 2.8 reference-machine cold/warm collect timing
- 4.5 view check in a real terminal: colours and live resize (text-capture layout check at 120x40 and 80x24 is done, see `docs/spikes.md`)
- Group 5 tracer-bullet checkpoint (two real machines)
- 7.2 real fresh-session observation of missing `rate_limits`
- 8.1 timers-across-sleep spike (macOS and Linux)
- 8.5 real `install` / `install --apply` / `--statusline`
- 9.2 the-grid docs pointer: separate change on its own branch in https://github.com/oneafrikan/the-grid, HUMAN-reviewed; do not touch its in-flight worktrees
- 9.3 public-readiness gate: grep the tree for machine names, IPs, usernames, home paths, account names. Allowlist: the GitHub handle `oneafrikan` (README/TODO/tasks links) and the copyright holder name in `LICENSE`. Every other machine name, IP, username, home path or account name must have zero hits.

## Known limits / follow-ups (from QA of groups 1-4, none blocking)

1. A stored snapshot's `host` is not re-checked against config after a `host_name` rename within `interval_s`; the old `host` is delivered under the new filename (the view uses the filename, so no visible effect).
2. A snapshot's `interval_s` is unbounded; a host claiming a huge interval never goes stale (spec says take it from the snapshot; consider clamping in the hub reader).
3. The remote tmp file `.<host>.json.tmp` is shared between concurrent pushes (timer plus manual run); consider a per-process suffix.
4. The hub inbox path is derived from `XDG_STATE_HOME`/`$HOME` in the ssh non-interactive shell, while `view` uses `GRID_FLEET_STATE_DIR`/interactive XDG; push and view can disagree if the hub sets them differently.
5. `validate_hub` rejects a leading `-`, whitespace and control characters only; shell metacharacters pass (harmless in grid-fleet, no shell is involved, but a user `ProxyCommand` using `%h` could expand them).
6. `tests/test_guard.py` scans `grid_fleet/` only, not `bin/grid-fleet`, and checks only `match`, `X | Y` annotations and imports.
7. The upload completeness check catches truncation, not every short stream that ends in a newline (tool-written snapshots are one line plus a newline, so the known gap cannot occur for them).
8. `claude` is not on the non-interactive ssh PATH on hosts that install it in `~/.local/bin`, so `sessions` is null with warning `claude agents unavailable`. Timers will hit the same thing. The Group 8 installer must record the install-time PATH, and tests should cover it. Config has no `claude` path key; only `GRID_FLEET_CLAUDE_BIN` or PATH can fix it.
9. Add the verified Claude Code version to the tested list at 9.4 (currently warns on 2.1.306).
10. Onboard the two remaining machines as spokes. Per-host config lives outside the repo on each machine by design; do not record host details here.
11. Security review gate before going public: run `grid-security-reviewer` on push and install (host-name charset is a security boundary). Currently only in the `CLAUDE.md` agent table, not a task.
12. QA re-gate (`grid-qa-engineer`) after Groups 6-8.
13. 8.3 `install --statusline` depends on the Group 7 tap (7.3), so Group 8 splits: timers first, statusLine after Group 7. Note only; `tasks.md` is not amended.
14. A bad `GRID_FLEET_NOW` makes `collect` print a traceback (`ConfigError` uncaught); decide whether to handle it.
15. One test is skipped per run, by design: the guard tests skip on the Python version that cannot exercise them (`test_match_detected`: "match is a SyntaxError on 3.9"; `test_match_unparseable_on_39_is_reported`: "parses on this Python" on 3.10+). Seen on 3.9 and 3.14; 3.12 not run here.
16. Until timers exist (Group 8), every host goes grey `unknown` about 2 x interval after its last manual push. Expected, not a bug.

## Remote

- Remote created (private) and `main` pushed.
- Flip to public only after the 9.3 grep passes and groups 1-5 pass.

## Future decisions: outcomes roster (v2 idea, undecided)

An "outcomes roster": not a task list, a team-roster-style view of what is being built and where (outcome, repo@branch/worktree, machine, active sessions, state). Leaning: a v2 view over the same snapshots, with opt-in repo/branch fields (changes the privacy contract). Not in `fleet-tracer-bullet`.

Questions to answer before specifying it:

1. Is an "outcome" an OpenSpec change, a branch, or a free-text goal you type?
2. Should it be auto-derived, hand-declared, or the hybrid?
3. Should it be a tab in grid-fleet's TUI, or its own tool?
4. Should it wait until grid-fleet's tracer bullet is done?
