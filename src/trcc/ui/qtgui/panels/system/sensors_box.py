"""SensorsBox — the live readings, the HDD toggle, and the DRAM slots.

**This is the only box on a timer.**  :meth:`refresh_live` is what
:class:`SystemPanel` hands to ``start_periodic_updates``, and it reads
``ReadSensors`` and nothing else.

The HDD flag and the DRAM slot list are loaded ONCE, in ``_build_ui``, and
deliberately stay out of :meth:`refresh_live`: ``ListMemorySlots`` shells
out to ``dmidecode`` under a privilege helper, so folding it onto the tick
would run a root subprocess every two seconds for a list that cannot change
without opening the case.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QCheckBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from .....core.commands import (
    ControlCenterSnapshot,
    ListMemorySlots,
    ReadSensors,
    SetHddEnabled,
)
from ._base import SystemBox

log = logging.getLogger(__name__)

#: Height cap on the DRAM list — a few slots, not a scrolling panel.
_MEMORY_LIST_HEIGHT = 90


class SensorsBox(SystemBox):
    """Live hardware readings, with the two settings that shape them."""

    TITLE = "Sensors (live)"
    STRETCH = 1

    def _build_ui(self) -> None:
        log.debug("_build_ui")
        layout = QVBoxLayout(self)
        # Whether disk metrics reach sensor broadcasts at all.  Spinning a
        # sleeping disk to read its temperature is a real cost, which is why
        # the toggle exists rather than being always-on.
        self._hdd_check = QCheckBox("Include HDD metrics in broadcasts", self)
        self._hdd_check.toggled.connect(self._on_hdd_toggled)
        layout.addWidget(self._hdd_check)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(self._list)

        # DRAM identity is per-OS by nature: only Linux enriches with SPD/IMC
        # timings, so an absent field arrives as "" and is rendered "NC" --
        # the convention ``Platform.memory_info`` documents.
        self._memory = QListWidget(self)
        self._memory.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._memory.setMaximumHeight(_MEMORY_LIST_HEIGHT)
        layout.addWidget(QLabel("Memory slots:", self))
        layout.addWidget(self._memory)

        self.refresh_hdd()
        self.refresh_memory()
        self.refresh_live()

    def refresh_live(self) -> None:
        """The ONE method on the two-second timer.  Readings only."""
        log.debug("refresh_live")
        r = self.dispatch(ReadSensors())
        self._list.clear()
        for reading in r.readings:
            self._list.addItem(QListWidgetItem(
                f"{reading.sensor_id:30}  "
                f"{reading.value:>10.2f} {reading.unit:<6}  "
                f"({reading.category})",
            ))

    def refresh_hdd(self) -> None:
        """Show the PERSISTED flag, not whatever the widget last showed.

        ``blockSignals`` because ``setChecked`` emits ``toggled``, and an
        unguarded load would dispatch a write on every refresh -- the setting
        would then be whatever the UI happened to render, not what the user
        chose.  Same shape as ``MaintenanceBox.refresh``.
        """
        log.debug("refresh_hdd")
        snap = self.dispatch(ControlCenterSnapshot())
        log.info("refresh_hdd: enabled=%s", snap.hdd_enabled)
        self._hdd_check.blockSignals(True)
        self._hdd_check.setChecked(bool(snap.hdd_enabled))
        self._hdd_check.blockSignals(False)

    def refresh_memory(self) -> None:
        """List the DRAM slots.  Absent per-OS fields render as NC.

        NOT on the tick — see the module docstring.
        """
        log.debug("refresh_memory")
        r = self.dispatch(ListMemorySlots())
        log.info("refresh_memory: %d slot(s)", len(r.slots))
        self._memory.clear()
        for slot in r.slots:
            parts = [slot.locator or "NC", slot.size or "NC",
                     slot.speed or "NC", slot.manufacturer or "NC"]
            self._memory.addItem(QListWidgetItem("  ".join(parts)))
        if not r.slots:
            self._memory.addItem(QListWidgetItem("No DRAM slots reported"))

    def _on_hdd_toggled(self, checked: bool) -> None:
        log.info("_on_hdd_toggled: enabled=%s", checked)
        r = self.dispatch(SetHddEnabled(enabled=checked))
        if not r.ok:
            log.warning("_on_hdd_toggled: refused — %s", r.message)
