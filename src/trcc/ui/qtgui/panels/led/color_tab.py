"""ColorTab — global LED colour + brightness + presets.

Surfaces the :class:`ColorWheel` widget (from G2) alongside the
classic 8 preset buttons and three RGB spinboxes.  Brightness lives
here too because users adjust it in the same workflow.  Apply
dispatches :class:`SetLedColor` and :class:`SetLedBrightness`;
On/Off dispatches :class:`ToggleLed`.

Multi-zone devices use :class:`ZoneTab` for per-zone colour; this tab
edits the *global* colour (used in STATIC/BREATHING/COLORFUL modes
and as the zone-sync default).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

from .....core.commands import SetLedBrightness, SetLedColor, ToggleLed
from .....core.led_models import PRESET_COLORS
from .....core.results import LedSnapshotResult
from ...color_wheel import ColorWheel
from ._base import LedTabBase

log = logging.getLogger(__name__)

#: The preset colour a swatch carries, so its slot can be a named method
#: instead of a closure over the loop variable.
_PRESET_PROPERTY = "trcc_preset_rgb"


class ColorTab(LedTabBase):
    """Global colour + brightness + on/off + presets."""

    def __init__(self, app, key_provider, parent=None) -> None:
        log.debug("__init__: app=%s key_provider=%s", app, key_provider)
        super().__init__(app, key_provider, parent)
        self._color = QColor(255, 0, 0)
        self._build_ui()

    def _build_ui(self) -> None:
        # Colour wheel + selector swatch
        log.debug("_build_ui")
        self._wheel = ColorWheel(self)
        self._wheel.setMinimumSize(220, 220)
        self._wheel.hue_changed.connect(self._on_wheel_hue)

        self._swatch = QLabel(self)
        self._swatch.setFixedSize(80, 30)
        self._swatch.setAutoFillBackground(True)
        self._update_swatch()

        # RGB spinboxes
        self._r = self._make_spin()
        self._g = self._make_spin()
        self._b = self._make_spin()
        self._r.valueChanged.connect(self._on_rgb_changed)
        self._g.valueChanged.connect(self._on_rgb_changed)
        self._b.valueChanged.connect(self._on_rgb_changed)

        rgb_row = QHBoxLayout()
        rgb_row.addWidget(QLabel("R", self))
        rgb_row.addWidget(self._r)
        rgb_row.addSpacing(6)
        rgb_row.addWidget(QLabel("G", self))
        rgb_row.addWidget(self._g)
        rgb_row.addSpacing(6)
        rgb_row.addWidget(QLabel("B", self))
        rgb_row.addWidget(self._b)
        rgb_row.addStretch(1)

        # Brightness slider
        self._brightness = QSlider(Qt.Orientation.Horizontal, self)
        self._brightness.setRange(0, 100)
        self._brightness.setValue(65)
        self._brightness_label = QLabel("65%", self)
        self._brightness.valueChanged.connect(self._on_brightness_slid)

        brightness_row = QHBoxLayout()
        brightness_row.addWidget(self._brightness, stretch=1)
        brightness_row.addWidget(self._brightness_label)

        # 8 preset buttons in a 2×4 grid
        preset_box = QGroupBox("Presets", self)
        preset_grid = QGridLayout(preset_box)
        preset_grid.setSpacing(4)
        for i, (r, g, b) in enumerate(PRESET_COLORS):
            btn = QPushButton(self)
            btn.setFixedSize(40, 28)
            btn.setStyleSheet(
                f"background-color: rgb({r},{g},{b}); border: 1px solid #333;",
            )
            btn.setToolTip(f"#{r:02x}{g:02x}{b:02x}")
            # The colour rides ON the button rather than in a closure over the
            # loop variable, so one named slot serves all eight presets —
            # ``feedback_no_lambdas``.
            btn.setProperty(_PRESET_PROPERTY, (r, g, b))
            btn.clicked.connect(self._on_preset_clicked)
            preset_grid.addWidget(btn, i // 4, i % 4)

        # Apply / On / Off
        self._apply_btn = QPushButton("Apply colour", self)
        self._apply_btn.clicked.connect(self._on_apply)
        self._on_btn = QPushButton("On", self)
        self._on_btn.clicked.connect(self._on_global_on)
        self._off_btn = QPushButton("Off", self)
        self._off_btn.clicked.connect(self._on_global_off)

        button_row = QHBoxLayout()
        button_row.addWidget(self._apply_btn)
        button_row.addStretch(1)
        button_row.addWidget(self._on_btn)
        button_row.addWidget(self._off_btn)

        form = QFormLayout()
        form.addRow("RGB:", rgb_row)
        form.addRow("Brightness:", brightness_row)

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self._wheel)
        top.addWidget(self._swatch, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(top)
        root.addLayout(form)
        root.addWidget(preset_box)
        root.addLayout(button_row)
        root.addStretch(1)

    # ── Public ────────────────────────────────────────────────────────

    def refresh_from(self, snapshot: LedSnapshotResult | None) -> None:
        log.debug("refresh_from")
        if snapshot is None:
            return
        self._set_color(*snapshot.color, emit_signals=False)
        self._brightness.blockSignals(True)
        self._brightness.setValue(snapshot.brightness)
        self._brightness.blockSignals(False)
        self._brightness_label.setText(f"{snapshot.brightness}%")

    # ── Internals ─────────────────────────────────────────────────────

    def _on_brightness_slid(self, value: int) -> None:
        """Echo the slider position beside it.  Not the apply path."""
        log.debug("_on_brightness_slid: value=%s", value)
        self._brightness_label.setText(f"{value}%")

    def _on_preset_clicked(self) -> None:
        """A preset swatch was pressed — read its colour off the button."""
        button = self.sender()
        rgb = None if button is None else button.property(_PRESET_PROPERTY)
        log.info("_on_preset_clicked: rgb=%s", rgb)
        if rgb is not None:
            self._set_color(*rgb)

    def _make_spin(self) -> QSpinBox:
        log.debug("_make_spin")
        spin = QSpinBox(self)
        spin.setRange(0, 255)
        spin.setFixedWidth(60)
        return spin

    def _update_swatch(self) -> None:
        log.debug("_update_swatch")
        self._swatch.setStyleSheet(
            f"background-color: {self._color.name()}; "
            "border: 1px solid #333;",
        )

    def _set_color(
        self, r: int, g: int, b: int, emit_signals: bool = True,
    ) -> None:
        log.debug("_set_color: r=%s g=%s", r, g)
        self._color = QColor(r, g, b)
        self._update_swatch()
        # Sync RGB spinboxes without re-emitting.
        for box, value in ((self._r, r), (self._g, g), (self._b, b)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        # Sync the wheel's hue indicator.
        self._wheel.set_hue(self._color.hue() if self._color.hue() >= 0 else 0)
        if emit_signals:
            pass  # No external signal — consumer reads on Apply.

    def _on_wheel_hue(self, hue: int) -> None:
        # Wheel always emits a saturated, fully-bright pixel.
        log.info("_on_wheel_hue: hue=%s", hue)
        c = QColor.fromHsv(hue, 255, 255)
        self._set_color(c.red(), c.green(), c.blue(), emit_signals=False)

    def _on_rgb_changed(self) -> None:
        log.info("_on_rgb_changed")
        self._set_color(
            self._r.value(), self._g.value(), self._b.value(),
            emit_signals=False,
        )

    # ── Command dispatch ──────────────────────────────────────────────

    def _on_apply(self) -> None:
        log.info("_on_apply")
        key = self.current_key()
        if not key:
            return
        rgb = (self._color.red(), self._color.green(), self._color.blue())
        self._dispatch(SetLedColor(key=key, color=rgb))
        self._dispatch(SetLedBrightness(
            key=key, percent=self._brightness.value(),
        ))

    def _on_global_on(self) -> None:
        log.info("_on_global_on")
        key = self.current_key()
        if key:
            self._dispatch(ToggleLed(key=key, on=True))

    def _on_global_off(self) -> None:
        log.info("_on_global_off")
        key = self.current_key()
        if key:
            self._dispatch(ToggleLed(key=key, on=False))
