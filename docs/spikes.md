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
