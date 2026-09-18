"""Cross-UI smoke — the same device drives all four UI surfaces.

The UIs are UNIFIED: cli / api / gui / qtgui each dispatch the SAME Commands
(``DiscoverDevices`` / ``ConnectDevice``) to one Command bus.  A device that
works in the core therefore works through every UI — this proves each adapter
is live by driving a real ``MockPlatform`` device through its actual surface
(Typer CliRunner, FastAPI TestClient, the two Qt windows).

Device coverage is the core's job (``test_device_catalog_smoke.py``, every
cooler); this is the UI axis — device-agnostic, one device is enough.
"""
from __future__ import annotations

from pathlib import Path

from tests.mock_platform import MockPlatform
from trcc.adapters.render.qt import QtRenderer
from trcc.app import App
from trcc.core.commands import ConnectDevice

_SPEC = {"vid": "0402", "pid": "3922", "fbl": 100}
_KEY = "0402:3922"


def _app(tmp_path: Path) -> App:
    return App(MockPlatform([_SPEC], tmp_path), renderer=QtRenderer())


def test_the_window_captures_through_its_own_platform(tmp_path: Path) -> None:
    """Under ``TRCC_DAEMON=1`` the window's session is not the daemon's, so
    the screencast must read the screen through the Platform the window was
    started with, never through ``app.platform``.  Two distinct platforms
    here stand in for the two sessions; the capture the handler holds must be
    the host's.
    """
    from trcc.ui.gui.trcc_app import TRCCApp
    app = _app(tmp_path)
    host = MockPlatform([], tmp_path / "host")
    try:
        window = TRCCApp(app=app, platform=host)

        assert window._screencast._capture is host.screen_capture()
        assert window._screencast._capture is not app.platform.screen_capture()
    finally:
        app.close()


def test_stopping_the_screencast_releases_the_capture_through_the_port(
    tmp_path: Path,
) -> None:
    """The window calls ``stop`` on whatever capture it was handed -- the
    port's method, not an attribute it checks for -- so a portal session is
    closed when the cast ends and nothing keeps streaming a screen nobody is
    showing.
    """
    from tests.conftest import FakeScreenCapture
    from trcc.ui.gui.trcc_app import TRCCApp

    class _Recording(FakeScreenCapture):
        def __init__(self) -> None:
            super().__init__()
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

    app = _app(tmp_path)
    host = MockPlatform([], tmp_path / "host")
    host.capture = _Recording()
    try:
        window = TRCCApp(app=app, platform=host)
        window._screencast.cleanup()

        assert host.capture.stops == 1, "the window did not release its capture source"
    finally:
        app.close()


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_lists_and_connects_device(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trcc.ui.cli import _ctx
    from trcc.ui.cli.main import app

    _ctx.set_platform(MockPlatform([_SPEC], tmp_path))
    _ctx.set_renderer(QtRenderer())  # type: ignore[arg-type]
    try:
        runner = CliRunner()
        listed = runner.invoke(app, ["device", "list"])
        assert listed.exit_code == 0, listed.output
        assert _KEY in listed.output

        connected = runner.invoke(app, ["device", "connect", _KEY])
        assert connected.exit_code == 0, connected.output
    finally:
        _ctx.get_app.cache_clear()
        _ctx._platform_override = None
        _ctx._renderer_override = None


# ── API ──────────────────────────────────────────────────────────────────────


def test_api_lists_and_connects_device(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from trcc.ui.api.main import build_app

    api = build_app(trcc=_app(tmp_path))
    with TestClient(api) as client:
        listed = client.get("/devices")
        assert listed.status_code == 200, listed.text
        keys = [p["key"] for p in listed.json()["products"]]
        assert _KEY in keys, keys

        connected = client.post(f"/devices/{_KEY}/connect")
        assert connected.status_code == 200, connected.text


# ── GUI (TRCCApp) ─────────────────────────────────────────────────────────────


def test_gui_builds_a_handler_for_the_device(tmp_path: Path) -> None:
    from trcc.ui.gui.trcc_app import TRCCApp

    app = _app(tmp_path)
    try:
        assert app.dispatch(ConnectDevice(key=_KEY)).ok
        window = TRCCApp(app=app, platform=app.platform)
        window.replay_initial_devices()
        assert _KEY in window._handlers, list(window._handlers)
    finally:
        app.close()


# ── qtgui (MainWindow) ────────────────────────────────────────────────────────


def test_qtgui_main_window_constructs_with_a_device(tmp_path: Path) -> None:
    from trcc.ui.qtgui.app import MainWindow

    app = _app(tmp_path)
    try:
        assert app.dispatch(ConnectDevice(key=_KEY)).ok
        window = MainWindow(app=app)
        # The unified surface is live: the devices panel exists and the window
        # holds the same App that already has the connected device.
        assert "devices" in window._panels
        assert _KEY in app.devices
    finally:
        app.close()
