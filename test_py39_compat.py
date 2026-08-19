#!/usr/bin/env python3
"""
Keeps the kit importable on Python 3.9.

The declared floor was 3.10 on the assumption that PEP 604 unions (`str | None`)
require it. They do not, as long as every module carries
`from __future__ import annotations` — annotations then stay unevaluated strings.
A managed 3.9 install was locked out for no reason.

These tests are the guard against that regressing. They run on any version,
because the checks are static: `ast.parse(feature_version=(3, 9))` for syntax, and
an AST walk for unions in positions that ARE evaluated at runtime.

Not covered here: whether the tests all *pass* on 3.9. That needs a 3.9
interpreter. To check on a machine that has one:

    python3.9 -m venv /tmp/v39 && /tmp/v39/bin/pip install -e .
    /tmp/v39/bin/python -m unittest discover -s intake -p 'test_*.py'
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent
FLOOR = (3, 9)

# Stdlib APIs added after the floor. A hit means the floor must rise or the call
# must go — silently shipping one turns into an AttributeError on a customer's box.
POST_39_APIS = (
    "itertools.pairwise", ".bit_count(", "slots=True", "anext(", "aiter(",
    "EncodingWarning", "inspect.get_annotations", "contextlib.aclosing",
    "statistics.correlation", "statistics.covariance", "types.EllipsisType",
    "sys.orig_argv", "int.bit_count",
)

PRIMITIVES = {"str", "int", "float", "bool", "dict", "list", "tuple", "set",
              "bytes", "complex", "frozenset", "type"}


def python_files() -> list[Path]:
    skip = {"node_modules", ".venv", "build", "dist"}
    return sorted(p for p in KIT.rglob("*.py")
                  if not any(part in skip for part in p.parts))


class TestPython39Compatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = python_files()
        assert cls.files, "no python files discovered"

    def test_every_module_defers_annotations(self):
        """The one thing that makes `str | None` legal on 3.9."""
        missing = [str(p.relative_to(KIT)) for p in self.files
                   if "from __future__ import annotations" not in p.read_text()]
        self.assertEqual(missing, [], "these modules would evaluate unions at import")

    def test_syntax_parses_under_the_floor(self):
        bad = []
        for p in self.files:
            try:
                ast.parse(p.read_text(), feature_version=FLOOR)
            except SyntaxError as e:
                bad.append(f"{p.relative_to(KIT)}:{e.lineno} {e.msg}")
        self.assertEqual(bad, [])

    def test_no_unions_evaluated_at_runtime(self):
        """A union outside an annotation IS evaluated, and `str | None` raises
        TypeError on 3.9. Annotations are exempt only because of the future import."""
        offenders = []
        for p in self.files:
            tree = ast.parse(p.read_text())
            exempt: set[int] = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.AnnAssign) and n.annotation:
                    exempt |= {id(x) for x in ast.walk(n.annotation)}
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = n.args
                    for a in [*args.args, *args.kwonlyargs, *args.posonlyargs,
                              args.vararg, args.kwarg]:
                        if a is not None and a.annotation:
                            exempt |= {id(x) for x in ast.walk(a.annotation)}
                    if n.returns:
                        exempt |= {id(x) for x in ast.walk(n.returns)}
            for n in ast.walk(tree):
                if not (isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)):
                    continue
                if id(n) in exempt:
                    continue
                for side in (n.left, n.right):
                    is_none = isinstance(side, ast.Constant) and side.value is None
                    is_prim = isinstance(side, ast.Name) and side.id in PRIMITIVES
                    if is_none or is_prim:
                        offenders.append(f"{p.relative_to(KIT)}:{n.lineno}")
        self.assertEqual(sorted(set(offenders)), [])

    def test_no_stdlib_apis_newer_than_the_floor(self):
        hits = []
        for p in self.files:
            if p.name == Path(__file__).name:      # this file lists them as strings
                continue
            text = p.read_text()
            for api in POST_39_APIS:
                if api in text:
                    hits.append(f"{p.relative_to(KIT)}: {api}")
        self.assertEqual(hits, [])

    def test_declared_floor_matches_the_verified_one(self):
        pyproject = (KIT / "pyproject.toml").read_text()
        want = f'requires-python = ">={FLOOR[0]}.{FLOOR[1]}"'
        self.assertIn(want, pyproject,
                      "pyproject must declare the floor these tests verify")

        import re
        cli = (KIT / "cli.py").read_text()
        m = re.search(r"MIN_PYTHON = \((\d+), (\d+)\)", cli)
        self.assertIsNotNone(m, "cli.py must declare MIN_PYTHON")
        self.assertEqual((int(m.group(1)), int(m.group(2))), FLOOR,
                         "cli.py's runtime gate must match pyproject and these tests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
