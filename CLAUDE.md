# CLAUDE.md

## What this is

`grid-fleet`: terminal dashboard (Python, stdlib only) for Claude Code sessions, tokens,
subagent usage and Max-plan limits across a fleet of machines. Spec phase: no code yet.

## Hard constraints

- Python stdlib only, zero dependencies. Must run on stock macOS Python 3.9.6.
  - No `match`, no `X | Y` type unions at runtime, no `tomllib`. Config is JSON.
  - Verify with `/usr/bin/python3` (macOS system 3.9), not the Homebrew `python3` on PATH.
- Runs on macOS and Linux. OS-specific code is isolated in the installer module.
- Claude only in v1. Snapshot carries `agent: "claude"`. No plugin system.
- No HTML in v1. Terminal (curses) only.
- Never modify `~/.claude/settings.json` outside the `install` flow, and only after
  showing the exact diff and getting confirmation.

## Hygiene

- The repo is destined to be public. NO machine names, IPs, account names, usernames or
  home paths in any tracked file. Test fixtures are synthetic.
- Per-user rollout (which hub, which hosts) lives outside this repo.
- Privacy default: cwd, session names and project names are redacted from snapshots.
  Opt-in only, via config.
- Collector never prints or stores transcript text. It reads `usage` and `timestamp` only.
- Comment code generously: intent, not syntax.

## OpenSpec

- Specs live in `openspec/`. Current work: `openspec/changes/fleet-tracer-bullet/`.
- `openspec list`, `openspec show <change>`, `openspec validate <change>`.
- No non-trivial code without a spec delta. If implementation contradicts the spec, update the delta.
- `openspec/specs/` changes only via archive.

## Related repos

- the-grid (`~/.the-grid`, public): broader toolkit. Read-only from here. The pointer from the-grid
  to grid-fleet is a task in the change, done later on its own branch there.

## Git

- Local only until the tracer bullet works. No remote, no push yet.
- Commit style: `type(scope): subject` (feat, fix, docs, spec, chore).
