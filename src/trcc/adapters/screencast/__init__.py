"""Screen-capture adapters.

One port (:class:`ScreenCapture`) with backend(s) that grab a region of the
user's desktop on demand.  Today we ship the Qt-backed adapter; a
PipeWire / xdg-portal backend can land here behind the same port.

:func:`build_screen_capture` is the ONE place a backend is chosen.  Two
callers need that choice and neither may reach the other:

* ``BaseOS._build_screen_capture`` — for everything that captures through
  ``platform.screen_capture()`` (the CLI, the API, the screencast driver).
* ``ui/gui`` — which cannot ask ``app.platform`` at all: under
  ``TRCC_DAEMON=1`` the window holds an ``AppProxy`` that exposes
  ``dispatch`` alone.

That is not merely a workaround.  **The screen being captured belongs to the
session the UI is displayed in**, which in daemon mode is a different session
from the one that owns USB — the daemon may have no display at all.  So a UI
building its own local capture source is the correct ownership, and sharing
this function is what keeps "which backend" a single decision.
"""
import logging

from ...core.ports import ScreenCapture
from .qt import QtScreenCapture

log = logging.getLogger(__name__)

__all__ = ("QtScreenCapture", "build_screen_capture")


def build_screen_capture() -> ScreenCapture:
    """The desktop-capture backend for this session.

    One backend today.  ``QtScreenCapture`` already degrades internally —
    Qt native, then ``grim`` / ``scrot`` / ``gnome-screenshot`` where they
    exist, then a full grab cropped — so it answers on every desktop rather
    than on an OS's behalf.
    """
    log.info("build_screen_capture: QtScreenCapture (Qt native → grim → "
             "scrot → gnome-screenshot+crop → full-grab+crop)")
    return QtScreenCapture()
