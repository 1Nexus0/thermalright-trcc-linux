"""The screencast driver — what made CLI / API / daemon capture anything.

``StartScreencast`` only publishes ``ScreencastStarted``, and the GUI's
``ScreencastHandler`` was the sole subscriber that ran a timer.  Every other
client printed "Capturing on …" and captured nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pytest

from trcc.adapters.infra.send_scheduler import SyncSendScheduler
from trcc.app import App
from trcc.core.commands import (
    CaptureScreencastFrame,
    ConnectDevice,
    SendScreencastFrame,
    StartScreencast,
    StartScreencastDriver,
    StopScreencast,
    StopScreencastDriver,
)
from trcc.core.models import RawFrame
from trcc.services.screencast_driver import ScreencastDriver, task_key

from .conftest import FakePlatform, _CliRenderer
from .mock_platform import MockPlatform
from .test_display_rotation import RecordingRenderer

_KEY = "0402:3922"
_REGION = dict(x=10, y=20, w=64, h=48)


@pytest.fixture
def scheduler() -> SyncSendScheduler:
    return SyncSendScheduler()


@pytest.fixture
def app(tmp_home: Path, scheduler: SyncSendScheduler) -> App:
    a = App(platform=FakePlatform(tmp_home), send_scheduler=scheduler,
            renderer=_CliRenderer())          # type: ignore[arg-type]
    resp = bytearray(0xE100)
    resp[0] = 100                      # FBL=100 → 320x320
    a.platform.scsi.read_script.append(bytes(resp))   # type: ignore[attr-defined]
    assert a.dispatch(ConnectDevice(key=_KEY)).ok
    return a


@pytest.fixture
def casting(app: App) -> App:
    assert app.dispatch(StartScreencast(key=_KEY, audio=False, **_REGION)).ok
    return app


# ── the frame path ───────────────────────────────────────────────────


# The Phantom Spirit 120 Vision EVO: BULK, 480x480, and its handshake answers
# ``jpeg=True`` while the static registry row does NOT.  That disagreement is
# the whole point — on a device where the fallback happens to agree, omitting
# the profile is invisible, which is why this gate names a device where it is
# not.  Measured over the 9-LCD mock fleet, exactly two devices disagree:
# this one (jpeg) and 0416:5302 (320x240 rot=90 vs 240x320 rot=0).
_EVO = {"vid": "87ad", "pid": "70db", "pm": 4, "sub": 4,
        "name": "Phantom Spirit 120 Vision EVO"}
_EVO_KEY = "87ad:70db"


@pytest.fixture
def evo(tmp_home: Path, scheduler: SyncSendScheduler) -> App:
    """A connected EVO with a renderer that records which encoder ran."""
    a = App(platform=MockPlatform([_EVO], tmp_home), send_scheduler=scheduler,
            renderer=RecordingRenderer())     # type: ignore[arg-type]
    assert a.dispatch(ConnectDevice(key=_EVO_KEY)).ok
    return a


def test_the_screencast_frame_is_encoded_for_the_PANEL_not_the_registry(
    evo: App,
) -> None:
    """``SendScreencastFrame`` must hand the encoder the LIVE profile.

    Six of the seven ``app.display.build_*`` call sites pass
    ``profile=device.profile``; this one did not, so ``_resolve_profile``
    fell back to the registry FBL / a synthesised RGB565 profile.  On this
    panel that is not cosmetic: the handshake says JPEG and the fallback says
    RGB565, so every captured frame went out as **460,800 bytes instead of
    6,927** — the "460KB/frame" @alan7383 reported in #271, reproduced here.

    Asserted as WHICH ENCODER RAN rather than as a byte count: the count is a
    consequence of the choice, and the choice is the bug.
    """
    device = evo.devices[_EVO_KEY]
    assert device.profile is not None and device.profile.jpeg is True, (
        "fixture no longer models a JPEG panel — this gate is then vacuous"
    )

    renderer = evo.renderer
    renderer.calls.clear()                    # type: ignore[attr-defined]
    result = evo.dispatch(SendScreencastFrame(
        key=_EVO_KEY, frame=RawFrame(b"", 64, 64),
    ))
    assert result.ok is True, result.message

    ran = [name for name, _ in renderer.calls]   # type: ignore[attr-defined]
    assert "encode_jpeg" in ran, (
        "the panel handshook as JPEG and the frame was encoded some other "
        "way — the live profile is not reaching build_screencast_frame"
    )
    assert "encode_rgb565" not in ran, (
        "encoded RGB565 for a JPEG panel: that is the 66x oversized frame"
    )



def test_capture_grabs_the_region_the_session_declared(casting: App) -> None:
    """The region comes from ``screencast_region``, not from the caller.

    One home for "is a screencast running and over what" — the Command takes
    only a key, so a driver cannot drift from what StartScreencast persisted.
    """
    result = casting.dispatch(CaptureScreencastFrame(key=_KEY))

    assert result.ok is True, result.message
    assert casting.platform.capture.regions == [(10, 20, 64, 48)]


def test_capture_without_a_session_is_a_skip_not_a_crash(app: App) -> None:
    """A driver keeps dispatching briefly after the session is stopped.

    That race is normal, so "no region" must be an ok=False report rather than
    an exception that would kill the scheduler thread.
    """
    result = app.dispatch(CaptureScreencastFrame(key=_KEY))

    assert result.ok is False
    assert "no screencast session" in result.message
    assert app.platform.capture.regions == []


def test_a_failing_grab_does_not_raise(casting: App, monkeypatch) -> None:
    """Capture depends on a desktop session that can vanish mid-cast.

    Screen locked, portal consent revoked, ``grim`` uninstalled — the frame is
    lost, the session is not.

    MUTATION CHECK: drop the try/except in the Command and this raises.
    """
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("portal session revoked")

    monkeypatch.setattr(casting.platform.capture, "grab_region", boom)

    result = casting.dispatch(CaptureScreencastFrame(key=_KEY))

    assert result.ok is False
    assert "portal session revoked" in result.message


def test_a_source_that_is_not_ready_yet_is_quiet(
    casting: App, monkeypatch, caplog,
) -> None:
    """The portal's consent seconds are a dropped frame, not an error.

    ``CaptureNotReady`` fires on every tick of a consent window -- seven a
    second for up to thirty seconds.  ``App.dispatch`` escalates every
    ``ok=False`` to WARNING regardless of level, so the Command reports a
    completed tick with no frame, and the only line is a DEBUG one.
    """
    from trcc.core.ports import CaptureNotReady

    def not_yet(*_a: object, **_k: object) -> None:
        raise CaptureNotReady("waiting for consent")

    monkeypatch.setattr(casting.platform.capture, "grab_region", not_yet)
    with caplog.at_level(logging.DEBUG, logger="trcc"):
        result = casting.dispatch(CaptureScreencastFrame(key=_KEY))

    assert result.ok is True, "a consent wait was reported as a failure"
    assert "waiting for consent" in result.message
    loud = [r for r in caplog.records
            if r.levelno >= logging.WARNING and "CaptureScreencastFrame" in r.getMessage()]
    assert not loud, [r.getMessage() for r in loud]


# ── the cadence ──────────────────────────────────────────────────────


def test_the_driver_key_cannot_collide_with_the_device_sender(app: App) -> None:
    """THE trap this design turns on.

    ``ThreadSendScheduler.add`` evicts — and STOPS — any task already
    registered under the same key.  A driver registered under the bare device
    key would kill that device's ``DeviceSender`` and its keepalives, so the
    panel would go dark the moment a screencast started.

    MUTATION CHECK: return the bare device key from ``ScreencastDriver.key``
    and this fails.
    """
    driver = ScreencastDriver(app, _KEY)

    assert driver.key != _KEY
    assert driver.key == task_key(_KEY) == f"screencast:{_KEY}"


def test_driving_does_not_evict_the_device_sender(
    casting: App, scheduler: SyncSendScheduler,
) -> None:
    """The same trap, proved through the scheduler rather than by inspection."""
    casting.start_sender(_KEY)
    assert _KEY in scheduler._tasks

    assert casting.dispatch(StartScreencastDriver(key=_KEY)).ok

    assert _KEY in scheduler._tasks, "the screencast driver evicted the sender"
    assert task_key(_KEY) in scheduler._tasks


def test_each_tick_captures_one_frame(
    casting: App, scheduler: SyncSendScheduler,
) -> None:
    assert casting.dispatch(StartScreencastDriver(key=_KEY)).ok

    for tick in range(5):
        scheduler.tick(float(tick))

    assert len(casting.platform.capture.regions) == 5
    assert set(casting.platform.capture.regions) == {(10, 20, 64, 48)}


def test_stopping_the_driver_stops_the_capture(
    casting: App, scheduler: SyncSendScheduler,
) -> None:
    assert casting.dispatch(StartScreencastDriver(key=_KEY)).ok
    scheduler.tick(0.0)
    assert casting.dispatch(StopScreencastDriver(key=_KEY)).ok

    scheduler.tick(1.0)
    scheduler.tick(2.0)

    assert len(casting.platform.capture.regions) == 1


def test_disconnecting_stops_the_driver(
    casting: App, scheduler: SyncSendScheduler,
) -> None:
    """``App.stop_sender`` removes the BARE key; the driver is namespaced.

    Without an explicit removal the driver outlives its device and keeps
    capturing for something no longer attached.

    MUTATION CHECK: drop the ``remove(task_key(key))`` from ``stop_sender``
    and this fails.
    """
    casting.start_sender(_KEY)
    assert casting.dispatch(StartScreencastDriver(key=_KEY)).ok
    assert task_key(_KEY) in scheduler._tasks

    casting.stop_sender(_KEY)

    assert task_key(_KEY) not in scheduler._tasks


def test_driver_refuses_a_device_with_no_session(app: App) -> None:
    """Driving nothing is a mistake worth naming, not a silent no-op."""
    result = app.dispatch(StartScreencastDriver(key=_KEY))

    assert result.ok is False
    assert "no screencast session" in result.message
    assert task_key(_KEY) not in app._send_scheduler._tasks   # type: ignore[attr-defined]


def test_stopping_a_driver_that_never_ran_is_fine(app: App) -> None:
    """A client may stop a session it did not drive."""
    assert app.dispatch(StopScreencastDriver(key=_KEY)).ok


def test_stop_screencast_leaves_no_region_for_the_driver(casting: App) -> None:
    """After StopScreencast the frame Command must decline.

    The region IS the session flag, so clearing it is what makes an in-flight
    driver tick harmless.
    """
    assert casting.dispatch(StopScreencast(key=_KEY)).ok

    assert casting.dispatch(CaptureScreencastFrame(key=_KEY)).ok is False


# ── one producer owns the panel ──────────────────────────────────────


def _render_observer(app: App):
    """The App's own ``_DeviceRenderObserver``, whichever bus slot it sits in."""
    from trcc.app import _DeviceRenderObserver
    for attr in vars(app).values():
        if isinstance(attr, _DeviceRenderObserver):
            return attr
    raise AssertionError("no _DeviceRenderObserver on the App")


def test_a_running_screencast_suppresses_the_reactive_theme_render(
    casting: App, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE blink, gated.

    Reported 2026-09-14: "it does capture the screen but it blinks the metrics
    mask".  ``_DeviceRenderObserver`` dispatched ``RenderAndSend`` on every
    ``SensorsUpdated`` with no screencast check, so a second producer wrote the
    same panel — a full theme frame — while the capture tick was writing screen
    frames at ``SCREENCAST_TICK_S`` (~7 fps).  Measured in the reporter's log:
    ~13 capture frames, then one theme frame, every 2 s.

    The two composites are genuinely different pictures (one's background is
    the captured region, the other's is the theme), so this is not a redundant
    frame — it is a visible flash.

    MUTATION CHECK: drop the ``screencast_region is not None`` guard in
    ``_DeviceRenderObserver._on_visual_change`` and this fails.
    """
    from trcc.core.events import SensorsUpdated

    # An ACTIVE THEME is required or this passes vacuously: the observer
    # skips a device with no theme BEFORE it reaches the screencast guard, so
    # without this the first assertion proves nothing.  (Caught by the
    # "resumes after stop" half below refusing to fire.)
    from trcc.core.models import Theme
    casting.active_themes[_KEY] = Theme(name="T", path=Path("/nonexistent"), resolution=(320, 320))

    rendered: list[str] = []
    observer = _render_observer(casting)
    monkeypatch.setattr(
        observer, "_RenderAndSend",
        lambda key: (rendered.append(key), _Noop())[1],
    )

    casting.events.publish(SensorsUpdated(readings={}, metrics=None))
    assert rendered == [], (
        "a theme render was dispatched while a screencast owned the panel — "
        "two producers, which is the blink"
    )

    # …and it resumes the moment the session ends.
    assert casting.dispatch(StopScreencast(key=_KEY)).ok
    casting.events.publish(SensorsUpdated(readings={}, metrics=None))
    assert rendered == [_KEY], (
        "the theme render did not resume after the screencast stopped"
    )


class _Noop:
    """A Command the stubbed observer can dispatch harmlessly.

    ``LOG_LEVEL`` is not decoration: ``App.dispatch`` reads it to pick a
    logger, so without it the mutation check fails on an AttributeError
    instead of on the assertion it is meant to prove.
    """

    LOG_LEVEL: ClassVar[int] = logging.DEBUG

    def execute(self, app: App) -> None:
        return None


def test_the_metrics_actually_reach_the_screencast_pixels(tmp_home: Path) -> None:
    """BEHAVIOURAL, with the real renderer — the layer must change the bytes.

    The sibling gates in ``test_wire_frame_shape`` count ``composite`` CALLS,
    and that is structural: driving this by hand with a real ``QtRenderer``
    showed a frame whose composite call fired while the output stayed
    byte-identical, because an overlay with no elements paints nothing.  A
    call-count gate would have reported the blink fixed while the panel still
    showed a bare desktop.

    So this asserts what a user sees: same LENGTH (the geometry did not move)
    and different CONTENT (something was actually drawn).
    """
    from trcc.adapters.render.qt import QtRenderer
    from trcc.core.models import OverlayElement, RawFrame, Theme

    app = App(platform=FakePlatform(tmp_home), renderer=QtRenderer())
    resp = bytearray(0xE100)
    resp[0] = 100                       # FBL=100 → 320x320
    app.platform.scsi.read_script.append(bytes(resp))   # type: ignore[attr-defined]
    assert app.dispatch(ConnectDevice(key=_KEY)).ok

    info = app.get(_KEY).info
    frame = RawFrame(data=bytes([128, 128, 128]) * (320 * 320),
                     width=320, height=320)
    theme = Theme(name="T", path=tmp_home, resolution=(320, 320))
    app.settings.for_device(_KEY).overlay_enabled = True
    for i, y in enumerate((40, 90, 140)):
        app.settings.add_user_overlay_element(_KEY, OverlayElement(
            id=f"e{i}", type="text", text="CPU", x=40, y=y,
            size=36, color="#ff0000"))

    bare = app.display.build_screencast_frame(info=info, frame=frame, theme=None)
    drawn = app.display.build_screencast_frame(
        info=info, frame=frame, theme=theme, sensors={})

    assert len(bare) == len(drawn), (
        "compositing the overlay changed the frame SIZE — the geometry moved"
    )
    assert bare != drawn, (
        "the overlay composited but changed no pixel — the metrics are not "
        "reaching the panel, which is the reported defect"
    )


def test_the_preview_shows_the_same_picture_as_the_panel(tmp_home: Path) -> None:
    """The preview surface must be the COMPOSITED frame, not the raw grab.

    Reported 2026-09-14, immediately after the mask/metrics fix landed: "it
    does not show the metrics mask in preview just on the lcd screen".  The
    gui painted its preview directly from the captured image
    (``lcd_handler.on_screencast_frame``) while the wire frame went through
    the compositor, so the two diverged the moment the wire frame gained a
    layer.

    ``SendScreencastFrame`` now publishes ``FrameSent`` carrying the composited
    surface, the same way ``RenderAndSend`` does, so both faces read one
    picture.  Asserted on the SURFACE the event would carry, because that is
    the thing the gui paints.

    MUTATION CHECK: drop the ``_remember_preview`` call in
    ``build_screencast_frame`` and ``rendered_surface`` answers None — the
    preview would go blank rather than stale, which is why None is asserted
    against explicitly.
    """
    from trcc.adapters.render.qt import QtRenderer
    from trcc.core.models import OverlayElement, RawFrame, Theme

    app = App(platform=FakePlatform(tmp_home), renderer=QtRenderer())
    resp = bytearray(0xE100)
    resp[0] = 100
    app.platform.scsi.read_script.append(bytes(resp))   # type: ignore[attr-defined]
    assert app.dispatch(ConnectDevice(key=_KEY)).ok

    info = app.get(_KEY).info
    theme = Theme(name="T", path=tmp_home, resolution=(320, 320))
    app.active_themes[_KEY] = theme
    app.settings.for_device(_KEY).overlay_enabled = True
    app.settings.add_user_overlay_element(_KEY, OverlayElement(
        id="e0", type="text", text="CPU", x=40, y=40, size=36, color="#ff0000"))

    grab = RawFrame(data=bytes([128, 128, 128]) * (320 * 320),
                    width=320, height=320)
    app.display.build_screencast_frame(
        info=info, frame=grab, theme=theme, sensors={})

    preview = app.display.rendered_surface(_KEY)
    assert preview is not None, (
        "no preview surface was recorded — the gui would show a blank panel"
    )

    # The composited preview must differ from the flat grey grab: if it does
    # not, the preview is the raw capture again and the defect is back.
    bare = app.display.build_screencast_frame(info=info, frame=grab, theme=None)
    composited = app.display.build_screencast_frame(
        info=info, frame=grab, theme=theme, sensors={})
    assert bare != composited, (
        "the preview path is showing an uncomposited frame — the mask and "
        "metrics reach the panel but not the preview"
    )
