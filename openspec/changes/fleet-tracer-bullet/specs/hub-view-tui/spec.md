## Purpose

Show the whole fleet at a glance in a btop-style terminal dashboard on the hub, honest about stale data.

## ADDED Requirements

### Requirement: Read snapshots directly
`grid-fleet view` SHALL read `<host>.json` files from the local inbox and SHALL NOT open any network port or contact other machines.

#### Scenario: No network use
- **WHEN** view runs
- **THEN** it creates no listening socket and no outbound connection

#### Scenario: Auto refresh
- **WHEN** a new `<host>.json` lands in the inbox while view is running
- **THEN** the display reflects it within `refresh_s` seconds plus one redraw

### Requirement: Per-host sessions and tokens
The view SHALL show for each host the busy and waiting session counts and output tokens for 24 hours and 7 days as the headline, with input plus cache tokens on a separate muted line, and a total row.

#### Scenario: Fresh host
- **WHEN** a host snapshot is fresh with 2 busy and 1 waiting sessions
- **THEN** the host row shows busy 2 and waiting 1

#### Scenario: Output is the headline
- **WHEN** a snapshot has 400 thousand output tokens and 50 million cache-read tokens
- **THEN** the primary figure shown is 400k and the cache figure appears only on the muted secondary line

#### Scenario: Null section
- **WHEN** a host's `sessions` is `null`
- **THEN** its session cells show `unknown`, not 0

### Requirement: Staleness
A host is stale when the age of its inbox file, measured on the hub clock, exceeds twice the snapshot `interval_s`. Stale hosts SHALL render as grey `unknown` with their age, and SHALL be excluded from totals, with the footer stating how many hosts are fresh.

#### Scenario: Stale host
- **WHEN** a host's inbox file is 41 minutes old and its `interval_s` is 600
- **THEN** its row shows grey `unknown` and `stale 41m`, and its numbers are not in the total

#### Scenario: Never reported
- **WHEN** `expected_hosts` names a host with no inbox file
- **THEN** it renders `unknown (never reported)`

#### Scenario: Clock skew
- **WHEN** a spoke's clock is wrong by one hour
- **THEN** staleness is unaffected because it uses the hub's file mtime

### Requirement: Subagent leaderboard
The view SHALL show the top 8 agent types by invocations over 7 days, summed across fresh hosts, with `main` included.

#### Scenario: Cross-host sum
- **WHEN** two fresh hosts each report `core-researcher` with 5 invocations
- **THEN** the leaderboard shows 10

### Requirement: Limits shown once per account
The view SHALL show the 5-hour and 7-day limit bars once per `account`, taken per window from the snapshot with the greatest `limits.captured_at`, annotated with the capture age. A window whose `resets_at` has passed MUST render as `unknown`. An absent window MUST render as `unknown`, not 0 percent.

#### Scenario: Three hosts, one account
- **WHEN** three fresh hosts share account `default` and report different `captured_at`
- **THEN** exactly one 5h bar and one 7d bar are shown, from the latest capture

#### Scenario: Window expired
- **WHEN** the stored `five_hour.resets_at` is in the past
- **THEN** the 5h bar renders `unknown`

#### Scenario: No limits anywhere
- **WHEN** no snapshot has `limits`
- **THEN** the limits strip shows `unknown` for both windows

#### Scenario: Two accounts
- **WHEN** snapshots carry two distinct `account` labels
- **THEN** a limits block is shown for each

### Requirement: Bad input tolerated
The view SHALL ignore dotfiles, files larger than 256 KiB, invalid JSON and snapshots with an unknown `schema`, and SHALL report the number skipped in the footer.

#### Scenario: Corrupt file
- **WHEN** the inbox holds one valid and one corrupt file
- **THEN** the valid host renders and the footer shows `1 file skipped`

### Requirement: Terminal behaviour
The view SHALL redraw on terminal resize, show a one-line message below 60 columns by 16 rows, honour `NO_COLOR`, fall back to ASCII glyphs when the locale is not UTF-8 or `view.ascii` is true, quit on `q` or Ctrl-C leaving the terminal restored, and refresh immediately on `r`.

#### Scenario: Too small
- **WHEN** the terminal is 50 columns wide
- **THEN** a "terminal too small" message is shown instead of a broken layout

#### Scenario: Exit restores terminal
- **WHEN** the user presses `q`
- **THEN** the terminal is returned to its normal mode with the cursor visible

#### Scenario: No colour
- **WHEN** `NO_COLOR` is set
- **THEN** no colour attributes are used and meaning is still conveyed by text

### Requirement: Read-only apart from history
The view SHALL write only history files and their lock/ingest state.

#### Scenario: Inbox untouched
- **WHEN** view runs for a minute and exits
- **THEN** inbox files are byte-identical and mtimes unchanged
