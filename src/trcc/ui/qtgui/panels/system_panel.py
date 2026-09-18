"""SystemPanel — host for the system boxes.

Stacks one :class:`~.system.SystemBox` per concern and owns two things no
box can own alone: the vertical layout, and the single periodic timer.

Each box states its own ``TITLE`` and ``STRETCH``, so adding one is a line
in ``_setup_ui`` and nothing else.  The timer is wired to exactly ONE named
method — ``SensorsBox.refresh_live`` — rather than looped over every box, so
``ListMemorySlots`` (which shells out to ``dmidecode``) cannot drift onto a
two-second tick and the log a reporter pastes gains one record per tick
rather than six.

Replaces the 28-method monolith this file used to hold: qtgui's only god
class, and a 65% outlier in its own skin.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout

from ..base import BasePanel
from .system import (
    DashboardBox,
    GpuBox,
    HealthBox,
    MaintenanceBox,
    PlatformBox,
    SensorsBox,
)

log = logging.getLogger(__name__)

_SENSOR_REFRESH_MS = 2000


class SystemPanel(BasePanel):
    """Live system readout + diagnostic actions."""

    def _setup_ui(self) -> None:
        log.debug("_setup_ui")
        self._platform = PlatformBox(self.app, self)
        self._gpu = GpuBox(self.app, self)
        self._maintenance = MaintenanceBox(self.app, self)
        self._health = HealthBox(self.app, self)
        self._sensors = SensorsBox(self.app, self)
        self._dash = DashboardBox(self.app, self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        for box in (self._platform, self._gpu, self._maintenance,
                    self._health, self._sensors, self._dash):
            outer.addWidget(box, box.STRETCH)
        outer.addLayout(self._build_action_row())

        log.info("_setup_ui: six boxes built; ticking sensors every %d ms",
                 _SENSOR_REFRESH_MS)
        self.start_periodic_updates(
            _SENSOR_REFRESH_MS, self._sensors.refresh_live,
        )

    def _build_action_row(self) -> QHBoxLayout:
        """The two health actions, kept below the boxes they act on."""
        log.debug("_build_action_row")
        row = QHBoxLayout()
        refresh = QPushButton("Re-run health check", self)
        refresh.clicked.connect(self._health.refresh)
        row.addWidget(refresh)

        report = QPushButton("Save bug report…", self)
        report.clicked.connect(self._health.save_debug_report)
        row.addWidget(report)

        row.addStretch(1)
        return row
