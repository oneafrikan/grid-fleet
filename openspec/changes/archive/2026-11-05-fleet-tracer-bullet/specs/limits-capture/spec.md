## Purpose

Capture the account's 5-hour and 7-day Max-plan usage from the statusLine payload without disturbing the user's existing statusLine and without credentials.

## ADDED Requirements

### Requirement: Transparent chaining
The tap SHALL read its stdin once, pass the identical bytes to the user's original statusLine command, and return that command's stdout and exit code unchanged.

#### Scenario: Output identical
- **WHEN** the same payload is sent to the original command directly and via the tap
- **THEN** stdout and exit code are byte-identical

#### Scenario: Environment preserved
- **WHEN** the original command depends on environment variables
- **THEN** the tap passes the full environment through

#### Scenario: Tap failure cannot break the statusLine
- **WHEN** writing the limits file fails
- **THEN** the original command's output and exit code are still returned

#### Scenario: No original command
- **WHEN** no original command is configured
- **THEN** the tap prints nothing and exits 0

### Requirement: Slice-only atomic capture
When the payload contains `rate_limits`, the tap SHALL write only `captured_at` and, per window, `used_pct` and `resets_at` to the limits file, with mode 0600, by writing a temporary file and renaming it. It MUST NOT store any other payload field.

#### Scenario: Both windows present
- **WHEN** the payload has `five_hour` and `seven_day`
- **THEN** the file contains both windows and nothing else from the payload

#### Scenario: One window absent
- **WHEN** the payload has only `five_hour`
- **THEN** the file's `seven_day` keeps its previous value only if that value's `resets_at` is still in the future, otherwise it is omitted

#### Scenario: Concurrent sessions
- **WHEN** several sessions invoke the tap at the same moment
- **THEN** the file is always complete valid JSON

### Requirement: No rate limits yet
When the payload has no `rate_limits`, the tap SHALL leave the limits file unchanged and collect SHALL report `limits` as `null` if the file does not exist.

#### Scenario: Fresh session
- **WHEN** a session has made no API response and the payload lacks `rate_limits`
- **THEN** no limits file is created or modified

#### Scenario: Non-Max plan
- **WHEN** the account is not Pro/Max and `rate_limits` never appears
- **THEN** snapshots carry `limits: null` and the view shows `unknown`

### Requirement: Bounded overhead
The tap SHALL add less than 50 ms at the 95th percentile to the statusLine invocation on the reference machine (task 7.1 confirms or revises this figure), and MUST NOT make network calls.

#### Scenario: Latency
- **WHEN** 50 invocations are timed with and without the tap using a synthetic payload
- **THEN** the p95 difference is under 50 ms

### Requirement: No credentials
Limits capture MUST NOT read OAuth tokens, the macOS Keychain, or any Claude credential file, and MUST NOT contact any Anthropic endpoint.

#### Scenario: Code audit
- **WHEN** the limits module and tap are searched for credential paths and network calls
- **THEN** there are none
