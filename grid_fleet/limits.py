"""statusLine tap (wrapper around the user's original statusLine) + limits.json read.

Owner: Group 7 unit (core-platform-engineer). Skeleton stub from Group 1.
Entry point used by cli.py: tap_main(config, args) -> int, where
args.original_command is the list of argv words after `tap --` ([] if none).
Must stay fast: the tap runs on every statusLine refresh in every session.
"""

import sys


def tap_main(config, args) -> int:
    sys.stderr.write("grid-fleet tap: not implemented\n")
    return 2
