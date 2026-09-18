"""Screen-capture adapters.

One port (:class:`ScreenCapture`) with links that grab a region of the user's
desktop on demand -- Qt's own grab, external screenshot programs, and the
PipeWire / xdg-portal stream -- and :func:`build_screen_capture`, the ONE
place they are composed into a chain, from the display session the OS
adapter reports.

``BaseOS._build_screen_capture`` is its one caller: everything that captures
does so through ``Platform.screen_capture()`` -- the CLI, the API, the
screencast driver, and since 2026-09-18 the gui window, which asks the HOST
Platform it was started with rather than ``app.platform``.

That distinction is ownership, not a workaround.  **The screen being captured
belongs to the session the UI is displayed in**, which in daemon mode is a
different session from the one that owns USB -- the daemon may have no display
at all, and ``AppProxy`` exposes ``dispatch`` alone.  The window's own Platform
object is that session, so the port answers for it; until 2026-09-18 the
window imported this function directly for the same reason and bypassed the
port that existed for it.
"""
import logging
import re
from pathlib import Path

from ...core.models import DisplayServer, DisplaySession
from ...core.ports import ScreenCapture
from .pipewire import PIPEWIRE_AVAILABLE, PipeWireScreenCapture
from .qt import (
    GNOME_SCREENSHOT,
    GRIM,
    SPECTACLE,
    WAYLAND_TOOLS,
    X11_TOOLS,
    QtNativeCapture,
    ToolCapture,
    ToolSpec,
)

log = logging.getLogger(__name__)

__all__ = ("PipeWireScreenCapture", "QtNativeCapture", "ToolCapture",
           "build_screen_capture")

#: Desktops whose compositor speaks wlroots' screencopy protocol, which is
#: what ``grim`` needs.  Named by their ``XDG_CURRENT_DESKTOP`` token.
_WLROOTS_DESKTOPS = frozenset({"sway", "hyprland", "river", "wayfire", "labwc"})


def build_screen_capture(
    session: DisplaySession, config_dir: Path | None = None,
) -> ScreenCapture:
    """The desktop-capture chain for *session*.

    * Native windowing (Windows, macOS): Qt's grab is the whole answer.
    * X11, and a headless process: Qt's grab first, then the X11 grabbers
      where they exist.
    * Wayland: the portal stream, with the ONE tool this desktop lends
      behind it.  Qt's grab is never composed here -- the compositor hands a
      client only its own surfaces -- and neither are the X11 grabbers,
      which see Xwayland alone.

    Until 2026-09-18 one fixed chain served every session, so Plasma ran an
    X11 grabber that rang the bell on each call and a wlroots tool that
    failed every tick, on every tick.
    """
    match session.server:
        case DisplayServer.NATIVE:
            made: ScreenCapture = QtNativeCapture()
        case DisplayServer.WAYLAND:
            made = PipeWireScreenCapture(
                ToolCapture(_wayland_tools(session.desktops)),
                config_dir=config_dir, token_name=_token_name(session))
        case _:
            made = QtNativeCapture(then=ToolCapture(X11_TOOLS))
    log.info("build_screen_capture: %s desktops=%s -> %s (pipewire "
             "available=%s) config_dir=%s", session.server.value,
             session.desktops, _describe(made), PIPEWIRE_AVAILABLE, config_dir)
    return made


def _wayland_tools(desktops: tuple[str, ...]) -> tuple[ToolSpec, ...]:
    """The screenshot tool this Wayland desktop trusts, or all of them when
    the desktop is not one we know."""
    if "kde" in desktops:
        tools: tuple[ToolSpec, ...] = (SPECTACLE,)
    elif "gnome" in desktops:
        tools = (GNOME_SCREENSHOT,)
    elif _WLROOTS_DESKTOPS.intersection(desktops):
        tools = (GRIM,)
    else:
        tools = WAYLAND_TOOLS
    log.debug("_wayland_tools: %s -> %s", desktops, [t.name for t in tools])
    return tools


def _token_name(session: DisplaySession) -> str:
    """The restore-token file for this desktop.

    A token is meaningful only to the portal backend that issued it, and the
    backend is chosen per desktop -- one file for all of them meant that
    switching between GNOME and KDE asked again every time, each overwriting
    the other's grant.
    """
    key = re.sub(r"[^a-z0-9]+", "-", "-".join(session.desktops)).strip("-")
    name = f"{PipeWireScreenCapture.TOKEN_FILE}.{key or 'unknown'}"
    log.debug("_token_name: %s -> %s", session.desktops, name)
    return name


def _describe(chain: ScreenCapture) -> str:
    """``PipeWireScreenCapture(ToolCapture[spectacle])``, for the log line."""
    log.debug("_describe: %s", type(chain).__name__)
    match chain:
        case PipeWireScreenCapture():
            return f"PipeWireScreenCapture({_describe(chain._fallback)})"
        case QtNativeCapture():
            nxt = chain._then
            return ("QtNativeCapture" if nxt is None
                    else f"QtNativeCapture({_describe(nxt)})")
        case ToolCapture():
            return f"ToolCapture[{', '.join(t.name for t in chain.tools)}]"
    return type(chain).__name__
