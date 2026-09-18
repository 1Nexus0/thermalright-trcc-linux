"""GpuBox — which GPU the metric sources read.

Degrades to a disabled "No GPU detected" rather than an error: a box with
no discrete or integrated GPU is a supported state, not a fault.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton

from .....core.commands import ListGpus, SetGpuDevice
from ._base import SystemBox

log = logging.getLogger(__name__)


class GpuBox(SystemBox):
    """Pick the GPU whose readings feed the overlay."""

    TITLE = "Metric source"

    def _build_ui(self) -> None:
        log.debug("_build_ui")
        form = QFormLayout(self)
        self._combo = QComboBox(self)
        self._apply_btn = QPushButton("Use this GPU", self)
        self._apply_btn.clicked.connect(self._on_apply)
        row = QHBoxLayout()
        row.addWidget(self._combo, stretch=1)
        row.addWidget(self._apply_btn)
        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        form.addRow("GPU:", row)
        form.addRow("", self._status)
        self._populate()

    def _populate(self) -> None:
        """Fill the combo from ListGpus — self-healing on every call."""
        log.debug("_populate")
        result = self.dispatch(ListGpus())
        self._combo.clear()
        if not result.ok or not result.gpus:
            log.info("_populate: no GPU reported — control disabled")
            self._combo.addItem("No GPU detected", userData=None)
            self._combo.setEnabled(False)
            self._apply_btn.setEnabled(False)
            return
        log.info("_populate: %d GPU(s)", len(result.gpus))
        self._combo.setEnabled(True)
        self._apply_btn.setEnabled(True)
        for gpu in result.gpus:
            tag = "discrete" if gpu.is_discrete else "integrated"
            self._combo.addItem(f"{gpu.name} ({tag})", userData=gpu.key)

    def _on_apply(self) -> None:
        gpu_key = self._combo.currentData()
        if gpu_key is None:
            log.debug("_on_apply: nothing selected")
            return
        log.info("_on_apply: gpu_key=%s", gpu_key)
        r = self.dispatch(SetGpuDevice(gpu_key=str(gpu_key)))
        self._status.setText(r.message)
