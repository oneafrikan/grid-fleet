"""Environment-derived locations and the clock: the tiny piece of config the tap needs.

Why this is its own module: `grid-fleet tap` runs on every statusLine refresh and must add
well under 50 ms. Importing grid_fleet.config drags in dataclasses, typing, re and socket
(about 8-13 ms), so the tap imports only this file instead. config.py imports these names
from here and re-exports them, so there is exactly one definition of each.

Keep this module free of heavy imports (no typing, dataclasses, re, socket, argparse).
tests/test_limits.py asserts the tap's import set.
"""

import os
import time


class ConfigError(Exception):
    """The config file (or an override) is unusable. Message is user-facing."""


def _env(env):
    # Callers (and tests) may pass an explicit mapping; default is the process env.
    return os.environ if env is None else env


def _home():
    return os.path.expanduser("~")


def _xdg(env, var, default_rel):
    # XDG spec: only absolute paths are valid; anything else is ignored.
    val = env.get(var, "")
    if val and os.path.isabs(val):
        return val
    return os.path.join(_home(), default_rel)


def state_dir(env=None):
    """Where the snapshot, limits.json and inbox live (GRID_FLEET_STATE_DIR wins)."""
    e = _env(env)
    override = e.get("GRID_FLEET_STATE_DIR")
    if override:
        return override
    return os.path.join(_xdg(e, "XDG_STATE_HOME", ".local/state"), "grid-fleet")


def now():
    """Current epoch seconds. EVERYONE uses this, never time.time() directly."""
    fixed = os.environ.get("GRID_FLEET_NOW")
    if fixed:
        try:
            return int(float(fixed))
        except ValueError:
            raise ConfigError("GRID_FLEET_NOW must be epoch seconds, got %r" % fixed)
    return int(time.time())
