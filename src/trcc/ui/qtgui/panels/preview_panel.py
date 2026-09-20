"""PreviewPanel — the selected device's state, beside the always-on preview.

The rendered image is NOT here any more.  It lives in
:class:`~trcc.ui.qtgui.preview_surface.PreviewSurface`, which the window keeps
on screen permanently, so this panel would otherwise be a second copy of it:
a second ``BuildPreview`` on a second timer, rendering the same frame twice.
What is left is the read-out that has no place on the image itself.

Honest scope: only LCD devices have a renderable screen — for LED devices use
the LED panel.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ....core.commands import LcdSnapshot
from ..base import BasePanel

log = logging.getLogger(__name__)

_REFRESH_MS = 1000


class PreviewPanel(BasePanel):
    """State read-out for the device the window is editing."""

    def _setup_ui(self) -> None:
        log.debug("_setup_ui")
        state_box = QGroupBox("State", self)
        state_form = QFormLayout(state_box)
        self._theme_label = QLabel("—", state_box)
        self._size_label = QLabel("—", state_box)
        self._orientation_label = QLabel("—", state_box)
        self._brightness_label = QLabel("—", state_box)
        state_form.addRow("Theme:", self._theme_label)
        state_form.addRow("Render size:", self._size_label)
        state_form.addRow("Orientation:", self._orientation_label)
        state_form.addRow("Brightness:", self._brightness_label)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.addWidget(state_box)
        root.addStretch(1)

        self._selection.changed.connect(lambda _key: self._refresh())
        self.start_periodic_updates(_REFRESH_MS, self._refresh)
        self._refresh()

    def set_render_size(self, width: int, height: int) -> None:
        """Adopt the size of the render the surface just produced.

        Fed by :sig:`PreviewSurface.rendered` rather than measured here: the
        row used to show the SCALED pixmap's size under the label "Render
        size", so a 1600x720 panel read "480x216".
        """
        log.debug("set_render_size: %dx%d", width, height)
        self._size_label.setText(f"{width}×{height}" if width else "—")

    def _refresh(self) -> None:
        log.debug("_refresh")
        key = self.device_key
        if not key:
            self._theme_label.setText("—")
            return
        snapshot = self.dispatch(LcdSnapshot(key=key))
        if not snapshot.ok:
            log.debug("_refresh: no snapshot for %s — %s", key, snapshot.message)
            self._theme_label.setText(f"(no data for {key})")
            return
        self._theme_label.setText(snapshot.current_theme or "(no theme loaded)")
        self._orientation_label.setText(f"{snapshot.orientation}°")
        self._brightness_label.setText(f"{snapshot.brightness}%")
