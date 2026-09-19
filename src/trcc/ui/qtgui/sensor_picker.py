"""SensorPickerWidget — reusable categorized sensor selector.

Used by the overlay element editor when a user adds a metric element,
and by future panels that need "pick a sensor" affordances (LED linked-
mode sensor source, status panel filters, etc.).

Shape (built so non-technical users can find what they want fast):

* Top: a search box that filters by sensor id, label, or category.
* Middle: a 2-pane layout — categories on the left (CPU / GPU / Memory
  / Disk / Net / Fan / …), sensors in the picked category on the right.
* Bottom: a live preview line showing the currently-selected sensor's
  current value, so users see real data before they commit.

Emits ``selected(sensor_id, label)`` so callers can grab both the ID
(stable, used by the metric element) and the label (display only).
"""
from __future__ import annotations

import logging
from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...app import App
from ...core.commands import ReadSensors
from ..qt_periodic import PeriodicUpdater

log = logging.getLogger(__name__)

_REFRESH_MS = 2000


class SensorPickerWidget(QWidget):
    """A live, searchable sensor browser.

    Designed for embedding in dialogs (overlay editor) and panels
    (status, configuration).  Reads via ``ReadSensors`` on a 2-second
    refresh so the preview value reflects what the overlay will show.
    """

    selected = Signal(str, str)  # (sensor_id, label)

    def __init__(self, app: App, parent: QWidget | None = None) -> None:
        log.debug("__init__: app=%s parent=%s", app, parent)
        super().__init__(parent)
        self._app = app
        self._all_readings: list = []
        self._build()
        self._refresh()
        # A ``PeriodicUpdater`` rather than a bare ``QTimer``, for the suspend
        # /resume pair below: this widget is embedded in a panel
        # (``dashboard_box``) and a dialog (``overlay_editor``), so it goes off
        # screen with its host while its timer kept dispatching ``ReadSensors``
        # -- measured 0.48/s with every host hidden, after the BasePanel hooks
        # had already silenced the other poller.
        self._updates = PeriodicUpdater(self)
        self._updates.start(_REFRESH_MS, self._tick)

    def _build(self) -> None:
        log.debug("_build")
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(
            "Search by name, label, or category (e.g. 'cpu temp')…",
        )
        self._search.textChanged.connect(self._on_search_changed)

        self._category_list = QListWidget(self)
        self._category_list.setMaximumWidth(180)
        self._category_list.currentTextChanged.connect(self._on_category_changed)

        self._sensor_list = QListWidget(self)
        self._sensor_list.currentItemChanged.connect(self._on_sensor_changed)
        self._sensor_list.itemDoubleClicked.connect(self._emit_selection)

        twopane = QHBoxLayout()
        twopane.addWidget(self._category_list)
        twopane.addWidget(self._sensor_list, stretch=1)

        self._preview = QLabel(
            "Pick a sensor to see its current value.", self,
        )
        self._preview.setWordWrap(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._search)
        root.addLayout(twopane, stretch=1)
        root.addWidget(self._preview)

    # ── Public ────────────────────────────────────────────────────────

    def selected_sensor(self) -> tuple[str, str] | None:
        """Return (sensor_id, label) for the current selection, or None."""
        log.debug("selected_sensor")
        item = self._sensor_list.currentItem()
        if item is None:
            return None
        sid = str(item.data(Qt.ItemDataRole.UserRole))
        label = str(item.data(Qt.ItemDataRole.UserRole + 1))
        return (sid, label)

    def select_sensor_id(self, sensor_id: str) -> None:
        """Programmatically pick the row for ``sensor_id`` (if loaded)."""
        log.debug("select_sensor_id: sensor_id=%s", sensor_id)
        for i in range(self._sensor_list.count()):
            item = self._sensor_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == sensor_id:
                self._sensor_list.setCurrentItem(item)
                return

    # ── Internals ─────────────────────────────────────────────────────

    def _tick(self) -> None:
        """The TIMER's callback — skips while nothing can see the result.

        **The gate is on the SCHEDULE, never on ``_refresh`` itself.**  An
        earlier version put ``isVisible()`` inside ``_refresh`` and broke three
        existing tests, correctly: ``_refresh`` is also the EXPLICIT refresh
        path, called from ``__init__`` to populate the list and directly by
        callers who want it now.  Gating the operation made a manual refresh
        silently do nothing.

        **And it is ``isVisible()`` rather than a hide/show hook, which is a
        measurement, not a preference.**  This widget is a GRANDCHILD
        (``SensorPickerWidget < DashboardBox < SystemPanel < QStackedWidget``)
        and its timer starts in ``__init__``.  A panel that has never been the
        stack's current widget was never shown, so there is no hide event to
        react to.  Driven in the real skin with ``AboutPanel`` showing, the
        instrumented widget reported ``isVisible=False timer_active=True``
        and was still dispatching ``ReadSensors`` at 0.52/s; with this gate it
        reports 0.00/s.  ``BasePanel`` CAN use hooks, because a panel is the
        stack's DIRECT child and the stack hides it explicitly.
        """
        if not self.isVisible():
            log.debug("_tick: skipped — no ancestor on screen")
            return
        self._refresh()

    def _refresh(self) -> None:
        log.debug("_refresh")
        result = self._app.dispatch(ReadSensors())
        self._all_readings = list(result.readings)
        self._rebuild_categories()
        self._update_preview()

    def _rebuild_categories(self) -> None:
        log.debug("_rebuild_categories")
        seen = set()
        cats = []
        for reading in self._all_readings:
            if reading.category and reading.category not in seen:
                seen.add(reading.category)
                cats.append(reading.category)
        cats.sort()
        cats.insert(0, "All")

        current = self._category_list.currentItem()
        current_text = current.text() if current else "All"
        self._category_list.blockSignals(True)
        self._category_list.clear()
        for cat in cats:
            self._category_list.addItem(QListWidgetItem(cat))
        # Re-select the previous category (or "All" if it's gone).
        for i in range(self._category_list.count()):
            if self._category_list.item(i).text() == current_text:
                self._category_list.setCurrentRow(i)
                break
        else:
            self._category_list.setCurrentRow(0)
        self._category_list.blockSignals(False)
        self._rebuild_sensor_list()

    def _rebuild_sensor_list(self) -> None:
        log.debug("_rebuild_sensor_list")
        category_item = self._category_list.currentItem()
        category = category_item.text() if category_item else "All"
        query = self._search.text().strip().lower()

        # Preserve current selection across rebuilds when possible.
        previous = self.selected_sensor()

        self._sensor_list.blockSignals(True)
        self._sensor_list.clear()
        for reading in self._all_readings:
            if category != "All" and reading.category != category:
                continue
            if query and not _matches(reading, query):
                continue
            text = (
                f"{reading.label or reading.sensor_id}    "
                f"{reading.value:>10.2f} {reading.unit}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, reading.sensor_id)
            item.setData(
                Qt.ItemDataRole.UserRole + 1,
                reading.label or reading.sensor_id,
            )
            self._sensor_list.addItem(item)
        if previous is not None:
            self.select_sensor_id(previous[0])
        self._sensor_list.blockSignals(False)

    def _update_preview(self) -> None:
        log.debug("_update_preview")
        picked = self.selected_sensor()
        if picked is None:
            return
        sid = picked[0]
        for reading in self._all_readings:
            if reading.sensor_id == sid:
                self._preview.setText(
                    f"{reading.label or sid}: "
                    f"{reading.value:.2f} {reading.unit} "
                    f"({reading.category})",
                )
                return

    # ── Signal handlers ───────────────────────────────────────────────

    def _on_category_changed(self, _text: str) -> None:
        log.info("_on_category_changed: _text=%s", _text)
        self._rebuild_sensor_list()

    def _on_search_changed(self, _text: str) -> None:
        log.info("_on_search_changed: _text=%s", _text)
        self._rebuild_sensor_list()

    def _on_sensor_changed(
        self, _current: QListWidgetItem, _previous: QListWidgetItem,
    ) -> None:
        log.info("_on_sensor_changed")
        self._update_preview()
        self._emit_selection()

    def _emit_selection(self, *_args) -> None:
        log.debug("_emit_selection")
        picked = self.selected_sensor()
        if picked is not None:
            sid, label = picked
            self.selected.emit(sid, label)


def _matches(reading, query: str) -> bool:
    """Case-insensitive substring match across id, label, category."""
    log.debug("_matches: reading=%s query=%s", reading, query)
    haystack = " ".join((
        reading.sensor_id,
        reading.label or "",
        reading.category or "",
    )).lower()
    return query in haystack


# Defaultdict imported for future grouping logic — keeping it available
# without "unused import" hassle.
_ = defaultdict
