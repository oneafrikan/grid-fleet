# grid-fleet

A btop-style terminal dashboard for your Claude Code sessions across all your machines.

**Status: pre-alpha, spec approved, build not started.** Nothing is implemented yet.
The plan is in [`openspec/changes/fleet-tracer-bullet/`](openspec/changes/fleet-tracer-bullet/).

## What and why

Running Claude Code on several machines gives no single view of the fleet. grid-fleet shows, in one terminal:

- Sessions running on each machine, split busy / waiting.
- Token usage: output tokens as the headline, input and cache tokens as a muted second line.
- Which subagent types do the work over the last 7 days (the main session counts as an agent).
- Your Max-plan 5-hour and 7-day limits, shown once per account.

A host that has not reported recently shows as grey `unknown`, never as zero. It shows "now" only: no history is stored in v1.

## Planned commands

```
grid-fleet collect    # write a snapshot of this machine
grid-fleet push       # send the latest snapshot to the hub
grid-fleet view       # the dashboard (run on the hub)
grid-fleet install    # print / apply the timer and statusline setup
```

## Architecture

- Every machine ("spoke") runs `collect` then `push` on a 5, 10 or 15 minute timer.
- `collect` writes one snapshot JSON, summing token usage incrementally from local Claude logs.
- `push` sends it over plain `ssh` (stdin to a small POSIX sh script) to a hub, identified by an ssh host alias.
- The hub needs only `sshd`: no server, no database, no open port. Snapshots land atomically in an inbox.
- `view` runs on the hub, reads the inbox and draws the dashboard with curses.
- Limits come from an optional statusLine wrapper that chains to your existing statusLine and records `rate_limits`.

Details: [`design.md`](openspec/changes/fleet-tracer-bullet/design.md), [`tasks.md`](openspec/changes/fleet-tracer-bullet/tasks.md).

## Requirements

- Python 3.9 or newer, standard library only. Stock macOS Python works.
- macOS or Linux.
- Claude Code (the only supported agent in v1).

## Privacy

- Snapshots contain counts and token totals only.
- Working directories, session names, project directory names and session ids never appear in a snapshot, whatever the config.
- Transcript text is never stored or logged.
- Nothing leaves your machines except to the hub you configure.

## Related

- [the-grid](https://github.com/oneafrikan/the-grid): a related agent toolkit. grid-fleet is independent of it and does not depend on it.

## Licence

MIT. See `LICENSE`.
