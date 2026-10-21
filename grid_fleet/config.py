"""Config loading, host-name sanitising, state/config paths and the clock.

Owner: skeleton unit (Group 1). Every other module gets its paths, host name
and "now" from here, so tests can steer the whole tool with the GRID_FLEET_*
environment overrides and never touch the real ~/.claude or the real clock.

Public API (stable; later units rely on it):

    load_config(env=None) -> Config      strict load; raises ConfigError
    Config                               frozen dataclass of resolved settings
    ConfigError                          bad file / bad value / unknown schema
    sanitize_host_name(raw) -> str       charset A-Za-z0-9._-, <=64, no leading dot
    now() -> int                         epoch seconds; honours GRID_FLEET_NOW
    config_path(env=None) -> str
    state_dir(env=None) -> str
    claude_dir(env=None) -> str
    claude_bin(env=None) -> str

Environment overrides (design.md "Snapshot" / "Code layout"):
    GRID_FLEET_CONFIG       config file path
    GRID_FLEET_STATE_DIR    state dir (offsets.json, snapshot.json, limits.json, inbox/)
    GRID_FLEET_CLAUDE_DIR   Claude dir that contains projects/ (falls back to
                            CLAUDE_CONFIG_DIR if set, then ~/.claude)
    GRID_FLEET_CLAUDE_BIN   the `claude` executable (default: "claude" on PATH)
    GRID_FLEET_NOW          fixed epoch seconds for the clock
"""

import json
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

SCHEMA = 1
ALLOWED_INTERVALS = (300, 600, 900)
DEFAULT_INTERVAL_S = 600
DEFAULT_ACCOUNT = "default"
DEFAULT_REFRESH_S = 5

# The host name ends up in a remote file name (`<host>.json` in the hub inbox),
# so this charset is a security boundary, not cosmetics (design.md "Snapshot").
HOST_NAME_MAX = 64
# Used when sanitising leaves nothing (empty input, or only dots).
HOST_NAME_FALLBACK = "host"
_BAD_HOST_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ConfigError(Exception):
    """The config file (or an override) is unusable. Message is user-facing."""


def sanitize_host_name(raw: object) -> str:
    """Return a name safe to use as a file-name component.

    - every char outside A-Za-z0-9._- becomes "_" (so distinct hostile inputs
      stay distinguishable instead of collapsing together)
    - leading dots are stripped (no hidden files, no ".." traversal)
    - truncated to 64 chars
    - never empty: falls back to "host"
    """
    if not isinstance(raw, str):
        return HOST_NAME_FALLBACK
    cleaned = _BAD_HOST_CHARS.sub("_", raw).lstrip(".")[:HOST_NAME_MAX]
    return cleaned or HOST_NAME_FALLBACK


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    # Callers (and tests) may pass an explicit mapping; default is the process env.
    return os.environ if env is None else env


def _home() -> str:
    return os.path.expanduser("~")


def _xdg(env: Mapping[str, str], var: str, default_rel: str) -> str:
    # XDG spec: only absolute paths are valid; anything else is ignored.
    val = env.get(var, "")
    if val and os.path.isabs(val):
        return val
    return os.path.join(_home(), default_rel)


def config_path(env: Optional[Mapping[str, str]] = None) -> str:
    e = _env(env)
    override = e.get("GRID_FLEET_CONFIG")
    if override:
        return override
    return os.path.join(_xdg(e, "XDG_CONFIG_HOME", ".config"), "grid-fleet", "config.json")


def state_dir(env: Optional[Mapping[str, str]] = None) -> str:
    e = _env(env)
    override = e.get("GRID_FLEET_STATE_DIR")
    if override:
        return override
    return os.path.join(_xdg(e, "XDG_STATE_HOME", ".local/state"), "grid-fleet")


def claude_dir(env: Optional[Mapping[str, str]] = None) -> str:
    # Only returns a path; nothing here reads it. CLAUDE_CONFIG_DIR is honoured
    # when set (design.md open question 1 is verified by spike 2.1).
    e = _env(env)
    return (
        e.get("GRID_FLEET_CLAUDE_DIR")
        or e.get("CLAUDE_CONFIG_DIR")
        or os.path.join(_home(), ".claude")
    )


def claude_bin(env: Optional[Mapping[str, str]] = None) -> str:
    return _env(env).get("GRID_FLEET_CLAUDE_BIN") or "claude"


def now() -> int:
    """Current epoch seconds. EVERYONE uses this, never time.time() directly."""
    fixed = os.environ.get("GRID_FLEET_NOW")
    if fixed:
        try:
            return int(float(fixed))
        except ValueError:
            raise ConfigError("GRID_FLEET_NOW must be epoch seconds, got %r" % fixed)
    return int(time.time())


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


@dataclass(frozen=True)
class Config:
    """Resolved settings. Paths are filled from the environment at load time."""

    host_name: str = HOST_NAME_FALLBACK  # already sanitised
    account: str = DEFAULT_ACCOUNT
    interval_s: int = DEFAULT_INTERVAL_S
    hub: Optional[str] = None  # ssh alias, "local", or None (collect/view still work)
    expected_hosts: Tuple[str, ...] = ()  # config view.expected_hosts, sanitised
    refresh_s: int = DEFAULT_REFRESH_S  # config view.refresh_s
    ascii: bool = False  # config view.ascii
    statusline_original: Optional[str] = None
    # Resolved locations (from env overrides / XDG), not file keys.
    config_path: str = ""
    state_dir: str = ""
    claude_dir: str = ""
    claude_bin: str = "claude"


def _expect(data: dict, key: str, types: tuple, default, what: str):
    """Fetch an optional key, rejecting a wrong type (bool is not an int here)."""
    if key not in data or data[key] is None:
        return default
    val = data[key]
    if isinstance(val, bool) and bool not in types:
        raise ConfigError("%s must be %s" % (what, _what_type(types)))
    if not isinstance(val, types):
        raise ConfigError("%s must be %s" % (what, _what_type(types)))
    return val


def _what_type(types: tuple) -> str:
    return " or ".join(t.__name__ for t in types)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Load config.json. Missing file = all defaults. Unknown keys are ignored.

    Decision (bad `interval_s`): raise ConfigError rather than silently falling
    back, because a typo'd interval would otherwise change staleness maths with
    no feedback. Unknown `schema` also raises.
    """
    e = _env(env)
    path = config_path(e)
    data = {}  # type: dict
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except FileNotFoundError:
        parsed = None  # no file: defaults everywhere
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc))

    if parsed is not None:
        if not isinstance(parsed, dict):
            raise ConfigError("config %s must be a JSON object" % path)
        data = parsed
        schema = data.get("schema")
        # `True == 1` in Python, so exclude bool explicitly.
        if isinstance(schema, bool) or schema != SCHEMA:
            raise ConfigError(
                "config %s: unsupported schema %r (expected %d)" % (path, schema, SCHEMA)
            )

    interval = _expect(data, "interval_s", (int,), DEFAULT_INTERVAL_S, "interval_s")
    if interval not in ALLOWED_INTERVALS:
        raise ConfigError(
            "interval_s must be one of %s, got %r" % (list(ALLOWED_INTERVALS), interval)
        )

    host_raw = _expect(data, "host_name", (str,), None, "host_name")
    host = sanitize_host_name(host_raw if host_raw else _short_hostname())

    account = _expect(data, "account", (str,), DEFAULT_ACCOUNT, "account") or DEFAULT_ACCOUNT
    hub = _expect(data, "hub", (str,), None, "hub") or None
    statusline_original = _expect(
        data, "statusline_original", (str,), None, "statusline_original"
    )

    view = _expect(data, "view", (dict,), {}, "view")
    expected = _expect(view, "expected_hosts", (list,), [], "view.expected_hosts")
    if not all(isinstance(h, str) for h in expected):
        raise ConfigError("view.expected_hosts must be a list of strings")
    refresh = _expect(view, "refresh_s", (int,), DEFAULT_REFRESH_S, "view.refresh_s")
    if refresh < 1:
        raise ConfigError("view.refresh_s must be >= 1")
    ascii_only = _expect(view, "ascii", (bool,), False, "view.ascii")

    return Config(
        host_name=host,
        account=account,
        interval_s=interval,
        hub=hub,
        # Same sanitiser as the snapshot host, so names compare equal to inbox files.
        expected_hosts=tuple(sanitize_host_name(h) for h in expected),
        refresh_s=refresh,
        ascii=ascii_only,
        statusline_original=statusline_original,
        config_path=path,
        state_dir=state_dir(e),
        claude_dir=claude_dir(e),
        claude_bin=claude_bin(e),
    )
