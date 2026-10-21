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

### Requirement: Self-contained units
The generated unit SHALL use absolute paths to the Python interpreter and `grid-fleet` script and SHALL set a `PATH` captured at install time so `ssh` and its helpers resolve under a minimal scheduler environment.

#### Scenario: Minimal environment
- **WHEN** the scheduler runs the unit with only its configured environment
- **THEN** `ssh` and the interpreter are found

### Requirement: Idempotent and reversible
Re-running install with the same inputs SHALL be a no-op, with different inputs SHALL update in place, and `install --uninstall` SHALL remove exactly what install created, including restoring the statusLine.

#### Scenario: Second run
- **WHEN** `install --apply` is run twice with the same settings
- **THEN** the second run reports nothing to change

#### Scenario: Uninstall
- **WHEN** `install --uninstall` is confirmed
- **THEN** the timer is unloaded and its files removed, and `statusLine.command` is restored to its saved original

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
