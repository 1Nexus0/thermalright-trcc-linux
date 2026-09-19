"""DashboardBox — the 4-row-per-panel sensor grid the LCD overlay reads.

Persisted as ``<config_dir>/system_config.json``.  Until
``Get/SetSensorDashboard`` existed the GUI imported the persistence adapter
directly, so cli / api / qtgui could not read the file at all.

The working layout is held here and nothing persists until the user saves:
``SetSensorDashboard`` takes the WHOLE layout in one bulk verb, matching
``SetOverlayConfig``, rather than a rebind per row.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .....core.commands import GetSensorDashboard, SetSensorDashboard
from ...sensor_picker import SensorPickerWidget
from ._base import SystemBox

log = logging.getLogger(__name__)

#: Shown in the Sensor column when a row is bound to nothing.  An empty cell
#: is indistinguishable from a column that failed to populate.
_UNBOUND = "— unbound —"


class DashboardBox(SystemBox):
    """Bind each dashboard row to a sensor, then save the layout."""

    TITLE = "Dashboard layout"
    STRETCH = 1

    def _build_ui(self) -> None:
        log.debug("_build_ui")
        layout = QVBoxLayout(self)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Panel / row", "Sensor", "Unit"])
        self._tree.itemSelectionChanged.connect(self._on_row_picked)
        layout.addWidget(self._tree, 1)

        self._picker = SensorPickerWidget(self._app, self._bus, self)
        layout.addWidget(self._picker, 1)

        row = QHBoxLayout()
        bind = QPushButton("Bind selected row", self)
        bind.clicked.connect(self._on_bind)
        row.addWidget(bind)
        save = QPushButton("Save layout", self)
        save.clicked.connect(self._on_save)
        row.addWidget(save)
        row.addStretch(1)
        self._status = QLabel("", self)
        row.addWidget(self._status)
        layout.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        """Read the layout and show it.

        The Result hands out COPIES, so the working layout is held here and
        nothing persists until ``SetSensorDashboard`` -- which is also what
        happens over the daemon socket, where JSON hands back fresh objects.
        """
        log.debug("refresh")
        r = self.dispatch(GetSensorDashboard())
        self._panels = list(r.panels)
        log.info("refresh: %d panel(s), %d row(s) auto-mapped",
                 len(self._panels), getattr(r, "auto_mapped", 0))
        self._tree.clear()
        for p_i, panel in enumerate(self._panels):
            top = QTreeWidgetItem([panel.name, "", ""])
            top.setData(0, Qt.ItemDataRole.UserRole, (p_i, -1))
            for s_i, binding in enumerate(panel.sensors):
                child = QTreeWidgetItem(
                    [binding.label, binding.sensor_id or _UNBOUND,
                     binding.unit],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, (p_i, s_i))
                top.addChild(child)
            self._tree.addTopLevelItem(top)
        self._tree.expandAll()

    def _selected_binding(self) -> tuple[int, int] | None:
        """The (panel, row) a ROW is selected on — never a panel header."""
        items = self._tree.selectedItems()
        log.debug("_selected_binding: %d item(s) selected", len(items))
        if not items:
            return None
        addr = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not addr or addr[1] < 0:
            return None
        return addr

    def _on_row_picked(self) -> None:
        """Point the picker at whatever the selected row is already bound to."""
        addr = self._selected_binding()
        if addr is None:
            log.debug("_on_row_picked: no row selected")
            return
        p_i, s_i = addr
        current = self._panels[p_i].sensors[s_i].sensor_id
        log.debug("_on_row_picked: panel=%d row=%d current=%s",
                  p_i, s_i, current)
        if current:
            self._picker.select_sensor_id(current)

    def _on_bind(self) -> None:
        log.info("_on_bind")
        addr = self._selected_binding()
        if addr is None:
            log.warning("_on_bind: a panel heading has no row address")
            self._status.setText("Pick a row, not a panel heading.")
            return
        picked = self._picker.selected_sensor()
        if picked is None:
            log.warning("_on_bind: no sensor selected")
            self._status.setText("Pick a sensor first.")
            return
        sensor_id, label = picked
        p_i, s_i = addr
        binding = self._panels[p_i].sensors[s_i]
        binding.sensor_id = sensor_id
        if not binding.label:
            binding.label = label
        log.info("_on_bind: panel=%d row=%d -> %s", p_i, s_i, sensor_id)
        self._status.setText(f"{label} — unsaved")
        self._redraw_selected_row(sensor_id)

    def _redraw_selected_row(self, sensor_id: str) -> None:
        log.debug("_redraw_selected_row: sensor_id=%s", sensor_id)
        items = self._tree.selectedItems()
        if items:
            items[0].setText(1, sensor_id or _UNBOUND)

    def _on_save(self) -> None:
        """Persist the WHOLE layout — one bulk verb, not a per-row rebind."""
        log.info("_on_save: %d panel(s)", len(self._panels))
        r = self.dispatch(SetSensorDashboard(panels=tuple(self._panels)))
        self._status.setText(r.message)
        if r.ok:
            self.refresh()
