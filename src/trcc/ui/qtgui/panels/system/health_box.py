"""HealthBox — the doctor row, plus the bug-report bundle.

The report writes its outcome into this box's summary line, which is why
the two live together: ``GenerateDebugReport`` is the escalation path from
a failing check, not a separate feature.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFileDialog, QLabel, QVBoxLayout

from .....core.commands import GenerateDebugReport, RunHealthCheck
from ._base import SystemBox

log = logging.getLogger(__name__)

#: How much of the log the bundle carries.  Enough to hold a whole session.
_LOG_TAIL_LINES = 1000


class HealthBox(SystemBox):
    """What ``trcc doctor`` reports, and the bundle a reporter attaches."""

    TITLE = "Health"

    def _build_ui(self) -> None:
        log.debug("_build_ui")
        layout = QVBoxLayout(self)
        self._summary = QLabel("Running checks…", self)
        bold = QFont()
        bold.setBold(True)
        self._summary.setFont(bold)
        layout.addWidget(self._summary)

        self._details = QLabel("", self)
        self._details.setWordWrap(True)
        self._details.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._details)
        self.refresh()

    def refresh(self) -> None:
        """Re-run every check and render the result."""
        log.debug("refresh")
        r = self.dispatch(RunHealthCheck())
        log.info("refresh: %d check(s) — %s", len(r.checks), r.message)
        self._summary.setText(r.message)
        details: list[str] = []
        for check in r.checks:
            details.append(
                f"[{check.severity:4}] {check.name:22} {check.message}",
            )
            if check.fix_hint and check.severity != "OK":
                details.append(f"         hint: {check.fix_hint}")
        self._details.setText("\n".join(details))

    def save_debug_report(self) -> None:
        """Ask where, then write the bundle and say where it landed."""
        log.debug("save_debug_report")
        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            "Save TRCC debug report",
            "trcc-debug-report.txt",
            "Text files (*.txt);;All files (*.*)",
        )
        if not path_str:
            log.info("save_debug_report: cancelled")
            return
        r = self.dispatch(GenerateDebugReport(
            output_path=Path(path_str), log_tail_lines=_LOG_TAIL_LINES,
        ))
        log.info("save_debug_report: ok=%s path=%s", r.ok, r.output_path)
        self._summary.setText(
            f"Debug report saved to {r.output_path}" if r.ok
            else f"Debug report failed: {r.message}",
        )
