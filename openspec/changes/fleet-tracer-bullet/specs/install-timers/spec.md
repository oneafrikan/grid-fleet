## Purpose

Set up the periodic collector and the optional statusLine tap safely: show first, change only on confirmation, undo cleanly.

## ADDED Requirements

### Requirement: Plan before apply
`grid-fleet install` SHALL by default only print what it would write and run, and SHALL change the system only when invoked with `--apply` and after an interactive confirmation.

#### Scenario: Default run
- **WHEN** `install` runs without `--apply`
- **THEN** it prints the planned files and commands and modifies nothing

#### Scenario: Non-interactive apply
- **WHEN** `install --apply` runs with no TTY for confirmation
- **THEN** it prints the plan, exits non-zero and modifies nothing

### Requirement: Platform timers
On macOS, install SHALL create a launchd user agent; on Linux, a systemd user service and timer. Both SHALL run `collect` then `push` every `interval_s`, where `interval_s` is 300, 600 or 900 and other values are rejected. No step requires root.

#### Scenario: macOS
- **WHEN** `install --apply` is confirmed on macOS with interval 600
- **THEN** a user LaunchAgent plist with a 600 second interval exists and is loaded

#### Scenario: Linux
- **WHEN** `install --apply` is confirmed on Linux
- **THEN** a user `.service` and `.timer` exist under the user systemd directory and the timer is enabled

#### Scenario: Invalid interval
- **WHEN** the interval is 120
- **THEN** install exits non-zero naming the allowed values

#### Scenario: Headless Linux
- **WHEN** the platform is Linux
- **THEN** install prints a note that `loginctl enable-linger` is needed for the timer to run while logged out, and does not run it

### Requirement: Config written by install
`install` SHALL accept `--hub ALIAS|local`, `--account LABEL` and `--interval 300|600|900`, and `install --apply` SHALL write `interval_s`, `hub` and `account` into the grid-fleet config so the timer interval and the staleness interval cannot disagree. A flag SHALL win over the stored value; an omitted flag SHALL keep the stored value, else the default (`interval_s` 600, `account` `"default"`). `--hub` SHALL be checked with the same validator `push` uses (the reserved value `local` is accepted) and `--account` SHALL be a non-empty printable-ASCII label of at most 32 characters without leading or trailing whitespace. The plan SHALL show the before and after of just these three keys and change nothing. The write SHALL be atomic (temp file then rename), SHALL preserve every other key, unknown keys, key order and file layout, SHALL create a missing file as mode 0600 with `schema`, `host_name` (derived as `collect` derives it) and the three keys and a missing config directory as mode 0700, and SHALL never change the mode of an existing directory. The config SHALL be written before the timer is loaded, so the first tick sees the right hub. `--statusline` and `--uninstall` SHALL NOT write these keys and SHALL reject `--hub` and `--account`.

#### Scenario: Config created
- **WHEN** `install --apply --hub H --interval 300 --account A` is confirmed and no config file exists
- **THEN** the config file exists with mode 0600 holding `schema`, `host_name`, `interval_s` 300, `hub` `"H"` and `account` `"A"`, and it was written before the timer was loaded

#### Scenario: Config updated
- **WHEN** `install --apply --hub H2` is confirmed and the config holds other keys, unknown keys and a different `hub`
- **THEN** only the `hub` value changes and every other key, unknown key and the key order is preserved

#### Scenario: Plan shows the change
- **WHEN** `install` runs without `--apply`
- **THEN** it prints each of the three keys as old value to new value (or "unchanged") and writes nothing, not even the config directory

#### Scenario: Unusable config left alone
- **WHEN** the existing config is not valid JSON, not an object, or has an unknown `schema`
- **THEN** install exits non-zero naming the file and the file is byte-identical afterwards

#### Scenario: Hostile values
- **WHEN** `--hub` fails the push validator (for example it starts with `-` or contains whitespace or shell metacharacters) or `--account` is not a valid label
- **THEN** install exits non-zero (usage error) and writes nothing

#### Scenario: Interval written
- **WHEN** `install --apply --interval 900` is confirmed while the config holds `interval_s` 600
- **THEN** the config holds `interval_s` 900, the timer interval is 900 and no interval-mismatch warning is printed

### Requirement: Hub required to apply
`install --apply` SHALL refuse when no `hub` is known, meaning neither `--hub` nor a stored `hub` in the config (or a stored `hub` the push validator rejects), because every `push` tick would fail. The refusal SHALL print the plan with a warning, exit non-zero with a message naming `--hub` and the config key `hub`, and write and run nothing; it SHALL come before the interactive-terminal check.

#### Scenario: No hub
- **WHEN** `install --apply` runs with no `--hub` and no `hub` in the config
- **THEN** it exits non-zero with a message naming `--hub` and `hub`, no file is written and the scheduler is not called

#### Scenario: Hub from config
- **WHEN** `install --apply` runs without `--hub` and the config already holds a usable `hub`
- **THEN** the apply proceeds and keeps that `hub`

#### Scenario: Apply twice
- **WHEN** `install --apply` is run twice with the same inputs
- **THEN** the second run reports nothing to change, including the config, and does not prompt

### Requirement: Self-contained units
The generated unit SHALL use absolute paths to the Python interpreter and `grid-fleet` script and SHALL set a `PATH` captured at install time so `ssh` and its helpers resolve under a minimal scheduler environment.

#### Scenario: Minimal environment
- **WHEN** the scheduler runs the unit with only its configured environment
- **THEN** `ssh` and the interpreter are found

### Requirement: Idempotent and reversible
Re-running install with the same inputs SHALL be a no-op, with different inputs SHALL update in place, and `install --uninstall` SHALL remove the timer files and restore the statusLine, and SHALL leave the config's `interval_s`, `hub` and `account` untouched (they are the user's settings, not timer state).

#### Scenario: Second run
- **WHEN** `install --apply` is run twice with the same settings
- **THEN** the second run reports nothing to change

#### Scenario: Uninstall
- **WHEN** `install --uninstall` is confirmed
- **THEN** the timer is unloaded and its files removed, and `statusLine.command` is restored to its saved original

#### Scenario: Uninstall keeps the config keys
- **WHEN** `install --uninstall` is confirmed after an install that wrote `hub`, `account` and `interval_s`
- **THEN** the config file is byte-identical to before the uninstall

### Requirement: Confirmed statusLine change
The statusLine tap SHALL be installed only by `install --statusline`, which SHALL print the exact before and after of `statusLine.command` in `~/.claude/settings.json`, require explicit confirmation, back up the file, preserve all other settings, write atomically, and save the original command so it can be restored.

#### Scenario: Existing statusLine
- **WHEN** settings.json has a statusLine command and the user confirms
- **THEN** the command becomes the tap wrapping the original, all other keys are unchanged, and a backup exists

#### Scenario: Declined
- **WHEN** the user declines the confirmation
- **THEN** settings.json is byte-identical

#### Scenario: No statusLine configured
- **WHEN** settings.json has no statusLine
- **THEN** the diff shows a tap with no original command and still requires confirmation

#### Scenario: Never silent
- **WHEN** any command other than `install --statusline` runs
- **THEN** `~/.claude/settings.json` is not modified
