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
``grab_screen_region`` (the screencast timer's per-tick grab) and
``ScreenCaptureOverlay``, which says what a dragged rectangle means here.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import QRect, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from ..screen_overlay import DragSelectOverlay, grab_full_screen

log = logging.getLogger(__name__)


def grab_screen_region(x: int, y: int, w: int, h: int) -> QPixmap:
    """Capture a specific screen region. X11 + Wayland compatible.

    Called repeatedly by the screencast timer (~150ms interval), so this
    needs to be reasonably efficient.

    X11: QScreen.grabWindow(0, x, y, w, h) captures the region directly.
    Wayland: grim with -g geometry flag, or full capture + crop fallback.

    Returns:
        QPixmap of the region, or null pixmap on failure.
    """
    # Try Qt native capture with region (works on X11)
    if (screen := QApplication.primaryScreen()):
        pixmap = screen.grabWindow(0, x, y, w, h)  # type: ignore[arg-type]
        if not pixmap.isNull() and pixmap.width() > 1:
            return pixmap

    # Wayland fallback: grim with -g region flag
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmp_path = f.name

    try:
        geometry = f"{x},{y} {w}x{h}"
        for cmd in [
            ['grim', '-g', geometry, tmp_path],            # Wayland (wlroots)
            ['scrot', '-a', f'{x},{y},{w},{h}', tmp_path], # X11 fallback
        ]:
            tool = cmd[0]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=2)
                if result.returncode == 0 and Path(tmp_path).stat().st_size > 0:
                    pixmap = QPixmap(tmp_path)
                    if not pixmap.isNull():
                        log.debug("Region capture via %s", tool)
                        return pixmap
                else:
                    log.debug("Region capture tool %s failed (exit %d)", tool, result.returncode)
            except FileNotFoundError:
                log.debug("Region capture tool %s not installed", tool)
            except subprocess.TimeoutExpired:
                log.warning("Region capture tool %s timed out", tool)

        # Last resort: full screen capture + crop
        full = grab_full_screen()
        if not full.isNull():
            return full.copy(x, y, w, h)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    return QPixmap()


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
