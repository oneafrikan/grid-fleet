# grid-fleet

A btop-style terminal dashboard for your Claude Code sessions across all your machines.

**Status: pre-alpha, spec phase.** Nothing is implemented yet. The first change,
`openspec/changes/fleet-tracer-bullet/`, defines the tracer bullet.

## What it shows

- How many Claude Code sessions are running on each machine, split busy / waiting.
- Token usage: output tokens as the headline, input and cache tokens as a muted second line.
- Which subagent types are used most over the last 7 days (the main session counts as an agent).
- Your Max-plan 5-hour and 7-day limits, shown once per account.

A host that has not reported recently shows as grey "unknown", never as zero.

## How it works

- Every machine ("spoke") runs a collector on a 5, 10 or 15 minute timer.
- The collector writes one snapshot JSON and pushes it over plain `ssh` to a hub you choose.
- The hub needs only `sshd`. No server, no database, no open port.
- On the hub, `grid-fleet view` reads the snapshots and draws the dashboard.

## Planned commands

```
grid-fleet collect    # write a snapshot of this machine
grid-fleet push       # send the latest snapshot to the hub
grid-fleet view       # the dashboard (run on the hub)
grid-fleet install    # print / apply the timer and statusline setup
```

## Requirements

- Python 3.9 or newer, standard library only. Stock macOS Python works.
- macOS or Linux.
- Claude Code (the only supported agent in v1).

## Privacy

- Snapshots contain counts and token totals only.
- Working directories, session names and project names are left out unless you opt in.
- Nothing leaves your machines except to the hub you configure.

## Related

- [the-grid](https://github.com/oneafrikan/the-grid), the broader agent toolkit this grew out of. grid-fleet works without it.

## Licence

MIT. See `LICENSE`.
