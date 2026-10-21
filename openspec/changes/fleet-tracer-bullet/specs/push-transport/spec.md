## Purpose

Deliver each host's latest snapshot to a hub the user chooses, over plain ssh, with no hub-side service beyond `sshd`.

## ADDED Requirements

### Requirement: Push over ssh
`grid-fleet push` SHALL send the latest snapshot to the hub identified by the `hub` config value, which is an ssh host alias, by running `ssh` with the snapshot on stdin so that it appears in the hub's inbox as `<host>.json`. The hub MUST need nothing installed beyond `sshd` and a POSIX shell.

#### Scenario: Successful push
- **WHEN** push runs with `hub` set to a reachable alias
- **THEN** the hub inbox contains `<host>.json` equal to the pushed snapshot and push exits 0

#### Scenario: Non-interactive ssh
- **WHEN** push runs
- **THEN** ssh is invoked with `BatchMode=yes` and `ConnectTimeout=10` so it can never prompt or hang

#### Scenario: Hub unreachable
- **WHEN** ssh fails
- **THEN** push exits non-zero, keeps the local snapshot, logs a one-line reason, and does not retry in a loop

#### Scenario: Hub not configured
- **WHEN** `hub` is absent from config
- **THEN** push exits non-zero with a message naming the missing key and sends nothing

### Requirement: Atomic delivery
The hub-side write MUST be atomic: a reader never observes a partial `<host>.json`.

#### Scenario: Interrupted upload
- **WHEN** the connection drops mid-transfer
- **THEN** the previous `<host>.json` is unchanged and any partial data exists only in a dot-prefixed temporary file

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
Push MUST refuse to run if the host name contains characters outside `A-Za-z0-9._-` or starts with a dot, because it is used in a remote file name and command.

#### Scenario: Malicious host name
- **WHEN** config sets `host_name` to `../x; rm -rf ~`
- **THEN** the name is sanitised before use and nothing outside the inbox directory is written

### Requirement: No credentials in the tool
grid-fleet SHALL rely on the user's own ssh configuration and agent and MUST NOT read, store or transmit keys, passwords or tokens.

#### Scenario: Config contents
- **WHEN** the config file and all state files are inspected after normal use
- **THEN** none contains a key, password or token
