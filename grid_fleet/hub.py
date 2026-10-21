"""Hub inbox: path helpers (Group 1), reader + staleness (Group 4 extends).

Owner: Group 4 unit (grid-frontend-dev) for everything beyond the path helpers.
Layout (design.md "Hub"), under the hub state dir:

    inbox/<host>.json          latest snapshot per host (atomic rename)
    inbox/.<host>.json.tmp     in-flight upload; ignored by readers
"""

import os

from grid_fleet.config import sanitize_host_name


def _require_safe_host(host: str) -> str:
    # The host becomes a file name. Refuse anything the sanitiser would change,
    # so a caller that forgot to sanitise fails loudly instead of path-traversing.
    if host != sanitize_host_name(host):
        raise ValueError("unsafe host name: %r" % (host,))
    return host


def inbox_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "inbox")


def inbox_path(state_dir: str, host: str) -> str:
    return os.path.join(inbox_dir(state_dir), _require_safe_host(host) + ".json")


def inbox_tmp_path(state_dir: str, host: str) -> str:
    # Leading dot: readers skip dotfiles, so a half-written upload is never read.
    return os.path.join(inbox_dir(state_dir), "." + _require_safe_host(host) + ".json.tmp")
