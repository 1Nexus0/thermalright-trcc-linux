#!/usr/bin/env python3
"""How many responsibilities does each class carry — the god-class census.

**Why this exists.**  ``ui/qtgui`` was recorded at ZERO classes over 25
methods on 2026-09-15.  Two days later ``SystemPanel`` was at 28, and
nothing noticed, because nothing was watching: there was no god-class gate
anywhere in the repo.  A decomposition without a gate regrows — that is the
durable finding, larger than the class that prompted it.

**The weight is methods that have an implementation.**  A class's method
count is a proxy for how many things it is responsible for, and an
``@abstractmethod`` has no body, so there is nothing for the class to be
responsible for.  That is not a new rule invented here: it is the same
exclusion ``logging_coverage.py`` argues for ("abstract methods and stubs —
no body ran"), applied to a different question.

It matters concretely.  Counting raw methods puts two PORTS at the top of
the list — ``ContentStore`` (26 methods, **26 abstract**) and ``Platform``
(25 / 24) — and a port's width is its CONTRACT, fixed by how many things the
domain asks an adapter to do.  Narrowing it is an ISP change to the domain,
not a decomposition, and mixing the two would make the gate demand the wrong
work.  With the exclusion those two weigh 0 and 1, and ``BaseOS`` correctly
drops from 28 to 23: fat, but 23 of it is real shared OS behaviour.

Non-abstract *stubs* (a body of only ``pass`` / ``...``) were measured and
excluded nothing — the census is 11 classes either way at threshold 25 — so
they are counted, and this note is here so the next reader knows the
simpler predicate was a measurement rather than an oversight.

**Scope defence.**  An unparseable file RAISES rather than being skipped.  A
census that silently drops what it cannot read is a gate whose scope shrinks
without anyone deciding to shrink it, and the file it dropped is exactly the
one being edited.

Usage::

    PYTHONPATH=src python3.12 dev/tools/class_census.py
    PYTHONPATH=src python3.12 dev/tools/class_census.py --threshold 20
    PYTHONPATH=src python3.12 dev/tools/class_census.py --area ui/qtgui

``tests/test_god_classes.py`` imports :func:`god_classes` and ratchets the
answer, so the tool and the gate can never measure different things.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "trcc"

#: A class at or above this many implemented methods is a god class.  Chosen
#: from the distribution rather than taste: 23 -> 14 classes, 24 -> 12,
#: 25 -> 11, 26 -> 10.  25 is the flat part, and it is the number the
#: 2026-09-12 census in ``project_plan_the_remaining_gap`` already used.
GOD_CLASS_METHODS = 25


@dataclass(frozen=True)
class ClassWeight:
    """One class, weighed by the methods it actually implements."""

    name: str
    path: str
    lineno: int
    methods: int
    abstract: int
    lines: int

    def __str__(self) -> str:
        return (f"{self.methods:4d} impl +{self.abstract:<3d} abstract  "
                f"{self.lines:5d} lines  {self.name}  ({self.path}:{self.lineno})")


def _is_abstract(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for ``@abstractmethod`` / ``@abstractproperty``, plain or dotted."""
    names = {"abstractmethod", "abstractproperty"}
    return any(
        (isinstance(d, ast.Name) and d.id in names)
        or (isinstance(d, ast.Attribute) and d.attr in names)
        for d in fn.decorator_list
    )


def census(root: Path | None = None) -> list[ClassWeight]:
    """Weigh every class under *root*, heaviest first.

    Raises ``SyntaxError`` on a file it cannot parse — see the module
    docstring on why that is not a skip.
    """
    base = root or _SRC
    rows: list[ClassWeight] = []
    for path in sorted(base.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            abstract = [m for m in methods if _is_abstract(m)]
            rows.append(ClassWeight(
                name=node.name,
                path=str(path.relative_to(base.parent.parent)),
                lineno=node.lineno,
                methods=len(methods) - len(abstract),
                abstract=len(abstract),
                lines=(node.end_lineno or node.lineno) - node.lineno + 1,
            ))
    rows.sort(key=lambda r: (-r.methods, r.name))
    return rows


def god_classes(
    threshold: int = GOD_CLASS_METHODS,
    root: Path | None = None,
) -> list[ClassWeight]:
    """Every class at or over *threshold* implemented methods."""
    return [c for c in census(root) if c.methods >= threshold]


def main(argv: list[str]) -> int:
    threshold = GOD_CLASS_METHODS
    if "--threshold" in argv:
        threshold = int(argv[argv.index("--threshold") + 1])

    root = _SRC
    if "--area" in argv:
        root = _SRC / argv[argv.index("--area") + 1]
        if not root.is_dir():
            print(f"No such area: {root}")
            return 1

    rows = census(root)
    heavy = [c for c in rows if c.methods >= threshold]
    print(f"{len(rows)} class(es) under {root}, "
          f"{len(heavy)} at or over {threshold} implemented methods\n")
    for row in heavy:
        print(f"  {row}")
    if not heavy:
        print("  (none)")
    print(f"\nNext heaviest below the line: "
          f"{', '.join(f'{c.name} {c.methods}' for c in rows[len(heavy):][:5])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
