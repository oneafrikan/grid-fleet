# Makes `tests` a package and puts this directory on sys.path so that
# `from helpers.claude_tree import ...` works whether the suite is run as
# `unittest discover -s tests` (the documented command) or from the repo root.
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
