"""The desktop-capture fallback chain — ONE chain, and every rung reachable.

Until 2026-09-14 this chain existed **three times**: here in the adapter, in
``ui/gui/screen_capture.py`` (the gui's per-tick grab) and in
``ui/screen_overlay.py`` (the region picker's frozen backdrop).  They had
diverged on the rung that matters — only the UI copies knew about
``gnome-screenshot``, which is the ONLY one of the three tools that works on
GNOME and KDE Wayland (``grim`` is wlroots-only, ``scrot`` is X11).

The user-visible result: on GNOME Wayland a user could freeze the screen in the
region picker, start a screencast, and get a black panel from the CLI, the API
or qtgui — while the gui beside them worked.  Same desktop, same Command,
different answer per face.

These tests pin the rungs by **forcing each one to be the only survivor**, so a
rung that stops being reachable fails here rather than on someone's desktop.
Each also checks the CROP, because a full-screen rung that returns the whole
screen instead of the asked-for region is the failure mode a "did it return
something" assertion cannot see.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from trcc.adapters.screencast import build_screen_capture
from trcc.adapters.screencast.qt import QtScreenCapture
from trcc.core.ports import ScreenCapture

pytest.importorskip("PySide6")

#: A region inset from the origin, so a missing crop shows up as wrong pixels
#: rather than wrong size alone.
REGION = (40, 30, 64, 48)
FULL = (320, 240)
INK = (0, 200, 40)
#: Everything OUTSIDE the region — a crop must not return any of it.
BACKDROP = (200, 0, 0)


@pytest.fixture
def cap() -> QtScreenCapture:
    return QtScreenCapture()


def _png(path: Path, w: int, h: int, rgb: tuple[int, int, int],
         marker: tuple[int, int, int, int] | None = None) -> None:
    """A PNG filled with *rgb*, optionally with INK painted at *marker*.

    The marker is what makes a crop assertion meaningful.  Asserting only the
    returned SIZE cannot see a missing crop: ``_pixmap_to_raw_frame`` resizes
    whatever it is given to the requested dimensions, so an uncropped
    full-screen grab comes back at exactly the right size holding the whole
    desktop squashed into it.  Measured — removing the ``.copy(QRect(...))``
    left all eight tests green until this marker was added.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter
    img = QImage(w, h, QImage.Format.Format_RGB888)
    img.fill(QColor(*rgb))
    if marker is not None:
        painter = QPainter(img)
        painter.fillRect(QRect(*marker), QColor(*INK))
        painter.end()
    img.save(str(path), "PNG")


def _uniform(frame: Any, rgb: tuple[int, int, int]) -> bool:
    """True when every pixel of *frame* is *rgb* (RGB24, no row padding)."""
    px = frame.data
    return all(tuple(px[i:i + 3]) == rgb for i in range(0, len(px), 3))


def _only(tool: str, monkeypatch: pytest.MonkeyPatch,
          size: tuple[int, int]) -> list[list[str]]:
    """Make *tool* the only binary on PATH and have it write a *size* PNG."""
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == tool else None

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        if size == FULL:
            # A whole-desktop grab: BACKDROP everywhere, INK only inside the
            # requested region, so a correct crop returns pure INK and a
            # missing crop returns a mix.
            _png(Path(cmd[-1]), *size, BACKDROP, marker=REGION)
        else:
            _png(Path(cmd[-1]), *size, INK)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("trcc.adapters.screencast.qt.shutil.which", fake_which)
    monkeypatch.setattr("trcc.adapters.screencast.qt.subprocess.run", fake_run)
    # Qt's native grab must not pre-empt the tool under test.
    monkeypatch.setattr(QtScreenCapture, "_qt_grab",
                        lambda self, x, y, w, h: None)
    return calls


# ── the region rungs ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["grim", "scrot"])
def test_region_tools_are_asked_for_the_region(
    cap: QtScreenCapture, monkeypatch: pytest.MonkeyPatch, tool: str,
) -> None:
    """``grim -g`` / ``scrot -a`` take a geometry, so no crop is needed."""
    calls = _only(tool, monkeypatch, (REGION[2], REGION[3]))
    frame = cap.grab_region(*REGION)

    assert len(calls) == 1 and calls[0][0] == tool
    assert " ".join(calls[0]).count(str(REGION[2])) >= 1, (
        f"{tool} was not given the region geometry: {calls[0]}")
    assert (frame.width, frame.height) == (REGION[2], REGION[3])


# ── the rung that was missing ──────────────────────────────────────────────

def test_gnome_screenshot_grabs_full_and_is_cropped(
    cap: QtScreenCapture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GNOME / KDE Wayland rung — full grab, then crop.

    ``gnome-screenshot`` has no scriptable region flag (``-a`` is an
    interactive picker), so it MUST be cropped afterwards.  Returning the
    whole screen would look like a working capture and put the wrong picture
    on the panel.
    """
    calls = _only("gnome-screenshot", monkeypatch, FULL)
    frame = cap.grab_region(*REGION)

    assert len(calls) == 1 and calls[0][0] == "gnome-screenshot"
    assert "-f" in calls[0], "gnome-screenshot needs -f <file>"
    assert (frame.width, frame.height) == (REGION[2], REGION[3])
    assert len(frame.data) == REGION[2] * REGION[3] * 3
    # CONTENT, not size — the assertion that can actually see a missing crop.
    assert _uniform(frame, INK), (
        "full-screen grab was not cropped to the requested region — the "
        "frame carries pixels from outside it")


def test_region_tools_are_preferred_over_the_full_grab(
    cap: QtScreenCapture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters: cropping a whole screen is the expensive last resort."""
    seen: list[str] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}"          # everything is installed

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        seen.append(cmd[0])
        _png(Path(cmd[-1]), REGION[2], REGION[3], INK)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("trcc.adapters.screencast.qt.shutil.which", fake_which)
    monkeypatch.setattr("trcc.adapters.screencast.qt.subprocess.run", fake_run)
    monkeypatch.setattr(QtScreenCapture, "_qt_grab",
                        lambda self, x, y, w, h: None)

    cap.grab_region(*REGION)
    assert seen == ["grim"], f"expected grim first, got {seen}"


# ── nothing available ──────────────────────────────────────────────────────

def test_every_backend_failing_raises_rather_than_returning_black(
    cap: QtScreenCapture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A black frame is indistinguishable from a legitimately dark desktop.

    The port's contract says raise, so the caller can say WHY it stopped
    instead of silently pushing black to the panel.
    """
    monkeypatch.setattr("trcc.adapters.screencast.qt.shutil.which",
                        lambda name: None)
    monkeypatch.setattr(QtScreenCapture, "_qt_grab",
                        lambda self, x, y, w, h: None)
    monkeypatch.setattr(
        "trcc.adapters.screencast.qt.QApplication.primaryScreen",
        staticmethod(lambda: None))

    with pytest.raises(OSError, match="Screen capture failed"):
        cap.grab_region(*REGION)


def test_invalid_region_is_refused(cap: QtScreenCapture) -> None:
    with pytest.raises(OSError, match="Invalid region size"):
        cap.grab_region(0, 0, 0, 100)


# ── the single chooser ─────────────────────────────────────────────────────

def test_one_place_chooses_the_backend() -> None:
    """``build_screen_capture`` is what both the OS and ``ui/gui`` call.

    Two callers, one decision — so a PipeWire backend lands for every face at
    once instead of for whichever one remembered to look for it.
    """
    made = build_screen_capture()
    assert isinstance(made, ScreenCapture)
    assert isinstance(made, QtScreenCapture)


def test_the_os_delegates_to_that_chooser(monkeypatch: pytest.MonkeyPatch) -> None:
    from trcc.adapters.system.linux import LinuxOS

    sentinel = QtScreenCapture()
    monkeypatch.setattr("trcc.adapters.screencast.build_screen_capture",
                        lambda: sentinel)
    assert LinuxOS()._build_screen_capture() is sentinel


# ── the blank-grab trap ───────────────────────────────────────────────


def test_an_offscreen_qt_never_supplies_a_capture(monkeypatch) -> None:
    """An offscreen Qt cannot see a screen, so it must not be asked.

    ``grabWindow`` does NOT fail on the offscreen platform — it returns a
    correctly-sized, non-null, essentially black pixmap.  Every blank test in
    this adapter is a SIZE test (``isNull`` / ``width() <= 1``), so that black
    rectangle passed as a capture and the external tools were never tried.
    MEASURED against ImageMagick ground truth on the same rectangle: offscreen
    scored a mean absolute error of 68.9, native xcb scored 0.0.

    Reachable from every non-GUI face — ``_ensure_qt_app`` forces
    ``QT_QPA_PLATFORM=offscreen`` for headless rendering without asking
    whether a display exists.
    """
    from trcc.adapters.screencast.qt import QtScreenCapture

    cap = QtScreenCapture()
    assert cap._qt_can_grab() is False, (
        "the suite runs offscreen, so Qt must decline — if this passes, Qt is "
        "about to hand the wire a black frame"
    )
    assert cap._qt_grab(0, 0, 64, 64) is None


def test_the_full_screen_fallback_is_guarded_too(monkeypatch) -> None:
    """THE SECOND ROUTE.  Qt is reached twice, and one guard missed it.

    ``_external_grab`` falls back to a Qt FULL-screen grab and crops it.  The
    first version of this guard covered only ``_qt_grab``, so the fallback
    went on serving the same blank pixmap by a different path — the fix
    measured identically broken until both routes asked one predicate.
    """
    from trcc.adapters.screencast.qt import QtScreenCapture

    cap = QtScreenCapture()
    # No external tool may answer, so the ONLY remaining source is the Qt
    # full-screen fallback — which must decline rather than crop a blank.
    monkeypatch.setattr(QtScreenCapture, "_run_tools",
                        lambda self, attempts, tmp_path: None)

    assert cap._external_grab(0, 0, 64, 64) is None, (
        "the full-screen fallback cropped an offscreen grab and returned it"
    )


def test_a_blank_source_raises_instead_of_sending_black() -> None:
    """With nothing able to capture, the answer is an error, not a picture.

    Returning black is worse than failing: the caller sends it to the panel
    and the user sees a dead screencast with no message anywhere.
    """
    import pytest as _pytest

    from trcc.adapters.screencast.qt import QtScreenCapture

    cap = QtScreenCapture()
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(QtScreenCapture, "_run_tools",
                   lambda self, attempts, tmp_path: None)
        with _pytest.raises(OSError, match="Screen capture failed"):
            cap.grab_region(0, 0, 64, 64)


def test_the_region_tools_cover_plain_x11() -> None:
    """``grim`` is wlroots and ``scrot`` is not everywhere.

    A plain X11 desktop with neither — this dev box — had NO region tool at
    all, so capture failed outright once the blank Qt grab stopped being
    accepted.  ``maim`` and ImageMagick's ``import`` are the common X11
    answers; with ``import`` the headless chain measured a mean absolute
    error of 0.00 against ground truth.
    """
    import inspect

    from trcc.adapters.screencast.qt import QtScreenCapture

    src = inspect.getsource(QtScreenCapture._external_grab)
    for tool in ("grim", "scrot", "maim", "import"):
        assert f'"{tool}"' in src, f"{tool} is not in the region chain"
