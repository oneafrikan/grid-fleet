"""Static guard (task 1.4): grid_fleet/ must stay Python 3.9 + stdlib only.

Parses every file under grid_fleet/ with `ast` and fails on:
  - `match` statements (3.10+; on 3.9 a file using one also fails to parse)
  - `X | Y` in ANY annotation (evaluated at runtime on 3.9 unless deferred; we flag it
    even with `from __future__ import annotations`, to stay safe and simple)
  - imports of anything outside the stdlib allowlist (including `tomllib`, 3.11+)
Runs under 3.9, where sys.stdlib_module_names does not exist, so the allowlist is explicit.
"""

import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "grid_fleet")

# Top-level stdlib modules (3.9-era plus the ones this tool plausibly needs). Anything
# else is a third-party dependency and fails. tomllib is deliberately absent (3.11+).
# `_signal` is the builtin behind `signal`; limits.py (the statusLine tap) uses it for the
# raw SIGPIPE number because importing `signal` costs ~3 ms of start-up on every refresh.
STDLIB = frozenset("""
_signal __future__ abc argparse array ast atexit base64 binascii bisect builtins calendar codecs
collections colorsys configparser contextlib copy csv ctypes curses dataclasses datetime
decimal difflib email enum errno fcntl fnmatch functools getpass gettext glob grp gzip
hashlib heapq hmac html http importlib inspect io ipaddress itertools json locale logging
math mimetypes multiprocessing numbers operator os pathlib platform plistlib pwd queue
random re resource secrets select selectors shlex shutil signal socket stat statistics
string struct subprocess sys tempfile termios textwrap threading time traceback tty types
typing unicodedata unittest urllib uuid warnings weakref zlib
""".split())

# Modules that exist in newer stdlib but must not be used (3.9 floor).
FORBIDDEN = frozenset({"tomllib"})
# This package's own name is fine.
OWN = frozenset({"grid_fleet"})


def _has_bitor(node: ast.AST) -> bool:
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(node))


def _annotations(tree: ast.AST):
    for n in ast.walk(tree):
        if isinstance(n, ast.arg) and n.annotation is not None:
            yield n.annotation
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            yield n.returns
        elif isinstance(n, ast.AnnAssign):
            yield n.annotation


def check_source(source: str, name: str = "<src>"):
    """Return a list of human-readable violations for one source string."""
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as exc:
        return ["%s: does not parse on this Python: %s" % (name, exc)]
    problems = []
    match_type = getattr(ast, "Match", None)  # absent on 3.9
    for n in ast.walk(tree):
        if match_type is not None and isinstance(n, match_type):
            problems.append("%s:%d: match statement" % (name, n.lineno))
        if isinstance(n, ast.Import):
            mods = [(a.name, n.lineno) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            # level > 0 is a relative import of our own package: fine.
            mods = [(n.module or "", n.lineno)] if n.level == 0 else []
        else:
            continue
        for mod, line in mods:
            top = mod.split(".")[0]
            if top in FORBIDDEN:
                problems.append("%s:%d: forbidden module %s" % (name, line, top))
            elif top not in STDLIB and top not in OWN:
                problems.append("%s:%d: non-stdlib import %s" % (name, line, top))
    for ann in _annotations(tree):
        if _has_bitor(ann):
            problems.append("%s:%d: `X | Y` in annotation" % (name, ann.lineno))
        # String annotations are parsed lazily by nobody on 3.9, but check them anyway.
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                if _has_bitor(ast.parse(ann.value, mode="eval")):
                    problems.append("%s:%d: `X | Y` in string annotation" % (name, ann.lineno))
            except SyntaxError:
                pass
    return problems


def package_files():
    for root, _dirs, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


class TestGuardOnPackage(unittest.TestCase):
    def test_package_files_found(self):
        # Guard against the guard silently scanning nothing.
        names = {os.path.basename(p) for p in package_files()}
        self.assertTrue({"cli.py", "config.py", "hub.py"} <= names, names)

    def test_no_violations(self):
        problems = []
        for path in package_files():
            with open(path, encoding="utf-8") as fh:
                problems += check_source(fh.read(), os.path.relpath(path, REPO))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


class TestGuardDetects(unittest.TestCase):
    """The checker must actually catch what it claims to (self-test on bad snippets)."""

    def test_bitor_annotations(self):
        self.assertTrue(check_source("def f(x: int | None): pass"))
        self.assertTrue(check_source("def f() -> int | None: pass"))
        self.assertTrue(check_source("x: int | str = 1"))
        self.assertTrue(check_source("from __future__ import annotations\ndef f(x: int | None): pass"))
        self.assertTrue(check_source("def f(x: 'int | None'): pass"))
        self.assertFalse(check_source("def f(x: int) -> int:\n    return x | 1"))  # runtime bit-or is fine

    def test_third_party_and_tomllib(self):
        self.assertTrue(check_source("import requests"))
        self.assertTrue(check_source("from yaml import safe_load"))
        self.assertTrue(check_source("import tomllib"))
        self.assertTrue(check_source("import os.path\nimport numpy.linalg"))

    def test_allows_stdlib_and_own(self):
        self.assertEqual(check_source("import os, json\nfrom typing import Optional\n"
                                      "from grid_fleet import config\nfrom . import x"), [])

    @unittest.skipIf(sys.version_info < (3, 10), "match is a SyntaxError on 3.9")
    def test_match_detected(self):
        self.assertTrue(check_source("match x:\n    case 1:\n        pass\n"))

    def test_match_unparseable_on_39_is_reported(self):
        if sys.version_info >= (3, 10):
            self.skipTest("parses on this Python")
        self.assertTrue(check_source("match x:\n    case 1:\n        pass\n"))


if __name__ == "__main__":
    unittest.main()
