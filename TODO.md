# TODO

## State

- [x] Scaffold repo, OpenSpec init
- [x] Change `fleet-tracer-bullet` drafted and approved (proposal, design, specs, tasks)
- [ ] Build (starts 2026-10-22)

## Next steps (in order)

1. Read `CLAUDE.md`, then `openspec/changes/fleet-tracer-bullet/{design,tasks}.md` and the five `specs/*/spec.md`.
2. Start at Group 1 in `tasks.md` (skeleton and CLI), then Groups 2, 3, 4.
3. Stop at Group 5 (HUMAN checkpoint) before starting Group 6.
4. Keep `docs/spikes.md` current with spike results (numbers only, no hostnames).

## HUMAN-only tasks

- 2.2 capture a real `waiting` row
- 2.8 reference-machine cold/warm collect timing
- 3.4 real collect + push to a real hub
- 4.5 view layout check at 80x24 and 120x40
- Group 5 tracer-bullet checkpoint (two real machines)
- 7.2 real fresh-session observation of missing `rate_limits`
- 8.1 timers-across-sleep spike (macOS and Linux)
- 8.5 real `install` / `install --apply` / `--statusline`
- 9.2 the-grid docs pointer: separate change on its own branch in https://github.com/oneafrikan/the-grid, HUMAN-reviewed; do not touch its in-flight worktrees
- 9.3 public-readiness gate: grep the tree for machine names, IPs, usernames, home paths, account names; pass is zero hits

## Remote

- There is NO git remote yet.
- When decided, create `oneafrikan/grid-fleet` (MIT). Private first is acceptable; flip to public only after the 9.3 grep passes (and groups 1-5 pass).

## Future decisions: outcomes roster (v2 idea, undecided)

An "outcomes roster": not a task list, a team-roster-style view of what is being built and where (outcome, repo@branch/worktree, machine, active sessions, state). Leaning: a v2 view over the same snapshots, with opt-in repo/branch fields (changes the privacy contract). Not in `fleet-tracer-bullet`.

Questions to answer before specifying it:

1. Is an "outcome" an OpenSpec change, a branch, or a free-text goal you type?
2. Should it be auto-derived, hand-declared, or the hybrid?
3. Should it be a tab in grid-fleet's TUI, or its own tool?
4. Should it wait until grid-fleet's tracer bullet is done?
