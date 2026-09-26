"""BuildPreview — the one render-without-sending every UI dispatches.

Pins the contract the four hand-written copies (gui / qtgui / cli / api) had
drifted apart on: the same personalized sensors the wire path uses, an empty
answer that is not a failure, and one Result that carries a surface, encoded
bytes or a sampled grid depending on what the caller asked for.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trcc.adapters.render.qt import QtRenderer
from trcc.app import App
from trcc.core.commands import BuildPreview, ConnectDevice, LoadImage, SendImage
from trcc.core.events import SensorsUpdated
from trcc.core.models import Theme
from trcc.services.display import DisplayService
from trcc.services.media import Playback

from .mock_platform import MockPlatform

_SPEC = {"type": "lcd", "vid": "87ad", "pid": "70db",
         "resolution": "854x480", "pm": 11, "sub": 5}
_VID, _PID = 0x87AD, 0x70DB
_KEY = "87ad:70db"


@pytest.fixture
def app(tmp_path: Path) -> App:
    """A connected 854x480 device — non-square, so aspect bugs show up."""
    app = App(MockPlatform([_SPEC], tmp_path), renderer=QtRenderer())
    app.attach(_VID, _PID)
    assert app.dispatch(ConnectDevice(key=_KEY)).ok
    # SUB 5 means this panel is portrait-MOUNTED, so connect seeds it to 90
    # the way the vendor app does.  These tests assert ASPECT, so the angle
    # has to be stated rather than inherited — otherwise they quietly re-test
    # the mount rule instead of the contract they are named for.  The mount
    # rule has its own guard in tests/test_mount_orientation_seed.py.
    app.settings.set_orientation(_KEY, 0)
    return app


def _load_theme(app: App, tmp_path: Path) -> Theme:
    theme = Theme(
        path=tmp_path / "theme", name="t",
        resolution=(854, 480), config={"elements": []},
    )
    app.active_themes[_KEY] = theme
    return theme


# ── The two empty answers, which are not the same answer ─────────────────


def test_unattached_device_is_a_failure(app: App) -> None:
    r = app.dispatch(BuildPreview(key="dead:beef"))

    assert r.ok is False
    assert r.surface is None
    assert "dead:beef" in r.message


def test_no_active_theme_is_ok_with_no_surface(app: App) -> None:
    """Pre-load is a normal state, not a failure — the GUI hits it on every
    non-render send, and ok=False would WARN through App.dispatch each time."""
    r = app.dispatch(BuildPreview(key=_KEY))

    assert r.ok is True
    assert r.surface is None
    assert r.message == "No active theme — nothing to preview"


# ── What the caller asked for is what comes back ─────────────────────────


def test_surface_comes_back_sized_like_the_panel(app: App, tmp_path: Path) -> None:
    _load_theme(app, tmp_path)

    r = app.dispatch(BuildPreview(key=_KEY))

    assert r.ok is True
    assert r.surface is not None
    assert (r.width, r.height) == (854, 480)
    assert r.theme_name == "t"
    # Nothing was encoded — the Qt skins pay no encode cost.
    assert r.image == b""
    assert r.media_type == ""
    assert r.pixels == []


def test_png_encode_returns_png_bytes_and_the_surface(
    app: App, tmp_path: Path,
) -> None:
    _load_theme(app, tmp_path)

    r = app.dispatch(BuildPreview(key=_KEY, encode="png"))

    assert r.ok is True
    assert r.image.startswith(b"\x89PNG\r\n\x1a\n")
    assert r.media_type == "image/png"
    assert r.surface is not None   # in-process callers still get it free


def test_jpeg_encode_returns_jpeg_bytes(app: App, tmp_path: Path) -> None:
    _load_theme(app, tmp_path)

    r = app.dispatch(BuildPreview(key=_KEY, encode="jpeg"))

    assert r.ok is True
    assert r.image.startswith(b"\xff\xd8\xff")
    assert r.media_type == "image/jpeg"


def test_sample_grid_follows_the_surface_aspect_ratio(
    app: App, tmp_path: Path,
) -> None:
    """20 columns of an 854x480 panel is 11.2 rows → 12 (even, for the
    half-block renderer).  A square grid here is the bug this replaces."""
    _load_theme(app, tmp_path)

    r = app.dispatch(BuildPreview(key=_KEY, sample_cols=20))

    assert len(r.pixels) == 12
    assert all(len(row) == 20 for row in r.pixels)
    assert all(len(px) == 3 for row in r.pixels for px in row)


# ── The drift this Command exists to end ─────────────────────────────────


def test_sensors_are_personalized_like_the_wire_path(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gui / cli / api fed the renderer RAW readings while RenderAndSend fed
    personalized ones, so a °F user saw the Celsius number under a °F glyph
    and an HDD-off user saw disk metrics the panel omitted."""
    _load_theme(app, tmp_path)
    app.settings.app.temp_unit = "F"
    app.settings.app.hdd_enabled = False

    enum = app.platform.sensors()
    monkeypatch.setattr(
        enum, "read_all",
        lambda: {"cpu:temp": 42.0, "cpu:usage": 15.0, "disk:read": 1.5},
    )
    seen: dict[str, float] = {}
    original = DisplayService.build_preview_surface

    def spy(self: DisplayService, *args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs["sensors"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DisplayService, "build_preview_surface", spy)

    assert app.dispatch(BuildPreview(key=_KEY)).ok is True

    assert seen["cpu:temp"] == pytest.approx(107.6)   # 42 °C in °F
    assert seen["cpu:usage"] == 15.0                  # untouched
    assert "disk:read" not in seen                    # HDD disabled → dropped


def test_render_failure_is_reported_not_raised(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every UI wrapped this call so a bad theme couldn't take the panel
    down.  The guard lives here now — surfaced as ok=False, never swallowed."""
    _load_theme(app, tmp_path)

    def boom(self: DisplayService, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no font for you")

    monkeypatch.setattr(DisplayService, "build_preview_surface", boom)

    r = app.dispatch(BuildPreview(key=_KEY))

    assert r.ok is False
    assert r.surface is None
    assert "RuntimeError" in r.message and "no font for you" in r.message


# ── Daemon safety — the reason this is a Command at all ──────────────────


def test_result_survives_the_ipc_boundary_with_bytes_intact(
    app: App, tmp_path: Path,
) -> None:
    """Over the socket the live surface can't travel, so a daemon client asks
    for an encode and reads bytes — the same contract FrameSent.surface has."""
    from trcc import ipc

    _load_theme(app, tmp_path)
    r = app.dispatch(BuildPreview(key=_KEY, encode="png", sample_cols=8))

    back = ipc.decode_result(ipc.encode_result(r))

    assert back.surface is None            # dropped, loudly, at the boundary
    assert back.image == r.image           # bytes round-trip base64
    assert back.pixels == r.pixels         # grid round-trips as tuples
    assert (back.width, back.height) == (854, 480)


# ── A pushed image owns the panel until the next theme load (#306) ──────

_RED, _BLUE = (255, 0, 0), (0, 0, 255)


def _png(tmp_path: Path, name: str, rgb: tuple[int, int, int]) -> Path:
    renderer = QtRenderer()
    path = tmp_path / name
    path.write_bytes(renderer.encode_png(
        renderer.create_surface(64, 64, color=(*rgb, 255))))
    return path


def _wire(app: App, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every frame the app writes from here on."""
    sent: list[bytes] = []
    real = app.send

    def spy(key: str, payload: Any, **kw: Any) -> bool:
        sent.append(bytes(payload))
        return real(key, payload, **kw)

    monkeypatch.setattr(app, "send", spy)
    return sent


def _push(app: App, tmp_path: Path) -> None:
    """A real theme on the panel, then a blue push over it."""
    assert app.dispatch(LoadImage(key=_KEY, path=_png(tmp_path, "r.png", _RED))).ok
    assert app.dispatch(SendImage(key=_KEY, path=_png(tmp_path, "b.png", _BLUE))).ok


def test_a_push_survives_the_next_sensor_tick(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The theme re-rendered on the next ``SensorsUpdated`` and painted over the
    push within ~2 s, so the "ephemeral" send lasted one sensor interval."""
    _push(app, tmp_path)
    sent = _wire(app, monkeypatch)

    app.events.publish(SensorsUpdated(readings={}))

    assert sent == []
    assert _KEY not in app.active_themes


def test_the_preview_shows_the_pushed_image(app: App, tmp_path: Path) -> None:
    _push(app, tmp_path)

    r = app.dispatch(BuildPreview(key=_KEY, encode="png", sample_cols=8))

    assert r.ok is True
    assert r.image
    assert r.theme_name == ""
    assert r.message == "Preview 854x480 of the pushed image"
    assert {px for row in r.pixels for px in row} == {_BLUE}


def test_the_next_theme_load_takes_the_panel_back(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _push(app, tmp_path)
    assert app.dispatch(LoadImage(key=_KEY, path=_png(tmp_path, "r.png", _RED))).ok
    sent = _wire(app, monkeypatch)

    app.events.publish(SensorsUpdated(readings={}))

    assert len(sent) == 1


def test_a_push_is_refused_while_a_screencast_owns_the_panel(
    app: App, tmp_path: Path,
) -> None:
    """Refused, not stopped: stopping a screencast clears its SAVED region."""
    assert app.dispatch(LoadImage(key=_KEY, path=_png(tmp_path, "r.png", _RED))).ok
    region = (0, 0, 100, 100, False)
    app.settings.set_screencast_region(_KEY, region)

    r = app.dispatch(SendImage(key=_KEY, path=_png(tmp_path, "b.png", _BLUE)))

    assert r.ok is False
    assert r.message == f"A screencast owns {_KEY} — stop it first"
    assert app.settings.for_device(_KEY).screencast_region == region
    assert _KEY in app.active_themes


def test_a_push_stops_a_video_and_keeps_the_saved_background(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The video would paint over the push at its own fps; the saved
    background stays, so a restart brings it back.  And the push writes ONE
    frame: stopping the video first, with the theme still active, would flash
    a theme render ahead of it."""
    assert app.dispatch(LoadImage(key=_KEY, path=_png(tmp_path, "r.png", _RED))).ok
    saved = str(tmp_path / "bg.mp4")
    frame = QtRenderer().encode_png(QtRenderer().create_surface(8, 8))
    app.media._playbacks[_KEY] = Playback(frames=[frame], fps=15)
    app.settings.set_background_path(_KEY, saved)
    sent = _wire(app, monkeypatch)

    assert app.dispatch(SendImage(key=_KEY, path=_png(tmp_path, "b.png", _BLUE))).ok

    assert len(sent) == 1
    assert app.media.playback(_KEY) is None
    assert app.settings.for_device(_KEY).background_path == saved
