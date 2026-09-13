"""RegionSelectOverlay — drag a rectangle to choose a screen region.

Lives next to :mod:`screen_overlay` because they share the same frozen-
screenshot base.  Where the eyedropper picks a single pixel, this
overlay returns a rectangle ``(x, y, w, h)`` that the screencast pipe
re-grabs on every tick.

Visuals:

* Whole screen dimmed; the selection rectangle "punches through" with
  the original screenshot.
* A floating size label (``WxH``) tracks the cursor so users can hit
  exact aspect ratios.

Signals:

* :sig:`region_selected(x, y, w, h)` — left-mouse drag completed.
* :sig:`cancelled()` — ESC or right-click.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Signal

from ..screen_overlay import DragSelectOverlay

log = logging.getLogger(__name__)


class RegionSelectOverlay(DragSelectOverlay):
    """Drag out a screen region; emits it as ``(x, y, w, h)``.

    The drag interaction itself lives in :class:`DragSelectOverlay`, shared
    with the gui capture tool — this class only says what the rectangle
    means here: the four numbers that describe it.
    """

    region_selected = Signal(int, int, int, int)
    cancelled = Signal()

    def _emit_cancel(self) -> None:
        log.info("RegionSelectOverlay._emit_cancel: cancelled by user")
        self.cancelled.emit()

    def _confirm(self, sel: QRect) -> None:
        log.info("RegionSelectOverlay._confirm: region %dx%d at (%d, %d)",
                 sel.width(), sel.height(), sel.x(), sel.y())
        self.hide()
        self.region_selected.emit(sel.x(), sel.y(), sel.width(), sel.height())
        self.deleteLater()
