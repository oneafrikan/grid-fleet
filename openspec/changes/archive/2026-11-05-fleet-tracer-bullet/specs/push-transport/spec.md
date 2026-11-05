## Purpose

Deliver each host's latest snapshot to a hub the user chooses, over plain ssh, with no hub-side service beyond `sshd`.

## ADDED Requirements

### Requirement: Push over ssh
`grid-fleet push` SHALL send the latest snapshot to the hub identified by the `hub` config value, which is an ssh host alias, by running `ssh` with the snapshot on stdin so that it appears in the hub's inbox as `<host>.json`. The hub MUST need nothing installed beyond `sshd` and a POSIX shell. A `hub` value MUST be at most 255 characters; a longer one is rejected like the other invalid values. A `hub` value that is empty, starts with `-`, or contains anything outside letters, digits and `. _ @ : -` (so no whitespace, control characters or shell/ssh metacharacters) MUST be rejected, so it can never be parsed as an ssh option. The alias SHALL also follow a `--` argument so ssh cannot read it as an option, and ssh SHALL run with `ClearAllForwardings=yes`.

#### Scenario: Successful push
- **WHEN** push runs with `hub` set to a reachable alias
- **THEN** the hub inbox contains `<host>.json` equal to the pushed snapshot and push exits 0

#### Scenario: Non-interactive ssh
- **WHEN** push runs
- **THEN** ssh is invoked with `BatchMode=yes` and `ConnectTimeout=10` so it can never prompt or hang

#### Scenario: Hub unreachable
- **WHEN** ssh fails
- **THEN** push exits non-zero, keeps the local snapshot, logs a one-line reason, and retries only as the "Transient ssh failure is retried" requirement allows (never an unbounded loop)
- **AND** the logged line contains only the reason class and exit code and MUST NOT echo ssh's own output, which may name the hub

#### Scenario: Hostile hub alias
- **WHEN** `hub` is `-oProxyCommand=x`
- **THEN** push exits non-zero and runs nothing

#### Scenario: Hub not configured
- **WHEN** `hub` is absent from config
- **THEN** push exits non-zero with a message naming the missing key and sends nothing

### Requirement: Transient ssh failure is retried
When an ssh delivery fails at the transport level, push SHALL retry it within the same run, up to 3 attempts in total: it waits 5 seconds before the second attempt and 15 seconds before the third. A transport failure is ssh exiting 255 or the per-attempt timeout backstop firing. Every attempt MUST send the same snapshot bytes. Push MUST NOT retry any other failure: a remote command that ran and failed (any other non-zero ssh exit, such as the hub refusing an incomplete stream), a rejected `hub` value, ssh not being runnable, or `hub` set to `"local"`. Push SHALL still print exactly one line, free of the hub name and ssh output, and the exit codes stay as before. The worst case for one run is bounded: 3 times the per-attempt timeout plus the two waits.

#### Scenario: Retry succeeds
- **WHEN** the first ssh attempt exits 255 and the second succeeds
- **THEN** push waits 5 seconds between them, sends the same bytes both times, exits 0, the hub inbox holds the complete `<host>.json`, and the one line reads `push: delivered (ssh, attempt 2 of 3)`
- **AND** a push that succeeds on its first attempt still reads `push: delivered (ssh)` and does not wait

#### Scenario: Retries exhausted
- **WHEN** all 3 attempts fail at the transport level
- **THEN** push waits 5 seconds then 15 seconds, exits with the same non-zero code as a single failure, keeps the local snapshot, and logs the one line `push failed (ssh exit 255) after 3 attempts; local snapshot kept` (a timeout reads `timeout after <n>s` in place of `ssh exit 255`)

#### Scenario: Timeout is retried
- **WHEN** an ssh attempt hits the timeout backstop and the next attempt succeeds
- **THEN** the push is retried like an exit 255 and succeeds

#### Scenario: Remote failure is not retried
- **WHEN** ssh exits with any non-zero status other than 255
- **THEN** push makes exactly one attempt, does not wait, and logs `push failed (ssh exit <n>); local snapshot kept`

#### Scenario: Config errors and the local hub are not retried
- **WHEN** `hub` is rejected as invalid, or is `"local"`
- **THEN** push never retries and never waits

#### Scenario: Interrupted attempt then clean attempt
- **WHEN** the connection drops mid-transfer on the first attempt and the second attempt completes
- **THEN** the hub's `<host>.json` is the complete snapshot and no temporary file is left in the inbox

### Requirement: Atomic delivery
The hub-side write MUST be atomic: a reader never observes a partial `<host>.json`. The hub MUST NOT rename a temporary file that does not end in a newline.

#### Scenario: Interrupted upload
- **WHEN** the connection drops mid-transfer
- **THEN** the previous `<host>.json` is unchanged and any partial data exists only in a dot-prefixed temporary file

#### Scenario: Connection drops after partial bytes
- **WHEN** the client connection drops after partial bytes and the hub sees end of input
- **THEN** the previous `<host>.json` is unchanged and the partial data exists only in a dot-prefixed temporary file

### Requirement: Local hub
When `hub` is `"local"`, push SHALL write into the local inbox using the same atomic rename and SHALL NOT invoke ssh.

#### Scenario: Hub is this machine
- **WHEN** `hub` is `"local"`
- **THEN** `<host>.json` appears in the local inbox and no ssh process is started

### Requirement: Fresh data only
Push SHALL NOT deliver a snapshot whose `collected_at` is older than `interval_s`; it SHALL collect a fresh one first.

#### Scenario: Old local snapshot
- **WHEN** the stored snapshot is older than `interval_s` and push runs alone
- **THEN** a new snapshot is collected and that is what is delivered

### Requirement: Host name safety
Push MUST sanitise the host name to `A-Za-z0-9._-` (max 64 characters, no leading dot) before use, because it is used in a remote file name and command. It does not refuse to run.

#### Scenario: Malicious host name
- **WHEN** config sets `host_name` to `../x; rm -rf ~`
- **THEN** the name is sanitised before use and nothing outside the inbox directory is written

### Requirement: No credentials in the tool
grid-fleet SHALL rely on the user's own ssh configuration and agent and MUST NOT read, store or transmit keys, passwords or tokens.

#### Scenario: Config contents
- **WHEN** the config file and all state files are inspected after normal use
- **THEN** none contains a key, password or token
