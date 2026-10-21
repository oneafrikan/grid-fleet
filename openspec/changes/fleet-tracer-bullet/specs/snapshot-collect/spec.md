## Purpose

Produce one small, privacy-safe JSON snapshot per host describing Claude Code sessions, token usage, subagent usage and account limits, cheaply enough to run every few minutes.

## ADDED Requirements

### Requirement: Snapshot content
`grid-fleet collect` SHALL write one JSON snapshot containing `schema`, `agent` (`"claude"`), `agent_version`, `host`, `account`, `collected_at` (epoch seconds), `interval_s`, `sessions`, `tokens`, `agents`, `limits` and `warnings`. A section that could not be collected MUST be `null`, never zero.

#### Scenario: Full snapshot
- **WHEN** collect runs on a host with active sessions, transcripts and a captured limits file
- **THEN** every section is populated and `agent` is `"claude"`

#### Scenario: Section unavailable
- **WHEN** the Claude projects directory does not exist
- **THEN** `tokens` and `agents` are `null`, a warning is recorded, and the exit code is 0

#### Scenario: Host name is safe for use as a file name
- **WHEN** the configured or detected host name contains characters outside `A-Za-z0-9._-` or starts with a dot
- **THEN** the snapshot `host` is sanitised to that charset and limited to 64 characters

### Requirement: Session counting
The collector SHALL count sessions from `claude agents --json`, including only rows with `kind` equal to `interactive` whose `pid` is a live process, and SHALL report counts of `busy`, `waiting`, `idle` and `other` plus `total`.

#### Scenario: Busy and waiting split
- **WHEN** the command returns two live interactive rows with status `busy` and one with status `waiting`
- **THEN** `sessions` is `busy: 2, waiting: 1, idle: 0, other: 0, total: 3`

#### Scenario: Stale background rows ignored
- **WHEN** the command returns a row with `kind` `background` or a `pid` that is not running
- **THEN** that row is not counted

#### Scenario: Unknown status
- **WHEN** a live interactive row has a status other than busy, waiting or idle
- **THEN** it is counted under `other`

#### Scenario: Command unavailable
- **WHEN** the `claude` binary is missing, exits non-zero, produces non-JSON output or takes longer than 10 seconds
- **THEN** `sessions` is `null` and a warning is recorded

### Requirement: Token totals
The collector SHALL sum `output`, `input`, `cache_creation` and `cache_read` tokens from `message.usage` in main and subagent transcripts, bucketed by event timestamp, and report rolling `last_24h`, rolling `last_7d` and seven `by_day` entries by UTC date. With hourly buckets, `last_24h` is the 24 whole UTC hour buckets ending with the current partial hour and `last_7d` is the 168 buckets ending the same way, so `last_7d` is not equal to the sum of `by_day` and an event exactly 24 hours old is outside `last_24h`.

#### Scenario: Window boundaries
- **WHEN** a usage event is 25 hours old
- **THEN** it is included in `last_7d` and excluded from `last_24h`

#### Scenario: Duplicate usage lines
- **WHEN** consecutive transcript lines carry the same `message.id`
- **THEN** that message's usage is counted once

#### Scenario: Subagent tokens included
- **WHEN** a session has subagent transcripts with usage in the window
- **THEN** their tokens are included in the totals

### Requirement: Subagent leaderboard
The collector SHALL report, for the last 7 days, per agent type the number of invocations and output tokens, where the type is `agentType` from the subagent's meta file and the main session is reported as type `main`. An invocation is one transcript with at least one usage event in the window.

#### Scenario: Main counts as an agent
- **WHEN** two main-session transcripts and one `core-researcher` subagent transcript have usage in the window
- **THEN** `agents.last_7d` contains `main` with 2 invocations and `core-researcher` with 1

#### Scenario: Sorted output
- **WHEN** several types are present
- **THEN** they are sorted by invocations descending

#### Scenario: Meta file missing
- **WHEN** a subagent transcript has no readable meta file
- **THEN** it is reported under type `unknown`

### Requirement: Incremental scan
The collector SHALL NOT re-read transcript bytes it has already summed. It SHALL keep a per-host offset cache, scan only transcripts modified within the last 8 days, process only complete lines, and recount a file from the start if it is smaller than its recorded offset. A warm collect run, excluding the `claude agents` call, MUST complete in under 2 seconds on the spike reference corpus (task 2.1 confirms or revises this figure).

#### Scenario: Warm run reads only new bytes
- **WHEN** collect runs twice and 1 KiB is appended to one transcript between runs
- **THEN** the second run reads only the appended bytes of that file and its totals equal a from-scratch recount

#### Scenario: Partial last line
- **WHEN** a transcript ends in an incomplete line
- **THEN** that line is not counted and is read in full on the next run once complete

#### Scenario: Truncated transcript
- **WHEN** a transcript is smaller than its recorded offset
- **THEN** its cache entry is dropped and it is recounted from the start with no double counting

#### Scenario: Cleaned-up log
- **WHEN** a transcript has been deleted but its usage is within the last 7 days
- **THEN** its previously summed usage still counts until it ages out of the window

#### Scenario: Cache lost
- **WHEN** the offset cache is missing or corrupt
- **THEN** collect rebuilds it from the transcripts within 8 days and produces correct totals

### Requirement: Fail soft and version awareness
The collector MUST NOT fail the run because of an unreadable file, an unparseable line, or an unrecognised layout. It SHALL record count-only warnings and, when the Claude Code version is not in the tested set, a version warning.

#### Scenario: Corrupt line
- **WHEN** a transcript contains a line that is not valid JSON
- **THEN** the line is skipped, a warning with the skipped count is recorded, and the run exits 0

#### Scenario: Untested version
- **WHEN** `claude --version` reports a version outside the tested set
- **THEN** collection still runs and `warnings` contains a version warning

### Requirement: Privacy by default
Snapshots, logs and warnings MUST NOT contain working directories, session names, project directory names, session ids, file paths or transcript text, whatever the configuration. There is no opt-in to include them. The collector SHALL read only `type`, `message.id`, `message.usage`, `timestamp` from transcript lines and `agentType` from meta files.

#### Scenario: No identifying strings
- **WHEN** collect runs, with any config, on a host whose sessions have names and working directories
- **THEN** no string in the snapshot equals or contains any of those names, directories, project directory names or session ids

### Requirement: Runtime constraints
All commands SHALL run on Python 3.9 using only the standard library, on macOS and Linux, and configuration SHALL be JSON.

#### Scenario: Stock macOS Python
- **WHEN** the test suite runs under `/usr/bin/python3` (3.9.6) on macOS
- **THEN** it passes with no third-party packages installed
