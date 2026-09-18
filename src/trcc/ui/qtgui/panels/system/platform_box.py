"""PlatformBox — the static identity block: distro, install method, paths.

Read once from ``GetPlatformInfo``.  None of it changes while the app runs,
so it is loaded at construction and never ticked.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QFormLayout, QLabel

from .....core.commands import GetPlatformInfo
from ._base import SystemBox

log = logging.getLogger(__name__)


class PlatformBox(SystemBox):
    """Distro / install method / where this install keeps its files."""

    TITLE = "Platform"

    def _build_ui(self) -> None:
        log.debug("_build_ui")
        form = QFormLayout(self)
        self._distro = QLabel("…")
        self._install = QLabel("…")
        self._paths = QLabel("…")
        self._paths.setWordWrap(True)
        form.addRow("Distro:", self._distro)
        form.addRow("Install:", self._install)
        form.addRow("Paths:", self._paths)
        self.refresh()

    def refresh(self) -> None:
        log.debug("refresh")
        r = self.dispatch(GetPlatformInfo())
        log.info("refresh: distro=%s install=%s", r.distro_name, r.install_method)
        self._distro.setText(r.distro_name or "—")
        self._install.setText(r.install_method or "—")
        self._paths.setText(
            f"config: {r.config_dir}\n"
            f"data:   {r.data_dir}\n"
            f"log:    {r.log_file}",
        )
