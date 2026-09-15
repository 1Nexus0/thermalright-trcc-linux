"""Screen-capture adapters.

One port (:class:`ScreenCapture`) with backends that grab a region of the
user's desktop on demand: the Qt-backed adapter for X11, and the
PipeWire / xdg-portal backend for Wayland -- which lived in ``ui/gui`` until
2026-09-15 and so reached only one of the four faces.

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
from pathlib import Path

from ...core.ports import ScreenCapture
from .pipewire import PIPEWIRE_AVAILABLE, PipeWireScreenCapture
from .qt import QtScreenCapture

log = logging.getLogger(__name__)

__all__ = ("PipeWireScreenCapture", "QtScreenCapture",
           "build_screen_capture")


def build_screen_capture(config_dir: Path | None = None) -> ScreenCapture:
    """The desktop-capture backend for this session.

    ``QtScreenCapture`` degrades internally — Qt native, then ``grim`` /
    ``scrot`` / ``maim`` / ``import``, then ``gnome-screenshot`` cropped —
    so it answers on every X11 desktop rather than on an OS's behalf.  On
    Wayland every one of those returns black, so the portal backend wraps it:
    the session starts in the background and the Qt chain answers until it is
    up, which is the policy the gui has used all along.

    Composed unconditionally rather than behind a Wayland test.  The adapter
    self-guards — no PyGObject, or a portal that refuses, and every call is
    simply the fallback — so there is no environment to sniff here and an X11
    session pays one attribute check per grab.
    """
    log.info("build_screen_capture: PipeWireScreenCapture(available=%s) over "
             "QtScreenCapture (Qt native → grim → scrot → maim → import → "
             "gnome-screenshot+crop → full-grab+crop) config_dir=%s",
             PIPEWIRE_AVAILABLE, config_dir)
    return PipeWireScreenCapture(QtScreenCapture(), config_dir=config_dir)
