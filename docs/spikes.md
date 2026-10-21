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
- `CLAUDE_CONFIG_DIR` relocation check: pending HUMAN (not attempted by the agent).

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
