# TODO

## State

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [x] Groups 1-4 built and independently QA'd (253 tests, 1 skipped, at the Group 1-4 gate)
- [x] Real multi-host bring-up done (3.4 ticked, see `docs/spikes.md`)
- [x] Private remote exists and `main` is pushed
- [x] Group 7 built: tap and limits reader (7.3), limits strip (7.4); spike 7.1 passed, 7.2 synthetic cases recorded. Independent QA done for Group 7 (not yet reflected in `tasks.md`, where 7.4 and 7.5 stay unticked).
- [x] Group 8 timers and statusLine chaining built (8.2, 8.3, 8.4 ticked)
- [x] Group 6 built: `agents.last_7d` and `by_day` collect (6.1), sparkline and leaderboard view (6.2)
- [ ] In progress, pending merge: fix units from review (limits tap fast path, push tmp-file uniqueness, ssh option hardening, hub alias charset, backup file modes), QA of Group 6 and of installer 8.3, security review gate on push and install
- [ ] Group 5 (5.1, 5.2) not formally ticked; 5.1 mostly observed (several hosts, correct busy counts, grey `unknown` after 2 x interval seen)
- [ ] Group 9: 9.1 README written in the docs unit (README, CLAUDE.md, this file), pending merge; 9.2 and 9.3 HUMAN; 9.4 not started

## How to run it

- Local try-out: `scripts/try-local.sh [--no-view]` (throwaway state dir; collect twice, push to hub `local`, view; cleans up after itself).
- Tests: `/usr/bin/python3 -m unittest discover -s tests` on macOS (system 3.9); `python3 -m unittest discover -s tests` on Linux.
- Commands (`bin/grid-fleet --help`): `collect` writes the local snapshot, `push` delivers it to the hub, `view` is the dashboard (run on the hub). `install` prints the timer plan by default; `--apply`, `--statusline` and `--uninstall` need a TTY and a `y` (see README).
- Config: `~/.config/grid-fleet/config.json`, JSON, e.g. `{"schema":1,"host_name":"<name>","hub":"local"}`. `install` does not write `hub`, `account` or `interval_s` (open decision B). Override the path with `GRID_FLEET_CONFIG`; the other `GRID_FLEET_*` overrides are listed in `grid_fleet/config.py`.
- Hub mode: `"hub": "local"` copies into the local inbox (no ssh; use on the hub itself); any other value is an ssh alias, used with `BatchMode`.

## Next steps (in order)

PROPOSED order, pending the maintainer's confirmation:

1. Merge the in-progress fix units (see State). Re-run the full suite on 3.9 after each.
2. Record QA results: Group 7 (done), Group 6, installer 8.3. Tick 6.x and 7.4/7.5 in `tasks.md` once QA passes.
3. Security review gate on push and install (`grid-security-reviewer`, follow-up 11), then QA re-gate (`grid-qa-engineer`, follow-up 12).
4. Merge the docs unit (README, CLAUDE.md, this file) and tick 9.1.
5. HUMAN: Group 5 checkpoint (5.1, 5.2), then 8.1 and 8.5 on real machines.
6. Decide the OPEN maintainer decisions below.
7. 9.4 archive the change and record the verified Claude Code version. 9.3 public flip stays HUMAN.

Standing rules:

- Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`, before coding.
- Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## OPEN maintainer decisions

- A. Stale hosts' limits: the view takes limits per account from the snapshot with the greatest `captured_at` among fresh and stale hosts (`design.md`). Decide whether a stale host's limits should still count, or only fresh hosts'.
- B. Installer config: should `install` write `interval_s`, `hub` and `account` into the config (as `design.md` "Install flags" sketches with `--hub` and `--account`), or stay as it is (only `statusline_original`, with a warning when `--interval` differs from `interval_s`)? Related: should `install --apply` refuse when no `hub` is set (today the plan prints a warning and apply proceeds)?

## HUMAN-only tasks

- 2.2 capture a real `waiting` row
- 2.1 `CLAUDE_CONFIG_DIR` sub-check: set it to a temp dir, start a throwaway session, record yes/no in `docs/spikes.md`
- 2.8 reference-machine cold/warm collect timing
- 4.5 view check in a real terminal: colours and live resize (text-capture layout check at 120x40 and 80x24 is done, see `docs/spikes.md`)
- Group 5 tracer-bullet checkpoint (two real machines)
- 7.2 real fresh-session observation of missing `rate_limits`
- 8.1 timers-across-sleep spike (macOS and Linux)
- 8.5 real `install` / `install --apply` / `--statusline`: `install --apply` and `install --statusline` need a TTY, so the maintainer runs them from a real terminal on one real machine (an agent cannot). Then confirm the timer fires, the hub shows the host, and the original statusLine output is unchanged
- 9.2 the-grid docs pointer: separate change on its own branch in https://github.com/oneafrikan/the-grid, HUMAN-reviewed; do not touch its in-flight worktrees
- 9.3 public-readiness gate: grep the tree for machine names, IPs, usernames, home paths, account names. Allowlist: the GitHub handle `oneafrikan` (README/TODO/tasks links) and the copyright holder name in `LICENSE`. Every other machine name, IP, username, home path or account name must have zero hits.

## Known limits / follow-ups (1-16 from QA of groups 1-4; none blocking)

1. A stored snapshot's `host` is not re-checked against config after a `host_name` rename within `interval_s`; the old `host` is delivered under the new filename (the view uses the filename, so no visible effect).
2. A snapshot's `interval_s` is unbounded; a host claiming a huge interval never goes stale (spec says take it from the snapshot; consider clamping in the hub reader).
3. The remote tmp file `.<host>.json.tmp` is shared between concurrent pushes (timer plus manual run); consider a per-process suffix.
4. The hub inbox path is derived from `XDG_STATE_HOME`/`$HOME` in the ssh non-interactive shell, while `view` uses `GRID_FLEET_STATE_DIR`/interactive XDG; push and view can disagree if the hub sets them differently.
5. `validate_hub` rejects a leading `-`, whitespace and control characters only; shell metacharacters pass (harmless in grid-fleet, no shell is involved, but a user `ProxyCommand` using `%h` could expand them).
6. `tests/test_guard.py` scans `grid_fleet/` only, not `bin/grid-fleet`, and checks only `match`, `X | Y` annotations and imports.
7. The upload completeness check catches truncation, not every short stream that ends in a newline (tool-written snapshots are one line plus a newline, so the known gap cannot occur for them).
8. `claude` is not on the non-interactive ssh PATH on hosts that install it in `~/.local/bin`, so `sessions` is null with warning `claude agents unavailable`. Timers will hit the same thing. The Group 8 installer (8.2) records the install-time PATH in the unit and has tests; verify on a real machine at 8.5. Config has no `claude` path key; only `GRID_FLEET_CLAUDE_BIN` or PATH can fix it.
9. Add the verified Claude Code version to the tested list at 9.4 (the list holds 2.1.305; other versions warn).
10. Onboard the two remaining machines as spokes. Per-host config lives outside the repo on each machine by design; do not record host details here.
11. Security review gate before going public: run `grid-security-reviewer` on push and install (host-name charset is a security boundary). Currently only in the `CLAUDE.md` agent table, not a task.
12. QA re-gate (`grid-qa-engineer`) after Groups 6-8.
13. 8.3 `install --statusline` depended on the Group 7 tap (7.3), so Group 8 was split: timers first, statusLine after Group 7. Both are built now.
14. A bad `GRID_FLEET_NOW` makes `collect` print a traceback (`ConfigError` uncaught); decide whether to handle it.
15. One test is skipped per run, by design: the guard tests skip on the Python version that cannot exercise them (`test_match_detected`: "match is a SyntaxError on 3.9"; `test_match_unparseable_on_39_is_reported`: "parses on this Python" on 3.10+). Seen on 3.9 and 3.14; 3.12 not run here.
16. Without an installed timer, every host goes grey `unknown` about 2 x interval after its last manual push. Expected, not a bug. The timers are built (8.2) but not yet verified across sleep on real machines (8.1, 8.5).
17. A restricted receiver (`grid-fleet receive --host NAME`) could back per-spoke forced-command ssh keys, so a spoke can only write its own row. Decided: not in v1; the README Security model documents the trust assumption instead.
18. Custom agent type names are sent to the hub as written. Option: filter custom types to `custom`. Decided: document it (README Privacy), do not filter.
19. A subagent whose meta file says `agentType` `main` or `unknown` merges into the `main` or `unknown` leaderboard bucket.
20. Hub account labels are cut to 32 ASCII characters, so near-identical long labels can merge into one limits block.
21. No warning when `limits.json` is corrupt or unreadable: `limits` is `null` and the view shows `unknown`. Consider a `limits file unreadable` warning.
22. Limits carry one `captured_at` for both windows, so the capture age can understate the age of a window that was absent from the latest payload. Consider per-window capture times if this matters.
23. The `.claude/` OpenSpec scaffold exists in git history (commit "openspec init"). Kept (maintainer decision); it is gitignored and untracked now.
24. `settings.json` backups from `install --statusline` and `--uninstall` accumulate unpruned next to the file. They can hold secrets (README Security model).
25. The tap applies no timeout to the original statusLine command, by design: a hung original hangs the statusLine exactly as it would without the tap.

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
