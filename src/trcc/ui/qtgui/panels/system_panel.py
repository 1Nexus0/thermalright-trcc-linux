"""SystemPanel — platform info + sensor readouts + doctor + debug report.

Reads:
* ``GetPlatformInfo`` for the static identity block;
* ``ReadSensors`` on a 2-second timer for live CPU/GPU/temp values;
* ``RunHealthCheck`` on demand for the doctor row.

Writes (via Commands):
* ``GenerateDebugReport`` when the "Save bug report…" button is clicked
  → opens a save-as dialog and writes the bundle.

Demonstrates the BasePanel pattern: dispatch Commands, use the timer
helper, lay out widgets without business logic.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ....core.commands import (
    CheckForUpdate,
    ControlCenterSnapshot,
    DisableAutostart,
    EnableAutostart,
    GenerateDebugReport,
    GetAutostartStatus,
    GetPlatformInfo,
    GetSensorDashboard,
    ListGpus,
    ListMemorySlots,
    ReadSensors,
    RunHealthCheck,
    RunUpgrade,
    SetGpuDevice,
    SetHddEnabled,
    SetSensorDashboard,
)
from ....core.models import AUTOSTART_TARGETS, DEFAULT_AUTOSTART_TARGET
from ..base import BasePanel
from ..sensor_picker import SensorPickerWidget

log = logging.getLogger(__name__)

_SENSOR_REFRESH_MS = 2000


class SystemPanel(BasePanel):
    """Live system readout + diagnostic actions."""

    def _setup_ui(self) -> None:
        log.debug("_setup_ui")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        outer.addWidget(self._build_platform_box())
        outer.addWidget(self._build_gpu_box())
        outer.addWidget(self._build_maintenance_box())
        outer.addWidget(self._build_health_box())
        outer.addWidget(self._build_sensors_box(), 1)
        outer.addWidget(self._build_dashboard_box(), 1)
        outer.addLayout(self._build_action_row())

        self._refresh_platform()
        self._refresh_health()
        self._refresh_sensors()
        self._refresh_dashboard()
        self._refresh_memory()
        self._refresh_hdd()
        self.start_periodic_updates(_SENSOR_REFRESH_MS, self._refresh_sensors)

    # ── Widget builders ───────────────────────────────────────────────

    def _build_platform_box(self) -> QGroupBox:
        log.debug("_build_platform_box")
        box = QGroupBox("Platform", self)
        form = QFormLayout(box)
        self._distro_label = QLabel("…")
        self._install_label = QLabel("…")
        self._paths_label = QLabel("…")
        self._paths_label.setWordWrap(True)
        form.addRow("Distro:", self._distro_label)
        form.addRow("Install:", self._install_label)
        form.addRow("Paths:", self._paths_label)
        return box

    def _build_gpu_box(self) -> QGroupBox:
        log.debug("_build_gpu_box")
        box = QGroupBox("Metric source", self)
        form = QFormLayout(box)
        self._gpu_combo = QComboBox(box)
        self._set_gpu_btn = QPushButton("Use this GPU", box)
        self._set_gpu_btn.clicked.connect(self._on_set_gpu)
        row = QHBoxLayout()
        row.addWidget(self._gpu_combo, stretch=1)
        row.addWidget(self._set_gpu_btn)
        self._gpu_status = QLabel("", box)
        self._gpu_status.setWordWrap(True)
        form.addRow("GPU:", row)
        form.addRow("", self._gpu_status)
        self._populate_gpus()
        return box

    def _build_maintenance_box(self) -> QGroupBox:
        log.debug("_build_maintenance_box")
        box = QGroupBox("Maintenance", self)
        form = QFormLayout(box)
        self._autostart_check = QCheckBox("Start TRCC on login", box)
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        # WHICH ui starts.  All four can; a boolean could only ever mean gui.
        # The list comes from the ONE registry — a UI that spelled the targets
        # itself would be a second source to drift from it.
        self._autostart_target = QComboBox(box)
        for name in sorted(AUTOSTART_TARGETS):
            self._autostart_target.addItem(name, name)
        self._autostart_target.currentIndexChanged.connect(
            self._on_autostart_target_changed,
        )
        self._update_btn = QPushButton("Check for updates", box)
        self._update_btn.clicked.connect(self._on_check_update)
        # Checking told the user a newer release exists and then offered no way
        # to get it -- cli has ``trcc system upgrade`` and api has the route,
        # so qtgui users were the only ones who had to leave the app.
        self._upgrade_btn = QPushButton("Upgrade now…", box)
        self._upgrade_btn.clicked.connect(self._on_upgrade)
        self._maint_status = QLabel("", box)
        self._maint_status.setWordWrap(True)
        self._maint_status.setTextFormat(Qt.TextFormat.RichText)
        self._maint_status.setOpenExternalLinks(True)
        form.addRow(self._autostart_check)
        form.addRow("Start:", self._autostart_target)
        form.addRow(self._update_btn)
        form.addRow(self._upgrade_btn)
        form.addRow(self._maint_status)
        self._refresh_autostart()
        return box

    def _build_health_box(self) -> QGroupBox:
        log.debug("_build_health_box")
        box = QGroupBox("Health", self)
        layout = QVBoxLayout(box)
        self._health_summary = QLabel("Running checks…", box)
        bold = QFont()
        bold.setBold(True)
        self._health_summary.setFont(bold)
        layout.addWidget(self._health_summary)

        self._health_details = QLabel("", box)
        self._health_details.setWordWrap(True)
        self._health_details.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._health_details)
        return box

    def _build_sensors_box(self) -> QGroupBox:
        log.debug("_build_sensors_box")
        box = QGroupBox("Sensors (live)", self)
        layout = QVBoxLayout(box)
        # Whether disk metrics reach sensor broadcasts at all.  Spinning a
        # sleeping disk to read its temperature is a real cost, which is why
        # the toggle exists rather than being always-on.
        self._hdd_check = QCheckBox("Include HDD metrics in broadcasts", box)
        self._hdd_check.toggled.connect(self._on_hdd_toggled)
        layout.addWidget(self._hdd_check)

        self._sensors_list = QListWidget(box)
        self._sensors_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection,
        )
        layout.addWidget(self._sensors_list)

        # DRAM identity is per-OS by nature: only Linux enriches with SPD/IMC
        # timings, so an absent field arrives as "" and is rendered "NC" --
        # the convention ``Platform.memory_info`` documents.
        self._memory_list = QListWidget(box)
        self._memory_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection,
        )
        self._memory_list.setMaximumHeight(90)
        layout.addWidget(QLabel("Memory slots:", box))
        layout.addWidget(self._memory_list)
        return box

    def _build_dashboard_box(self) -> QGroupBox:
        """The 4-row-per-panel sensor grid the LCD overlay reads.

        Persisted as ``<config_dir>/system_config.json``.  Until
        ``Get/SetSensorDashboard`` existed the GUI imported the persistence
        adapter directly, so cli / api / qtgui could not read the file at all.
        """
        log.debug("_build_dashboard_box")
        box = QGroupBox("Dashboard layout", self)
        layout = QVBoxLayout(box)

        self._dash_tree = QTreeWidget(box)
        self._dash_tree.setColumnCount(3)
        self._dash_tree.setHeaderLabels(["Panel / row", "Sensor", "Unit"])
        self._dash_tree.itemSelectionChanged.connect(self._on_dash_row_picked)
        layout.addWidget(self._dash_tree, 1)

        self._dash_picker = SensorPickerWidget(self._app, box)
        layout.addWidget(self._dash_picker, 1)

        row = QHBoxLayout()
        bind = QPushButton("Bind selected row", box)
        bind.clicked.connect(self._on_bind_sensor)
        row.addWidget(bind)
        save = QPushButton("Save layout", box)
        save.clicked.connect(self._on_save_dashboard)
        row.addWidget(save)
        row.addStretch(1)
        self._dash_status = QLabel("", box)
        row.addWidget(self._dash_status)
        layout.addLayout(row)
        return box

    def _refresh_dashboard(self) -> None:
        """Read the layout and show it.

        The Result hands out COPIES, so the working layout is held here and
        nothing persists until ``SetSensorDashboard`` -- which is also what
        happens over the daemon socket, where JSON hands back fresh objects.
        """
        r = self.dispatch(GetSensorDashboard())
        self._dashboard = list(r.panels)
        log.info("_refresh_dashboard: %d panel(s), %d row(s) auto-mapped",
                 len(self._dashboard), getattr(r, "auto_mapped", 0))
        self._dash_tree.clear()
        for p_i, panel in enumerate(self._dashboard):
            top = QTreeWidgetItem([panel.name, "", ""])
            top.setData(0, Qt.ItemDataRole.UserRole, (p_i, -1))
            for s_i, binding in enumerate(panel.sensors):
                child = QTreeWidgetItem(
                    [binding.label, binding.sensor_id or "— unbound —",
                     binding.unit],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, (p_i, s_i))
                top.addChild(child)
            self._dash_tree.addTopLevelItem(top)
        self._dash_tree.expandAll()

    def _on_dash_row_picked(self) -> None:
        """Point the picker at whatever the selected row is already bound to."""
        addr = self._selected_binding()
        if addr is None:
            return
        p_i, s_i = addr
        current = self._dashboard[p_i].sensors[s_i].sensor_id
        log.debug("_on_dash_row_picked: panel=%d row=%d current=%s",
                  p_i, s_i, current)
        if current:
            self._dash_picker.select_sensor_id(current)

    def _selected_binding(self) -> tuple[int, int] | None:
        """The (panel, row) a ROW is selected on — never a panel header."""
        items = self._dash_tree.selectedItems()
        log.debug("_selected_binding: %d item(s) selected", len(items))
        if not items:
            return None
        addr = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not addr or addr[1] < 0:
            return None
        return addr

    def _on_bind_sensor(self) -> None:
        log.info("_on_bind_sensor")
        addr = self._selected_binding()
        if addr is None:
            self._dash_status.setText("Pick a row, not a panel heading.")
            return
        picked = self._dash_picker.selected_sensor()
        if picked is None:
            self._dash_status.setText("Pick a sensor first.")
            return
        sensor_id, label = picked
        p_i, s_i = addr
        binding = self._dashboard[p_i].sensors[s_i]
        binding.sensor_id = sensor_id
        if not binding.label:
            binding.label = label
        log.info("_on_bind_sensor: panel=%d row=%d -> %s", p_i, s_i, sensor_id)
        self._dash_status.setText(f"{label} — unsaved")
        self._redraw_selected_row(sensor_id)

    def _redraw_selected_row(self, sensor_id: str) -> None:
        log.debug("_redraw_selected_row: sensor_id=%s", sensor_id)
        items = self._dash_tree.selectedItems()
        if items:
            items[0].setText(1, sensor_id or "— unbound —")

    def _on_save_dashboard(self) -> None:
        """Persist the WHOLE layout — one bulk verb, not a per-row rebind."""
        log.info("_on_save_dashboard: %d panel(s)", len(self._dashboard))
        r = self.dispatch(SetSensorDashboard(panels=tuple(self._dashboard)))
        self._dash_status.setText(r.message)
        if r.ok:
            self._refresh_dashboard()

    def _build_action_row(self) -> QHBoxLayout:
        log.debug("_build_action_row")
        row = QHBoxLayout()
        refresh = QPushButton("Re-run health check", self)
        refresh.clicked.connect(self._refresh_health)
        row.addWidget(refresh)

        report = QPushButton("Save bug report…", self)
        report.clicked.connect(self._save_debug_report)
        row.addWidget(report)

        row.addStretch(1)
        return row

    # ── Refreshers ────────────────────────────────────────────────────

    def _refresh_platform(self) -> None:
        log.debug("_refresh_platform")
        r = self.dispatch(GetPlatformInfo())
        self._distro_label.setText(r.distro_name or "—")
        self._install_label.setText(r.install_method or "—")
        self._paths_label.setText(
            f"config: {r.config_dir}\n"
            f"data:   {r.data_dir}\n"
            f"log:    {r.log_file}",
        )

    def _refresh_health(self) -> None:
        log.debug("_refresh_health")
        r = self.dispatch(RunHealthCheck())
        self._health_summary.setText(r.message)
        details: list[str] = []
        for check in r.checks:
            line = f"[{check.severity:4}] {check.name:22} {check.message}"
            details.append(line)
            if check.fix_hint and check.severity != "OK":
                details.append(f"         hint: {check.fix_hint}")
        self._health_details.setText("\n".join(details))

    def _refresh_sensors(self) -> None:
        log.debug("_refresh_sensors")
        r = self.dispatch(ReadSensors())
        self._sensors_list.clear()
        for reading in r.readings:
            text = (
                f"{reading.sensor_id:30}  "
                f"{reading.value:>10.2f} {reading.unit:<6}  "
                f"({reading.category})"
            )
            item = QListWidgetItem(text)
            self._sensors_list.addItem(item)

    # ── Actions ───────────────────────────────────────────────────────

    def _populate_gpus(self) -> None:
        """Fill the GPU combo from ListGpus — self-healing, and degrades to
        a disabled 'No GPU detected' when none are present (a supported
        state, not an error)."""
        log.debug("_populate_gpus")
        result = self.dispatch(ListGpus())
        self._gpu_combo.clear()
        if not result.ok or not result.gpus:
            self._gpu_combo.addItem("No GPU detected", userData=None)
            self._gpu_combo.setEnabled(False)
            self._set_gpu_btn.setEnabled(False)
            return
        self._gpu_combo.setEnabled(True)
        self._set_gpu_btn.setEnabled(True)
        for gpu in result.gpus:
            tag = "discrete" if gpu.is_discrete else "integrated"
            self._gpu_combo.addItem(f"{gpu.name} ({tag})", userData=gpu.key)

    def _on_set_gpu(self) -> None:
        gpu_key = self._gpu_combo.currentData()
        if gpu_key is None:
            return
        log.info("_on_set_gpu: gpu_key=%s", gpu_key)
        r = self.dispatch(SetGpuDevice(gpu_key=str(gpu_key)))
        self._gpu_status.setText(r.message)

    def _refresh_autostart(self) -> None:
        log.debug("_refresh_autostart")
        r = self.dispatch(GetAutostartStatus())
        self._autostart_check.blockSignals(True)
        self._autostart_check.setChecked(r.enabled)
        self._autostart_check.blockSignals(False)
        # Show what is INSTALLED, not what the widget last showed — the entry
        # is the record, and another surface may have changed it.
        index = self._autostart_target.findData(r.target or DEFAULT_AUTOSTART_TARGET)
        if index >= 0:
            self._autostart_target.blockSignals(True)
            self._autostart_target.setCurrentIndex(index)
            self._autostart_target.blockSignals(False)

    def _on_autostart_toggled(self, checked: bool) -> None:
        target = self._autostart_target.currentData()
        log.info("_on_autostart_toggled: checked=%s target=%s", checked, target)
        r = self.dispatch(
            EnableAutostart(target=target) if checked else DisableAutostart(),
        )
        self._maint_status.setText(r.message)
        self._refresh_autostart()

    def _on_autostart_target_changed(self, index: int) -> None:
        """Re-install for the newly chosen target, but only if it is ON.

        Changing the picker while autostart is disabled must not enable it —
        the same invariant every ``refresh`` holds.
        """
        target = self._autostart_target.itemData(index)
        log.info("_on_autostart_target_changed: target=%s", target)
        if not self._autostart_check.isChecked():
            log.debug("_on_autostart_target_changed: disabled — not installing")
            return
        r = self.dispatch(EnableAutostart(target=target))
        self._maint_status.setText(r.message)

    def _on_check_update(self) -> None:
        log.info("_on_check_update")
        r = self.dispatch(CheckForUpdate())
        if not r.ok:
            self._maint_status.setText(f"Update check failed: {r.message}")
            return
        if r.latest_version and r.latest_version != r.local_version:
            self._maint_status.setText(
                f"Update available: {r.latest_version} "
                f"(you have {r.local_version}). "
                f'<a href="{r.release_url}">Release notes</a> — '
                'press "Upgrade now…" to install it.',
            )
        else:
            self._maint_status.setText(f"Up to date ({r.local_version}).")

    def _on_upgrade(self) -> None:
        """Run the package-manager upgrade, after showing exactly what runs.

        ``RunUpgrade`` shells out through the system package manager under
        sudo, so it is confirmed first -- the CLI refuses the same Command
        without ``--yes`` for this reason.  ``dry_run`` asks the Command
        itself what it WOULD run, so the confirmation quotes the real command
        line instead of a UI's guess at it.
        """
        log.info("_on_upgrade: asking the Command what it would run")
        preview = self.dispatch(RunUpgrade(dry_run=True))
        if not preview.ok:
            self._maint_status.setText(f"Upgrade unavailable: {preview.message}")
            return
        answer = QMessageBox.question(
            self, "Upgrade TRCC",
            f"{preview.message}\n\nThis runs as root. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            log.info("_on_upgrade: declined by the user")
            self._maint_status.setText("Upgrade cancelled.")
            return
        log.info("_on_upgrade: confirmed — running")
        r = self.dispatch(RunUpgrade(dry_run=False))
        self._maint_status.setText(
            r.message if r.ok else f"Upgrade failed: {r.message}",
        )

    def _refresh_hdd(self) -> None:
        """Show the PERSISTED flag, not whatever the widget last showed.

        ``blockSignals`` because ``setChecked`` emits ``toggled``, and an
        unguarded load would dispatch a write on every refresh -- the setting
        would then be whatever the UI happened to render, not what the user
        chose.  Same shape as ``_refresh_autostart``.
        """
        log.debug("_refresh_hdd")
        snap = self.dispatch(ControlCenterSnapshot())
        self._hdd_check.blockSignals(True)
        self._hdd_check.setChecked(bool(snap.hdd_enabled))
        self._hdd_check.blockSignals(False)

    def _on_hdd_toggled(self, checked: bool) -> None:
        log.info("_on_hdd_toggled: enabled=%s", checked)
        r = self.dispatch(SetHddEnabled(enabled=checked))
        if not r.ok:
            log.warning("_on_hdd_toggled: refused — %s", r.message)

    def _refresh_memory(self) -> None:
        """List the DRAM slots.  Absent per-OS fields render as NC."""
        log.debug("_refresh_memory")
        r = self.dispatch(ListMemorySlots())
        self._memory_list.clear()
        for slot in r.slots:
            parts = [slot.locator or "NC", slot.size or "NC",
                     slot.speed or "NC", slot.manufacturer or "NC"]
            self._memory_list.addItem(QListWidgetItem("  ".join(parts)))
        if not r.slots:
            self._memory_list.addItem(QListWidgetItem("No DRAM slots reported"))

    def _save_debug_report(self) -> None:
        log.debug("_save_debug_report")
        default_name = "trcc-debug-report.txt"
        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            "Save TRCC debug report",
            default_name,
            "Text files (*.txt);;All files (*.*)",
        )
        if not path_str:
            return
        out = Path(path_str)
        r = self.dispatch(GenerateDebugReport(output_path=out, log_tail_lines=1000))
        if r.ok:
            self._health_summary.setText(
                f"Debug report saved to {r.output_path}",
            )
        else:
            self._health_summary.setText(
                f"Debug report failed: {r.message}",
            )
