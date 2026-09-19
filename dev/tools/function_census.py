#!/usr/bin/env python3
"""How heavy is each FUNCTION — by length, and by decisions.

**Why this exists.**  ``class_census.py`` weighs a class by the methods it
implements, so it is blind to what those methods CONTAIN: measured 2026-09-19,
``uc_led_control._setup_ui`` is **585 lines** and no gate in the repo had an
opinion about it.  ``class_census`` also walks ``ClassDef`` only, so a
module-level function is invisible to it entirely — and two of the densest
functions in the tree are module-level.

**Two axes, because they measure different defects and barely overlap.**
At ``>100 lines`` and ``>=20 decisions``: 22 functions are only long, 2 are
only branchy, 5 are both.

* **lines** — a function you cannot hold in your head.  ``LoadTheme.execute``
  at 342.
* **decisions** — paths you must reason about and test.  ``format_metric`` is
  **70 lines with 30 decisions**, and a length gate cannot see it at all.

A length gate alone misses the densest logic; a decision gate alone misses the
585-line builders.  Neither is a superset, so both are reported.

**What counts as a decision, and what deliberately does not.**  ``if`` /
``while`` / ``except`` / ternary / ``match`` case / ``for`` / each extra
``and``-``or`` operand.  A COMPREHENSION counts as its loop and nothing more:
counting its ``if``s too inflated ``ipc._coerce`` from 20 to 26 and
``auto_map`` from 15 to 25, which ranked compact functional code alongside
genuinely tangled branching.  ``with`` and ``assert`` do not count — neither
is a path the reader chooses between.

**The weight is on the function's OWN body.**  A nested ``def`` is weighed
separately, and its lines and decisions are NOT also charged to its parent —
otherwise a factory would be penalised twice for the same code, and the API's
route factory (whose endpoints are nested ``def``s) would dominate a list it
does not belong on.

**Scope defence.**  An unparseable file RAISES rather than being skipped —
same reasoning ``class_census`` documents: the file a census cannot read is
exactly the one being edited.

Usage::

    PYTHONPATH=src python3.12 dev/tools/function_census.py
    PYTHONPATH=src python3.12 dev/tools/function_census.py --lines 140
    PYTHONPATH=src python3.12 dev/tools/function_census.py --decisions 18
    PYTHONPATH=src python3.12 dev/tools/function_census.py --area core

``tests/test_function_weight.py`` imports :func:`too_long` and
:func:`too_branchy` and ratchets both, so the tool and the gate can never
measure different things.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "trcc"

#: Over this many lines and a function no longer fits in one reading.  Chosen
#: from the distribution, not taste: 100 -> 27 functions, 120 -> 19,
#: 140 -> 16, 160 -> 8.  140 is where the long tail of genuine outliers
#: starts; 100 would pull in seven ``ui/gui`` builders that are being deleted
#: with the skin rather than decomposed.
LONG_FUNCTION_LINES = 140

#: At or over this many decisions a function has more paths than a reader can
#: hold.  Distribution measured with THIS tool's own metric: 14 -> 22,
#: 16 -> 12, 18 -> 9, 20 -> 7, 22 -> 5.  18 is where the slope flattens, and
#: it is the first threshold that admits ``ConnectDevice.execute`` without
#: admitting a dozen ordinary handlers.
#:
#: Measured with the tool, not from a scratch script: an earlier draft of this
#: comment carried 14 -> 21 / 18 -> 8, off by one in both places, because it
#: was computed by a probe that did not count comprehensions at all while the
#: tool counts each as its loop.  A distribution quoted beside a threshold is
#: a claim about the thing it is thresholding.
BRANCHY_FUNCTION_DECISIONS = 18

#: Statements the reader must choose between.  ``ast.With`` and ``ast.Assert``
#: are deliberately absent — see the module docstring.
_DECISION_NODES = (
    ast.If, ast.While, ast.ExceptHandler, ast.IfExp,
    ast.For, ast.AsyncFor, ast.match_case,
)
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


@dataclass(frozen=True)
class FunctionWeight:
    """One function, weighed by length and by the paths through it."""

    name: str
    path: str
    lineno: int
    lines: int
    decisions: int

    def __str__(self) -> str:
        return (f"{self.lines:5d} lines {self.decisions:3d} decisions  "
                f"{self.name}  ({self.path}:{self.lineno})")


def _own_body(fn: ast.AST) -> list[ast.AST]:
    """Every node belonging to *fn* itself, excluding nested functions.

    Without this a factory is charged for code that is already weighed on its
    own row — ``ui/api/main.py::build_app`` defines its middlewares and one
    endpoint inline, and counting them twice put it on a list it does not
    belong on.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _decisions(fn: ast.AST) -> int:
    total = 0
    for node in _own_body(fn):
        if isinstance(node, _DECISION_NODES):
            total += 1
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            total += 1           # its loop only; see the module docstring
    return total


def census(root: Path | None = None) -> list[FunctionWeight]:
    """Weigh every function under *root*, heaviest first.

    Raises ``SyntaxError`` on a file it cannot parse — see the module
    docstring on why that is not a skip.
    """
    base = root or _SRC
    rows: list[FunctionWeight] = []
    for path in sorted(base.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owner: dict[int, str] = {}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for member in cls.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner[id(member)] = cls.name
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualified = f"{owner.get(id(node), '')}.{node.name}".lstrip(".")
            rows.append(FunctionWeight(
                name=qualified,
                path=str(path.relative_to(base.parent.parent)),
                lineno=node.lineno,
                lines=(node.end_lineno or node.lineno) - node.lineno + 1,
                decisions=_decisions(node),
            ))
    rows.sort(key=lambda r: (-r.decisions, -r.lines, r.name))
    return rows


def too_long(
    threshold: int = LONG_FUNCTION_LINES, root: Path | None = None,
) -> list[FunctionWeight]:
    """Every function OVER *threshold* lines."""
    return [f for f in census(root) if f.lines > threshold]


def too_branchy(
    threshold: int = BRANCHY_FUNCTION_DECISIONS, root: Path | None = None,
) -> list[FunctionWeight]:
    """Every function AT OR OVER *threshold* decisions."""
    return [f for f in census(root) if f.decisions >= threshold]


def main(argv: list[str]) -> int:
    lines = LONG_FUNCTION_LINES
    decisions = BRANCHY_FUNCTION_DECISIONS
    if "--lines" in argv:
        lines = int(argv[argv.index("--lines") + 1])
    if "--decisions" in argv:
        decisions = int(argv[argv.index("--decisions") + 1])

    root = _SRC
    if "--area" in argv:
        root = _SRC / argv[argv.index("--area") + 1]
        if not root.is_dir():
            print(f"No such area: {root}")
            return 1

    rows = census(root)
    long_ = [f for f in rows if f.lines > lines]
    branchy = [f for f in rows if f.decisions >= decisions]
    both = {(f.path, f.lineno) for f in long_} & {(f.path, f.lineno) for f in branchy}
    print(f"{len(rows)} function(s) under {root}\n")
    print(f"OVER {lines} LINES — {len(long_)}")
    for row in sorted(long_, key=lambda r: -r.lines):
        print(f"  {row}")
    print(f"\nAT OR OVER {decisions} DECISIONS — {len(branchy)}")
    for row in branchy:
        print(f"  {row}")
    print(f"\n{len(both)} function(s) trip BOTH — the axes are not "
          f"substitutes for each other.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
