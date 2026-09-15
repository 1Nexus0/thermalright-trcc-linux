"""The Wayland backend, behind the stateless port.

The portal handshake itself cannot be tested here — it needs a compositor that
provides ``org.freedesktop.portal.ScreenCast`` and a human to approve the
consent dialog.  What CAN be tested is everything wrapped around it, which is
where a break would come from: the portal internals moved out of ``ui/gui``
byte-for-byte, the wrapper is new.

So the session is injected.  Every test below drives a fake one and asserts
the POLICY: serve the fallback until the portal is up, switch when it is, and
never block a caller on a consent dialog.
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from trcc.adapters.screencast import pipewire as pw
from trcc.adapters.screencast.pipewire import (
    PipeWireScreenCapture,
    crop_rgb24,
    unpad_rows,
)
from trcc.core.models import RawFrame
from trcc.core.ports import ScreenCapture


class _Fallback(ScreenCapture):
    """Stands in for the Qt chain; says who answered."""

    def __init__(self) -> None:
        self.calls = 0

    def grab_region(self, x: int, y: int, width: int, height: int) -> RawFrame:
        self.calls += 1
        return RawFrame(data=b"\x01\x02\x03" * (width * height),
                        width=width, height=height)


class _Session:
    """A portal session that is exactly as ready as the test says."""

    def __init__(self, running: bool = False, frame: Any = None,
                 start_result: bool = True) -> None:
        self.is_running = running
        self._frame = frame
        self._start_result = start_result
        self.started = threading.Event()
        self.stopped = False

    def start(self, timeout: float = 30.0) -> bool:
        self.started.set()
        self.is_running = self._start_result
        return self._start_result

    def grab_frame(self) -> Any:
        return self._frame

    def stop(self) -> None:
        self.stopped = True
        self.is_running = False


def _capture(session: _Session, fallback: _Fallback) -> PipeWireScreenCapture:
    cap = PipeWireScreenCapture(fallback, session_factory=lambda: session)
    return cap


# ── the stride bug ────────────────────────────────────────────────────


def test_padded_rows_are_unpadded() -> None:
    """GStreamer pads each row; a consumer assuming ``width * 3`` shears.

    MEASURED with ``GstVideo.VideoInfo`` on a real GStreamer 1.28: a
    1366-wide RGB frame has a stride of 4100 against ``width * 3 == 4098``.
    Two bytes a row, 768 rows — every row starts further left than the one
    above it and the picture leans.
    """
    w, h, stride = 1366, 4, 4100
    padded = b"".join(bytes([r]) * (w * 3) + b"\xEE\xEE" for r in range(h))

    tight = unpad_rows(padded, w, h, stride)

    assert len(tight) == w * 3 * h
    assert b"\xEE" not in tight, "padding survived into the pixels"
    assert [tight[r * w * 3] for r in range(h)] == list(range(h)), (
        "rows are misaligned — this is the diagonal shear"
    )


def test_an_already_tight_buffer_is_returned_unchanged() -> None:
    """The common case must cost one comparison, not a rebuild."""
    w, h = 64, 4
    tight = b"\x7f" * (w * 3 * h)

    assert unpad_rows(tight, w, h, w * 3) is tight


# ── the crop ──────────────────────────────────────────────────────────


def test_a_region_is_cut_out_of_the_full_screen_frame() -> None:
    """The portal hands back a whole screen; the port asked for a rectangle."""
    w, h = 8, 4
    data = bytes([(x + y * w) % 256 for y in range(h) for x in range(w)
                  for _ in range(3)])

    f = crop_rgb24(data, w, h, 2, 1, 3, 2)

    assert (f.width, f.height) == (3, 2)
    assert f.data == bytes([10, 10, 10, 11, 11, 11, 12, 12, 12,
                            18, 18, 18, 19, 19, 19, 20, 20, 20])


def test_a_region_past_the_edge_is_clamped() -> None:
    """The user shares one monitor and picks a region against another.

    Without the clamp this indexes past the buffer and the frame is garbage
    or a crash, on data that came from outside the app.
    """
    w, h = 4, 4
    f = crop_rgb24(b"\x09" * (w * h * 3), w, h, 3, 3, 99, 99)

    assert (f.width, f.height) == (1, 1)
    assert len(f.data) == 3


# ── the policy ────────────────────────────────────────────────────────


def test_the_fallback_answers_until_the_portal_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consent dialog must never make a caller wait.

    This is the gui's own policy, kept: start the session in the background
    and serve the Qt chain meanwhile.  It also dissolves a recorded collision
    — ``start(timeout=30.0)`` against ``AppProxy``'s 30 s IPC timeout — since
    nothing waits on the portal any more.
    """
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    fallback = _Fallback()
    session = _Session(running=False)

    frame = _capture(session, fallback).grab_region(0, 0, 4, 4)

    assert fallback.calls == 1, "the caller was not served by the fallback"
    assert frame.width == 4


def test_the_portal_frame_wins_once_the_session_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    fallback = _Fallback()
    session = _Session(running=True,
                       frame=(8, 4, bytes([0xAB]) * (8 * 4 * 3)))

    frame = _capture(session, fallback).grab_region(1, 1, 2, 2)

    assert fallback.calls == 0, "the fallback answered while the portal was up"
    assert (frame.width, frame.height) == (2, 2)
    assert frame.data == bytes([0xAB]) * 12


def test_a_running_session_with_no_frame_yet_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Up but not yet streaming is not a reason to hand back nothing."""
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    fallback = _Fallback()

    _capture(_Session(running=True, frame=None), fallback).grab_region(0, 0, 4, 4)

    assert fallback.calls == 1


def test_without_the_bindings_the_portal_is_never_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No PyGObject → pure fallback, which is what every face does today."""
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", False)
    fallback = _Fallback()
    session = _Session(running=True, frame=(8, 4, b"\x00" * 96))

    _capture(session, fallback).grab_region(0, 0, 4, 4)

    assert fallback.calls == 1
    assert not session.started.is_set(), (
        "a consent dialog was raised on a box with no portal bindings"
    )


def test_the_session_starts_once_across_many_grabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dialog per run, not one per frame."""
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    made: list[_Session] = []

    def factory() -> _Session:
        made.append(_Session(running=False))
        return made[-1]

    cap = PipeWireScreenCapture(_Fallback(), session_factory=factory)
    for _ in range(5):
        cap.grab_region(0, 0, 4, 4)

    assert len(made) == 1, f"the portal session was built {len(made)} times"


def test_construction_alone_raises_no_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_screen_capture`` runs for every face, screencast or not.

    Starting the session in the constructor would pop a "share your screen?"
    dialog at somebody who only ran ``trcc theme list``.
    """
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    made: list[_Session] = []

    def factory() -> _Session:
        made.append(_Session())
        return made[-1]

    PipeWireScreenCapture(_Fallback(), session_factory=factory)

    assert made == [], "a portal session was created before anyone captured"


def test_stop_lets_the_next_grab_start_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pw, "PIPEWIRE_AVAILABLE", True)
    made: list[_Session] = []

    def factory() -> _Session:
        made.append(_Session(running=False))
        return made[-1]

    cap = PipeWireScreenCapture(_Fallback(), session_factory=factory)
    cap.grab_region(0, 0, 4, 4)
    cap.stop()
    cap.grab_region(0, 0, 4, 4)

    assert len(made) == 2
    assert made[0].stopped is True


def test_the_shared_chooser_puts_the_portal_in_front_of_qt() -> None:
    """Both callers of ``build_screen_capture`` get the same answer.

    ``BaseOS._build_screen_capture`` and ``ui/gui`` may not reach each other,
    so "which backend" is one decision here — and it is what makes the CLI,
    the REST route and qtgui gain Wayland capture at all.
    """
    from trcc.adapters.screencast import build_screen_capture
    from trcc.adapters.screencast.qt import QtScreenCapture

    cap = build_screen_capture()

    assert isinstance(cap, PipeWireScreenCapture)
    assert isinstance(cap._fallback, QtScreenCapture)
