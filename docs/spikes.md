# Spike results

Append-only. Numbers only: no paths, names, ids or transcript content. Each unit adds its
own section.

## U2 / Group 2 spikes

Run on the reference machine (macOS, Apple Silicon, stock `/usr/bin/python3` 3.9.6, run with
`-I`). Scripts were throwaway and are not in the repo.

### 2.1 Naive full scan of the projects dir (files modified in the last 8 days)

| Measure | Value |
|---|---|
| `*.jsonl` files, all ages | 488 |
| `*.jsonl` files modified in last 8 days (main + subagent) | 98 |
| Total size of those files | 130.5 MB |
| Largest single file | 10.1 MB |
| Directory walk + stat to list them | 0.08 s |
| Naive scan, first pass (read + `json.loads` every line) | 0.50 s |
| Naive scan, second pass (OS cache warm) | 0.34 s |
| Lines parsed | 33,761 (0 unparseable) |
| Hot-cache incremental floor (walk + stat all files, read nothing), 3 passes | 0.004 s each |

Caveats:

- Cold cache cannot be forced without sudo, so "first pass" is the first pass in a fresh
  process, not a guaranteed cold-disk read. The files had very likely been read recently.
  Treat 0.50 s as a best case for a cold read, not a measured cold read.
- Python's C `json` makes the naive scan cheap on this corpus. The offset cache is NOT
  justified by this corpus alone: a naive rescan is already under 1 s. It is justified by
  (a) the spec scenario that a warm run reads only appended bytes, (b) cost scaling with
  corpus size (linear in bytes for the naive scan, near-constant for the cache; a heavy
  user, slow disk or a genuinely cold cache will be many times slower), and (c) retention of
  usage after the 30-day log cleanup, which a rescan cannot give.

Verdict:

- PASS. Figures exist. The hot-cache incremental design is ~4 ms of stat work plus the bytes
  appended since the last run, far under the 5 s fail line.
- The 2 s warm budget in the spec is CONFIRMED (no revision proposed). Measured headroom is
  large (naive 0.34 to 0.50 s; incremental floor 0.004 s).
- `CLAUDE_CONFIG_DIR` relocation check: was pending at this point; done later, result YES (see "2.1 sub-check" below).

### 2.3 Duplicate usage lines: three counting rules on the three largest main sessions

Output-token sums. Rule (a) every line carrying `message.usage`; rule (b) consecutive lines
with the same `message.id` collapse, keep the last; rule (c) last per `message.id` globally.

| Session | Size | Usage lines | Lines with no id | Distinct ids | (a) every line | (b) consecutive collapse | (c) last per id | b = c |
|---|---|---|---|---|---|---|---|---|
| 1 | 10.2 MB | 590 | 0 | 293 | 823,286 | 210,908 | 210,908 | yes |
| 2 | 10.1 MB | 555 | 0 | 255 | 671,046 | 257,689 | 257,689 | yes |
| 3 | 7.3 MB | 497 | 0 | 211 | 834,277 | 286,422 | 286,422 | yes |

Verdict:

- PASS. Rule (b) equals rule (c) on all three. Keep rule (b), as `design.md` decided.
- Rule (a) overcounts by roughly 2.6x to 3.9x, so the collapse is required, not optional.
- Every usage line in the sample carried a `message.id`. Lines without an id are never
  collapsed by the implementation (nothing to match on).

### 2.8 Cold and warm `collect` on the real corpus (reference machine)

Run on the maintainer's macOS laptop, real Claude directory, at commit `47f418f`. Numbers only.

| Run | Wall time |
|---|---|
| cold (empty offsets cache; OS file cache probably warm) | 1.20 s |
| warm, run 1 | 0.16 s |
| warm, run 2 | 0.16 s |

Verdict:

- PASS. Warm 0.16 s against the 2 s budget.
- "Cold" here means the offsets cache was empty, not that the OS file cache was cold (it cannot be
  forced without sudo, same caveat as 2.1).

### 2.1 sub-check: does `CLAUDE_CONFIG_DIR` relocate the projects dir?

- Method: a throwaway `claude -p` with `CLAUDE_CONFIG_DIR` set to an empty temp dir.
- Result: YES. A transcript `projects/<dir>/<uuid>.jsonl` was written under the temp config dir.
- The session reported "Not logged in", so no API call was made and no credentials were copied.
  The temp dirs were removed afterwards.
- Claude Code version observed on that laptop: 2.1.318. The code's tested list holds 2.1.305;
  task 9.4 records the final version.

## Fleet bring-up (real hosts)

Run by Claude Code at the human's request on 2026-10-21, at commit `7a17dc6`. Numbers and facts
only; no transcript content.

Topology:

- 1 hub (Ubuntu 24.04, always on, hub config `local`) and 3 spokes: a macOS arm64 laptop
  (stock Python 3.9.6), an Ubuntu 24.04 workstation, and a second macOS arm64 laptop.
- Spokes push over ssh through a Cloudflare-tunnel mesh (`ProxyCommand cloudflared access ssh`,
  `BatchMode`, `ConnectTimeout=10`).
- Each spoke cloned the repo at `7a17dc6` from the private remote, ran the test suite, wrote a
  config outside the repo, ran `collect` twice and `push` once to the hub.
- All three pushes exited 0. The hub inbox listed all four `<host>.json` files at mode 0600:
  1001 to 1149 bytes (spokes), 1045 bytes (hub).

Test suite at `7a17dc6`: 253 tests OK, 1 skip, on every platform below. The skip was not
investigated on 3.12.

| Platform | Python | Wall time |
|---|---|---|
| macOS arm64 laptop (clone test) | 3.9.6 (stock) | about 27.6 s |
| macOS arm64 (build machine, earlier) | 3.9.6 (stock) | about 28 s |
| Ubuntu 24.04 hub | 3.12.3 | 6.5 s |
| Ubuntu 24.04 workstation | 3.12.3 | 6.8 s |

Timings (Ubuntu hub, small corpus: 24h output 35,664 tokens, 7d output 104,351):

| Step | Time |
|---|---|
| `collect`, first run | 0.081 s |
| `collect`, second (warm) run | 0.033 s |
| local `push` (hub to itself) | 0.035 s |
| spoke `push` to hub over the mesh (work laptop) | 0.56 s wall |

Earlier throwaway test pushes to two Ubuntu machines took 1.5 s and 2.0 s wall (one 1122-byte file
each). One was byte-identical to the local snapshot; the other was structure-validated. The test
files were removed afterwards.

Throwaway real-ssh transport test (earlier the same day):

- `BatchMode` ssh through the mesh works non-interactively.
- The hub-side upload script, including the newline check, ran under both real non-interactive
  shells.
- Cleanup was verified.

`view` on the hub, rendered in a detached tmux session and captured as text:

- Fits and reads correctly at 120x40 and 80x24 with 6 hosts listed: 4 fresh, and 2 expected hosts
  that never reported, rendered as `unknown (never reported)`.
- Footer showed `4 of 6 hosts fresh`.
- Totals matched a hand sum: 24h output about 1.7M, 7d output about 4.0M, 2 busy.
- `q` exits cleanly.
- NOT judged: colours and real-terminal resize (text capture only).

Findings:

1. On the two Ubuntu hosts `claude` is installed in `~/.local/bin`, which is not on the
   non-interactive ssh PATH. With the default PATH, `claude agents --json` is unavailable, so
   `sessions` is null with warning `claude agents unavailable`. With `~/.local/bin` on PATH it
   works. Timers (launchd, systemd) will hit the same thing. The Group 8 installer MUST record the
   install-time PATH (`design.md` already says so). Config has no `claude` path key; only
   `GRID_FLEET_CLAUDE_BIN` or PATH can fix it.
2. The remaining warning on every host is `claude 2.1.306 not in tested versions` (the tested list
   holds 2.1.305). Expected behaviour. Adding the version to the tested list is task 9.4 and needs
   the human.
3. No timers exist yet (Group 8), so all pushes were manual.

### Fleet bring-up, second round (two more machines)

Two more machines were onboarded as spokes later, so six hosts now report to the hub.

| Platform | Python | Suite result |
|---|---|---|
| Arch-based Linux | 3.14 | 253 tests OK, 1 skip (at the commit that machine was on) |
| Ubuntu-based Linux server | 3.12 | 253 tests OK, 1 skip (at the commit that machine was on) |

- Each ran `collect` and `push`; both deliveries reached the hub. All six hosts now have an inbox file.
- The server could not read the private repo with its own GitHub identity, so it received a
  `git archive` copy without `.git`. The repo is public now, so that host can use a plain clone.

Stale-versus-fresh observation:

- A host paused beyond 2 x its interval showed grey `unknown` and was left out of the totals
  (the observation behind the 5.1 sign-off).
- Consequence for limits: at 47f418f the hub built the limits strip from fresh hosts only. The
  maintainer decided to include stale hosts' limits; that change is merged (the strip now shows the
  newest capture per window per account, with its age). Totals, leaderboard and sparkline still
  exclude stale hosts.

## U7 / Group 7 spikes

Run on the reference machine (macOS, Apple Silicon) on 2026-11-04, at commit `77d8e53`. Prototype
tap and all scripts were throwaway and are not in the repo. The statusLine was a synthetic Python
script that exits 0 with no output unless two gating env vars are set, otherwise prints one line
and exits 3 (non-zero on purpose, to exercise exit-code passthrough). No real statusLine or
settings were read.

### 7.1 Tap overhead and chaining (50 timed invocations per cell, 5 warm-up, direct and tapped runs interleaved)

Stock `/usr/bin/python3` 3.9.6 (target), two full runs:

| Gating vars | Run | Direct p50 / p95 (ms) | Tapped p50 / p95 (ms) | Added p50 (ms) | Added p95 (ms, p95 of tapped minus p95 of direct) | stdout and exit code byte-identical |
|---|---|---|---|---|---|---|
| unset (script exits early, rc 0) | 1 | 19.1 / 24.5 | 45.4 / 50.8 | 25.3 | 26.3 | yes |
| unset | 2 | 18.1 / 19.1 | 43.0 / 45.5 | 24.9 | 26.3 | yes |
| set (prints line, rc 3) | 1 | 19.5 / 21.8 | 45.7 / 51.3 | 26.2 | 29.4 | yes |
| set | 2 | 19.3 / 22.8 | 46.0 / 51.9 | 26.0 | 29.1 | yes |

Homebrew `python3` on PATH (3.14.6), one run, tap and original both under it:

| Gating vars | Direct p50 / p95 (ms) | Tapped p50 / p95 (ms) | Added p50 (ms) | Added p95 (ms) | Identical |
|---|---|---|---|---|---|
| unset | 16.7 / 17.5 | 42.2 / 45.3 | 25.4 | 27.8 | yes |
| set | 17.1 / 22.1 | 43.2 / 51.2 | 25.7 | 29.1 | yes |

Notes:

- Worst added p95 across all cells: 29.4 ms, under the 50 ms bound. Margin is about 20 ms.
- The added cost is one extra interpreter start plus one subprocess spawn. It does not scale with
  the original's own cost.
- Here the original was itself a Python script. A shell original would not change the tap's
  own added cost (the tap is the same code path), but absolute tapped totals would be lower.
- Full environment passed through unchanged (the gating vars reached the original in the set case).
- 3.9 and 3.14 differ by under 3 ms added p95. Timings varied run to run by up to about 6 ms at p95.

Verdict:

- PASS. p95 added latency under 50 ms in both env cases and output identical in both. The 50 ms
  bound in the spec is CONFIRMED. Shell-wrapper fallback not triggered.

### 7.1b Tap overhead of the real tap, before and after the fast path (QA finding F1)

Same method as 7.1 (50 timed invocations per cell, 5 warm-up, direct and tapped interleaved,
synthetic gated original, gating env var unset or set), but timing the real `bin/grid-fleet tap`.
Before = commit `a3564c0` (tap reached through `cli` and `config`). After = commit `48b2b9c` (tap
dispatched before `cli`, no argparse/dataclasses/typing/tempfile/subprocess import).
"Load" = one busy-loop process per core running during the cell. Added p50 = tapped p50 minus
direct p50; added p95 = tapped p95 minus direct p95. All values in ms. Other work was running on
the machine, so absolute values vary a few ms run to run.

`/usr/bin/python3` 3.9.6 (target), two interleaved full runs:

| Load | Gating vars | Run | Before added p50 / p95 | After added p50 / p95 |
|---|---|---|---|---|
| idle | unset | 1 | 40.7 / 42.2 | 23.7 / 27.3 |
| idle | unset | 2 | 40.7 / 42.8 | 22.1 / 30.7 |
| idle | set | 1 | 42.5 / 54.7 | 22.6 / 23.8 |
| idle | set | 2 | 40.2 / 42.7 | 21.9 / 22.9 |
| load | unset | 1 | 58.5 / 65.7 | 40.5 / 40.8 |
| load | unset | 2 | 60.4 / 63.3 | 39.4 / 43.0 |
| load | set | 1 | 60.8 / 64.4 | 40.4 / 42.0 |
| load | set | 2 | 59.7 / 66.5 | 39.1 / 43.3 |

Homebrew `python3` 3.14.6, one run:

| Load | Gating vars | Before added p50 / p95 | After added p50 / p95 |
|---|---|---|---|
| idle | unset | 43.3 / 51.2 | 20.6 / 24.5 |
| idle | set | 42.0 / 51.8 | 19.1 / 20.4 |
| load | unset | 60.1 / 68.7 | 31.7 / 33.0 |
| load | set | 56.9 / 64.1 | 32.7 / 34.3 |

Interpreter start-up alone, `python -c <import>`, `/usr/bin/python3`, 60 rounds, median in ms:

| Imports | p50 | p95 |
|---|---|---|
| none | 17.2 | 20.5 |
| `json` | 21.9 | 25.5 |
| `subprocess` | 23.2 | 30.0 |
| `json`, `subprocess` | 25.4 | 31.2 |

### 7.2 Missing limits, synthetic payloads (prototype tap, `/usr/bin/python3` 3.9.6)

| Case | Prior file | Resulting file state | Maps to spec |
|---|---|---|---|
| No `rate_limits` | none | no file created (state dir stays empty) | yes (Fresh session) |
| No `rate_limits` | both windows | byte-identical to before (hash match) | yes (file unchanged) |
| `rate_limits` is `{}` | none | no file created | spec silent, same outcome as above |
| Only `five_hour` | none | `captured_at` + `five_hour`; no `seven_day` | yes |
| Only `seven_day` | none | `captured_at` + `seven_day`; no `five_hour` | yes |
| Both | none | `captured_at` + both windows; mode 0600 | yes (Both windows present) |
| Only `five_hour` | `seven_day` resets_at in future | `seven_day` kept with previous values | yes (partial merge) |
| Only `five_hour` | `seven_day` resets_at in past | `seven_day` omitted | yes (partial merge) |
| Only `seven_day` | `five_hour` resets_at in future | `five_hour` kept with previous values | yes (partial merge) |

Other spec scenarios exercised on the prototype:

- Extra payload fields (cwd, session id, transcript path, model, an unknown field inside a window)
  were not present in the written file: yes.
- File mode 0600: yes in every written case.
- No original command: stdout empty, exit 0, slice still written.
- Limits write fails (state path unusable): original stdout and exit code (3) still returned.
- 30 simultaneous taps: final file valid JSON, no leftover temp files.

Observation (not a contradiction, flag for 7.3):

- On a partial merge the single file-level `captured_at` is set to now, so a carried-forward window
  looks as fresh as the one just captured. The spec has one `captured_at`, so per-window age
  cannot be recovered. The view's `as of <age>` for that window would then understate its age.

Result:

- Synthetic cases: all map to the spec. No spec delta needed from these.
- Real fresh-session observation: done 2026-11-05, see "7.2 real observation" below.

## Real-machine results, 2026-11-05 (tasks 7.2, 8.5)

Observed by the maintainer and the orchestrator on real machines. Numbers and facts only; no
hostnames, paths or payload content beyond the limits fields below.

### 7.2 real observation (first real statusLine payload through the tap)

- Fields present: `rate_limits.seven_day` only, with `used_percentage` 13 and `resets_at` about
  5.6 days ahead of capture.
- Fields absent: `rate_limits.five_hour`. The first payload carried no 5-hour window.
- Units confirmed: `used_percentage` is 0-100; `resets_at` is epoch seconds.
- File result: `limits.json` was written with `seven_day` only (the partial path). A previously stored
  synthetic `five_hour` whose `resets_at` was in the past was dropped, not carried forward.
- View result: the 7d bar showed 13%; the 5h bar showed `unknown  no data`.
- Verdict: PASS. Matches the spec and the synthetic table above: each window can be independently
  absent. No spec delta.

### 8.5 single-machine install (macOS laptop)

- `install --hub <hub-alias> --interval 600`: plan reviewed, then `--apply` and `y`.
- launchd job loaded: `run interval = 600 seconds`, `last exit code = 0`.
- First tick delivered a snapshot to the hub. The tick log shows `collect:` then `push: delivered (ssh)`.
- Interpreter recorded in the job: `/Library/Developer/CommandLineTools/usr/bin/python3` (stock
  macOS Python 3.9, what `/usr/bin/python3` resolves to). Running the installer with Homebrew Python
  would have recorded a Homebrew path that can vanish on a version bump. Run `install` with
  `/usr/bin/python3` on macOS.
- `install --statusline`, run in a real terminal:
  - The exact before/after of `statusLine.command` was shown. The original was a long third-party
    agent-hook script (Orca, credited in the README).
  - The file layout was reproduced, so no rewrite note appeared. All other settings were unchanged.
  - The maintainer typed `y`. A timestamped backup capped at mode 0600 was written next to
    `settings.json`.
  - The tap now runs on every statusLine refresh. `limits.json` was seen updating at the statusLine
    refresh rate. No errors, and the maintainer saw no visible change.

### 8.5 fleet rollout (six machines)

- Timers installed on all six: four Linux systemd user timers, two macOS launchd jobs.
- Every host was fresh on the hub within about 10 minutes of its first tick. Ages at the check:
  52 to 220 s.
- View on the hub: `6 of 6 hosts fresh`, per-host rows, totals equal to the sum of the rows, the 7d
  limit bar, the sparkline and the subagent leaderboard.
- Linger enabled on the three always-on Linux servers and on the Linux laptop, so the timer runs while
  logged out. The installer prints the note and never runs it.

### Incident: `claude` not on the recorded PATH (sessions null)

- What happened: on one macOS host the timer was installed through a non-interactive ssh command.
  Its `PATH` did not include the directory holding `claude` (`~/.local/bin`). The installer records
  the `PATH` at install time, so every tick ran with that `PATH`.
- Symptom: each tick reported `sessions: null` (the view shows only `unknown`) because
  `claude agents --json` could not be run. Same cause as bring-up finding 1 above, now seen through
  a timer.
- Fix applied: reinstalled the timer with that directory on `PATH` for the install command.
- Follow-up: an install-time warning in plan/apply when `claude` is not on the recorded `PATH` is in
  progress in the code (not landed at `a38edcc`). TODO follow-up 8 (PATH recording) bit once.
  Separate idea, undecided: show a per-host reason for null `sessions` in the view.

### Still open (HUMAN)

- 8.1 timers across sleep and reboot (macOS laptop sleep 3+ minutes with a 60 s throwaway agent; Linux
  reboot with and without linger).
- 4.5: a screenshot of the hub view at 6 hosts (limits strip, sparkline, leaderboard) was reviewed from
  a real terminal and the layout looks right. Colours and live resize are not yet checked.
- 2.2 a real `waiting` row.
- Claude Code version observed on the laptop: 2.1.318 (for 9.4).
