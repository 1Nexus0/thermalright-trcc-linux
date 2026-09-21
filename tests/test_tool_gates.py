"""Run every ``dev/tools`` self-test — a gate nobody runs is not a gate.

The tools in ``dev/tools`` are the instruments this project plans against: the
god-class count, the silent-function ratchet, the duplicate clusters, the
contract bypasses.  Several carry a ``--gate`` that re-proves the tool against
known answers in BOTH directions, so that a number can be trusted without
trusting its author.

**Measured 2026-09-20: nine tools define a ``gate()``, and the test suite
invoked exactly zero of them.**  Every gate ran only when a human remembered to
type it — which is the "prose, checked by nobody" failure the ratchets exist to
prevent, one level up.  A miscounting tool ratchets the wrong number forever
and stays green; a gate that catches it but never runs changes nothing.  That is
not hypothetical: a sibling ratchet once counted ``QMessageBox.warning`` as a
log line, and because a ratchet only ever moves DOWN, the false positive lowered
the bar permanently.

Discovery is **derived, never listed** — any tool that grows a ``gate()``
is picked up the day it lands, so this file cannot fall behind the directory it
describes.  The floor below guards the denominator: a discovery bug that found
nothing would leave this file green while testing not one thing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DEV = _ROOT / "dev"
_DEV_TOOLS = _DEV / "tools"

#: Searched recursively under ``dev/``, not just ``dev/tools/``.  The C# oracle
#: audits live in ``dev/decompiler/`` and none carries a ``gate()`` today — but
#: scoping discovery to one directory is how a gate added in the other would
#: run nowhere, which is the exact failure this file was written about.

#: Gates that must NOT run here, with the cause.  Not a convenience list — each
#: entry is a tool whose gate cannot answer in a test process, and the name is
#: asserted to still exist so a rename cannot silently drop both the exclusion
#: and the tool.
_EXCLUDED = {
    # CLAUDE.md, "Release": this one re-proves each package-manager channel
    # against a live index.  It is ONLINE by design, "so it cannot be a test
    # and cannot run in CI", and takes ~100 s.  It runs at release time.
    "check_program_deps.py": "online — queries live package indexes",
}

#: A floor, not a target — nine today.  See ``ui_contract._MIN_CONTRACT``: a
#: collector that silently returns little makes everything look complete.
_MIN_GATES = 8


def _gated_tools() -> list[Path]:
    """Every tool exposing a ``gate()``, minus the ones that cannot run here."""
    return sorted(
        path
        for path in _DEV.rglob("*.py")
        if "__pycache__" not in path.parts
        and path.name not in _EXCLUDED
        and "\ndef gate(" in path.read_text(encoding="utf-8")
    )


def test_discovery_finds_the_gates_that_exist() -> None:
    """The denominator: discovery that found nothing would test nothing."""
    found = _gated_tools()
    assert len(found) >= _MIN_GATES, (
        f"only {len(found)} gated tool(s) discovered in {_DEV_TOOLS}, under the "
        f"floor of {_MIN_GATES} — discovery is broken, and every gate below "
        f"would silently stop running while this file stayed green.  Found: "
        f"{[p.name for p in found]}"
    )


@pytest.mark.parametrize("name, reason", sorted(_EXCLUDED.items()))
def test_excluded_tools_still_exist(name: str, reason: str) -> None:
    """A rename must not drop the exclusion AND the tool in one silent move."""
    assert (_DEV_TOOLS / name).is_file(), (
        f"{name} is excluded from the gate run ({reason}) but no longer "
        f"exists — either restore it or drop the exclusion, because an "
        f"exclusion naming nothing hides the next tool that takes the name"
    )


@pytest.mark.parametrize(
    "tool", _gated_tools(), ids=lambda p: p.stem,
)
def test_tool_gate_passes(tool: Path) -> None:
    """Run the tool's own ``--gate`` and require a clean exit.

    A subprocess rather than an in-process call: that is how a human runs it,
    these modules are scripts that mutate ``sys.path`` on import, and several
    share top-level names that would collide in one interpreter.
    """
    result = subprocess.run(
        [sys.executable, str(tool), "--gate"],
        cwd=_ROOT,
        env={"PYTHONPATH": str(_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"{tool.name} --gate FAILED (exit {result.returncode}).  The tool does "
        f"not do what it claims, so any number it prints is not evidence.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
