#!/usr/bin/env python3
"""Find method bodies that sibling subclasses of one base write identically.

**Why this is a tool and not a grep.**  "abc consolidates" — pull an answer the
siblings already agree on UP to the base, keep polymorphism where behaviour
genuinely differs.  Deciding that needs the BODIES compared, not the names:
a grep finds `_build_autostart` on two OS classes and cannot tell you whether
they return the same thing, and the one measurement that matters is whether
they do.

**Why it reports its own falsifier.**  Two bodies can be byte-identical and
still mean different things, because each resolves its free variables against
its OWN class.  This has already bitten the project once: `task_key` read a
different `KEY_PREFIX` per subclass, so identical text was not an identical
answer.  Every cluster therefore prints the class attributes its body reads,
resolved per sibling, and marks the ones that DIFFER with ``!!``.  A cluster
carrying a ``!!`` line is not a pull-up candidate — it is the `0` case, where
the base takes the skeleton and each subclass supplies the varying value.

**What it deliberately does NOT report.**  Same shape under a DIFFERENT method
name.  `LinuxOS._build_autostart` and `BsdOS._build_autostart` are one cluster;
two differently-named methods that happen to share a shape are polymorphism,
and calling them duplication is how a previous cut of this tool produced a
false positive.  Matching is by (base, method name, body).

The ternary the maintainer sorts results by — three destinations, not a
similarity score:

    +1  identical, and the free variables agree      -> pull UP to the base
     0  identical shape, one varying point (``!!``)  -> base takes the skeleton,
                                                        or it belongs in a
                                                        different method entirely
    -1  genuinely different                          -> leave it; the siblings
                                                        are ONE GROUP needing one
                                                        shape, not N duplicates

A human makes that call.  This prints the evidence for it.

    PYTHONPATH=src python3 dev/tools/dup_bodies.py           # every cluster
    PYTHONPATH=src python3 dev/tools/dup_bodies.py --area ui # one area
    PYTHONPATH=src python3 dev/tools/dup_bodies.py --gate    # prove the tool works

**Run ``--gate`` before you trust a number.**  It re-proves the tool against
known answers on a synthetic fixture — a real duplicate IS found, a
same-shape/different-name pair is NOT, a differing class attribute IS flagged,
a one-literal change breaks the cluster, and a docstring-only difference does
not.  An instrument whose zero has never been challenged is not evidence; this
project has shipped a confident zero from a broken probe before.
"""
from __future__ import annotations

import ast
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "trcc"

#: A method whose body is only this is a stub, not a shared answer.  It is still
#: reported — thirty of them across one port family is exactly the finding — but
#: it is marked, because "every backend says it cannot" is a different problem
#: from "every backend repeats a computation".
_STUB_BODIES = frozenset({"return None", "pass", "return", "..."})


class Method:
    """One concrete method on one class, with everything a comparison needs."""

    def __init__(self, cls: ast.ClassDef, node: ast.FunctionDef
                 | ast.AsyncFunctionDef, path: Path) -> None:
        self.cls = cls
        self.node = node
        self.path = path
        self.name = node.name

    @property
    def body(self) -> list[ast.stmt]:
        """The body with any docstring removed — wording is not behaviour."""
        body = self.node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            return body[1:]
        return body

    @property
    def fingerprint(self) -> str:
        """Structural identity: signature + body, docstrings and comments out."""
        return ast.dump(self.node.args) + "||" + "".join(
            ast.dump(stmt) for stmt in self.body)

    @property
    def is_stub(self) -> bool:
        rendered = "".join(ast.unparse(s) for s in self.body).strip()
        return rendered in _STUB_BODIES

    @property
    def reads(self) -> set[str]:
        """Attribute names this body reads off ``self`` / ``cls``."""
        found: set[str] = set()
        for node in ast.walk(self.node):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("self", "cls")):
                found.add(node.attr)
        return found


class Cluster:
    """Sibling subclasses of one base that write one method identically."""

    def __init__(self, base: str, name: str, methods: list[Method]) -> None:
        self.base = base
        self.name = name
        self.methods = methods

    @property
    def copies_saved(self) -> int:
        """Lines that would stop existing if this were written once."""
        return len(self.methods[0].body) * (len(self.methods) - 1)

    @property
    def divergences(self) -> list[tuple[str, dict[str, str]]]:
        """Class attributes the shared body reads that are NOT shared.

        The falsifier printed beside the finding: identical text that resolves
        to a different value per subclass is the `0` case, not a pull-up.
        """
        out: list[tuple[str, dict[str, str]]] = []
        for attr in sorted(self.methods[0].reads):
            values = {m.cls.name: v for m in self.methods
                      if (v := _class_attr(m.cls, attr)) is not None}
            if len(set(values.values())) > 1:
                out.append((attr, values))
        return out

    def render(self) -> str:
        # Two packages can hold a same-named class — gui and qtgui both have a
        # BasePanel, and "BasePanel, BasePanel" reads as a tool bug rather than
        # as the cross-skin duplication it actually is.  Qualify by module when
        # the names alone do not tell them apart.
        names = [m.cls.name for m in self.methods]
        siblings = ", ".join(
            f"{m.cls.name} ({m.path.parent.name})"
            if names.count(m.cls.name) > 1 else m.cls.name
            for m in self.methods)
        mark = "  [stub]" if self.methods[0].is_stub else ""
        homes = sorted({str(m.path.relative_to(_SRC.parent.parent))
                        for m in self.methods})
        lines = [f"[{self.copies_saved:>3} lines] {self.base}.{self.name}"
                 f"  <- {siblings}{mark}"]
        lines += [f"            {home}" for home in homes]
        lines += [f"         !! self.{attr} differs: {values}"
                  for attr, values in self.divergences]
        return "\n".join(lines)


def _class_attr(cls: ast.ClassDef, name: str) -> str | None:
    """The class-level value assigned to *name*, or None if it is not one."""
    found: str | None = None
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets, value = [stmt.target.id], stmt.value
        else:
            continue
        if name in targets and value is not None:
            found = ast.unparse(value)
    return found


def _base_names(cls: ast.ClassDef) -> list[str]:
    """Base class names, unwrapping ``Base[T]`` and ``mod.Base``."""
    names = []
    for base in cls.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    return names


def _is_abstract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("abstract" in ast.unparse(d) for d in node.decorator_list)


def clusters(root: Path) -> list[Cluster]:
    """Every duplicate-body cluster under *root*, biggest saving first."""
    children: dict[str, list[tuple[ast.ClassDef, Path]]] = defaultdict(list)
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in _base_names(node):
                    children[base].append((node, path))

    # Where each base NAME is defined.  A name defined twice (gui and qtgui both
    # have a BasePanel) is ambiguous, and its children must not be pooled — but
    # siblings legitimately live in DIFFERENT files (one sensor backend per
    # module), so the split is by which base a child is nearest to, never by the
    # child's own file.  Keying on the child's file instead reports zero
    # clusters for every family that spans modules, which is most of them.
    defined_in: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                defined_in[node.name].append(path)

    def owner(base: str, child: Path) -> str:
        """Which definition of *base* this child most plausibly inherits."""
        homes = defined_in.get(base, [])
        if len(homes) <= 1:
            return base
        nearest = max(homes, key=lambda h: len(
            set(h.parts) & set(child.parts)))
        return f"{base}@{nearest.parent.name}"

    found: list[Cluster] = []
    for base, siblings in sorted(children.items()):
        if len(siblings) < 2:
            continue
        by_key: dict[tuple[str, str], list[Method]] = defaultdict(list)
        for cls, path in siblings:
            for member in cls.body:
                if (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not _is_abstract(member)):
                    by_key[(owner(base, path), member.name)].append(
                        Method(cls, member, path))
        for (qualified, name), methods in sorted(by_key.items()):
            groups: dict[str, list[Method]] = defaultdict(list)
            for method in methods:
                groups[method.fingerprint].append(method)
            found += [Cluster(qualified, name, group)
                      for group in groups.values() if len(group) > 1]
    return sorted(found, key=lambda c: -c.copies_saved)


# =========================================================================
# --gate — prove the tool before trusting its number
# =========================================================================

_FIXTURE = '''
class Base:
    pass

class Same1(Base):
    STYLE = "red"
    def paint(self):
        total = 1 + 2
        return self.STYLE + str(total)

class Same2(Base):
    STYLE = "red"
    def paint(self):
        """Wording differs; behaviour does not."""
        total = 1 + 2
        return self.STYLE + str(total)

class Diverges(Base):
    STYLE = "blue"
    def paint(self):
        total = 1 + 2
        return self.STYLE + str(total)

class Changed(Base):
    STYLE = "red"
    def paint(self):
        total = 1 + 3
        return self.STYLE + str(total)

class ShapeA(Base):
    def render_left(self):
        total = 1 + 2
        return str(total)

class ShapeB(Base):
    def render_right(self):
        total = 1 + 2
        return str(total)
'''


#: Siblings of one base, living in SEPARATE modules — the normal shape in this
#: tree (one sensor backend per file, one OS per file).  A gate fixture confined
#: to a single file cannot fail on the bug that breaks exactly this case, and
#: an earlier cut of the tool shipped reporting ZERO clusters for every
#: multi-module family while its single-file gate stayed green.
_FIXTURE_SPLIT_A = '''
class Shared:
    pass

class InFileA(Shared):
    def measure(self):
        reading = 40 + 2
        return reading
'''

_FIXTURE_SPLIT_B = '''
from fixture_a import Shared

class InFileB(Shared):
    def measure(self):
        reading = 40 + 2
        return reading
'''


def gate() -> int:
    """Re-prove the tool against known answers.  Offline and instant."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "fixture.py").write_text(_FIXTURE, encoding="utf-8")
        (root / "fixture_a.py").write_text(_FIXTURE_SPLIT_A, encoding="utf-8")
        (root / "fixture_b.py").write_text(_FIXTURE_SPLIT_B, encoding="utf-8")
        found = clusters(root)

    checks: list[tuple[str, bool]] = []
    paint = [c for c in found if c.name == "paint"]
    names = {m.cls.name for c in paint for m in c.methods}

    checks.append(("a real duplicate is found", len(paint) == 1))
    checks.append(("a docstring-only difference still clusters",
                   {"Same1", "Same2"} <= names))
    checks.append(("a one-literal change breaks the cluster",
                   "Changed" not in names))
    checks.append(("same shape under a different name is NOT a cluster",
                   not any(c.name.startswith("render_") for c in found)))
    diverging = [a for c in paint for a, _ in c.divergences]
    checks.append(("a differing class attribute is flagged",
                   "STYLE" in diverging))
    split = [c for c in found if c.name == "measure"]
    checks.append(("siblings in SEPARATE modules still cluster",
                   len(split) == 1
                   and {m.cls.name for m in split[0].methods}
                   == {"InFileA", "InFileB"}))

    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    failed = [label for label, ok in checks if not ok]
    if failed:
        print(f"\n{len(failed)} gate check(s) FAILED — do not trust this tool's "
              f"output until they pass.")
        return 1
    print(f"\nAll {len(checks)} gate checks passed.")
    return 0


def main(argv: list[str]) -> int:
    if "--gate" in argv:
        return gate()

    root = _SRC
    if "--area" in argv:
        root = _SRC / argv[argv.index("--area") + 1]
        if not root.is_dir():
            print(f"No such area: {root}")
            return 1

    found = clusters(root)
    total = sum(c.copies_saved for c in found)
    print(f"{len(found)} duplicate-body cluster(s) under "
          f"{root.relative_to(_SRC.parent.parent)}, "
          f"{total} line(s) of copy\n")
    for cluster in found:
        print(cluster.render())
    if found:
        print("\n`!!` marks a body whose free variables resolve DIFFERENTLY per "
              "sibling —\nthat is the `0` case (base takes the skeleton), not a "
              "pull-up.\nRun --gate to re-prove the tool before trusting these "
              "numbers.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
