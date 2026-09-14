"""
Screen Capture Overlay — frozen-screen region selection tool.

- Captures full screen (X11 + Wayland compatible)
- Shows frozen screenshot with dimmed overlay
- User draws selection rectangle
- Cropped region emitted as QImage

Works on both X11 and Wayland via fallback chain.

This header used to claim it "matches Windows FormScreenshot".  It does
not, and 2.1.6 says so: ``FormScreenshot`` is a borderless *viewfinder*
you drag around — ``MouseMove`` moves the whole Form and ``MouseUp``
reports only ``Left``/``Top``, which the caller writes into the screencast
panel's X and Y boxes.  Its width and height come from the panel, locked
to the LCD's aspect, so the Windows user chooses position and never size.
Grabbing a screen region as an IMAGE has no counterpart there at all —
2.1.6 loads images from file.  Only the gesture is shared with qtgui's
region picker, which is why only the gesture lives in
:class:`DragSelectOverlay`.

The frozen-screen primitives this builds on — ``grab_full_screen``,
``is_wayland`` and ``BaseScreenOverlay`` — live in ``ui/screen_overlay``
and are shared with the qtgui skin.  What stays here is region capture:
``ScreenCaptureOverlay``, which says what a dragged rectangle means here.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Signal

from ..screen_overlay import DragSelectOverlay

log = logging.getLogger(__name__)


class ScreenCaptureOverlay(DragSelectOverlay):
    """Drag out a screen region; emits it as a cropped :class:`QPixmap`.

    Usage::

        overlay = ScreenCaptureOverlay()
        overlay.captured.connect(on_captured)
        overlay.show()

    The drag interaction itself lives in :class:`DragSelectOverlay`, shared
    with the qtgui region picker — this class only says what the rectangle
    means here: the pixels inside it.
    """

    captured = Signal(object)  # QPixmap, or None when cancelled

    def _emit_cancel(self) -> None:
        log.info("ScreenCaptureOverlay._emit_cancel: cancelled by user")
        self.captured.emit(None)

    def _confirm(self, sel: QRect) -> None:
        log.info("ScreenCaptureOverlay._confirm: cropping %dx%d at (%d, %d)",
                 sel.width(), sel.height(), sel.x(), sel.y())
        self.hide()
        try:
            self.captured.emit(self._screenshot.copy(sel))
        except Exception:
            log.exception("ScreenCaptureOverlay._confirm: crop failed")
            self.captured.emit(None)
        self.deleteLater()
