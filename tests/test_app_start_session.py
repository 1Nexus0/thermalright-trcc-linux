"""``App.start_session`` — the partner of ``App.close``.

Its four calls used to be copy-pasted into ``run_daemon``, ``run_gui`` and
``run_qtgui``; the API had none of them, and the daemon's copy was missing
``metrics_loop.start()`` — a reporter ran ``trccd.service`` and watched a
connected device stay permanently blank (#148).

The coldplug half is the one the daemon never had at all: only Linux's hotplug
monitor replays already-present devices, so on Windows / macOS / BSD a daemon
came up owning USB with nothing connected.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.conftest import FakePlatform
from trcc.app import App


@pytest.fixture
def app(tmp_home: Path) -> App:
    return App(platform=FakePlatform(tmp_home))


def _spy_on_coldplug(monkeypatch) -> list[object]:
    """Record each ``discover_and_connect`` call, still running the real one.

    Replacing it outright would drop the ``_coldplug_done`` side effect that
    ``start_session`` reads — the double would then model the contract wrong
    and the test would fail against correct code.
    """
    calls: list[object] = []
    real = App.discover_and_connect

    def spy(self: App, on_progress=None) -> None:
        calls.append(on_progress)
        real(self, on_progress)

    monkeypatch.setattr(App, "discover_and_connect", spy)
    return calls


def test_start_session_runs_the_coldplug(app: App, monkeypatch) -> None:
    """The half the daemon never had — without it a device is never connected
    on any OS whose hotplug monitor reports only NEW devices."""
    calls = _spy_on_coldplug(monkeypatch)

    app.start_session()

    assert calls == [None], "start_session must coldplug"


def test_start_session_starts_all_three_loops(app: App) -> None:
    """#148 was one missing line out of these three."""
    app.start_session()
    try:
        assert app.metrics_loop.is_running, "metrics loop not started (#148)"
        assert app.led_animation_loop.is_running, "LED animation loop not started"
        assert app.platform.hotplug().is_running, "hotplug listener not started"
    finally:
        app.close()


def test_a_coldplug_that_already_ran_is_not_repeated(app: App, monkeypatch) -> None:
    """gui discovers on its splash worker and then calls start_session.  A
    second coldplug would re-attempt every device that failed and record its
    failure twice."""
    calls = _spy_on_coldplug(monkeypatch)

    app.discover_and_connect()          # the splash worker's call
    app.start_session()

    assert len(calls) == 1, "start_session repeated a coldplug the splash did"


def test_start_session_is_idempotent(app: App, monkeypatch) -> None:
    seen = _spy_on_coldplug(monkeypatch)
    try:
        app.start_session()
        app.start_session()
        assert len(seen) == 1
        assert app.metrics_loop.is_running
    finally:
        app.close()


def test_on_progress_reaches_the_coldplug(app: App, monkeypatch) -> None:
    """qtgui passes nothing; gui's splash passes a Qt signal so per-device
    status shows while connecting."""
    seen = _spy_on_coldplug(monkeypatch)

    sink: list[str] = []
    app.start_session(sink.append)

    assert seen == [sink.append]


def test_close_undoes_start_session(app: App) -> None:
    """The two are a pair; close must stop everything start_session began."""
    app.start_session()
    app.close()

    assert not app.metrics_loop.is_running
    assert not app.led_animation_loop.is_running
    assert not app.platform.hotplug().is_running


# ── The refresh interval must reach the SWEEP, not just the broadcast ──


def test_set_refresh_interval_reaches_the_sensor_sweep(app: App) -> None:
    """The user's one CPU lever has to move both halves of its own pipeline.

    ``MetricsLoop`` re-read ``refresh_interval_s`` every iteration, so the
    BROADCAST obeyed a ``SetRefreshInterval`` immediately.  The sweep feeding
    it did not: the enumerator's ``_interval_s`` was written once at
    ``start_polling`` and that call early-returns while its thread is alive.
    Driven on the real App before the fix, publishes fell 0.50/s -> 0.08/s
    while sweeps stayed at 0.50/s — a user who raised the interval to save CPU
    kept paying every sweep, forever.

    The cadence half of this contract is proved by counting sweeps in
    ``test_set_interval_changes_the_poll_cadence_while_the_thread_runs``; what
    is left to prove here is that the SETTING reaches that writer at all.  The
    existing ``SetRefreshInterval`` tests assert the stored value and the
    published event, and both pass whether or not the sweep ever hears about
    it — which is exactly how this survived.

    MUTATION CHECK: drop the ``set_interval`` push from ``MetricsLoop._loop``
    and this fails holding the boot-time default.
    """
    from trcc.core.commands import SetRefreshInterval
    from trcc.core.models import DEFAULT_REFRESH_INTERVAL_S

    sensors = app.platform.sensors()
    app.metrics_loop.start()
    try:
        assert sensors._interval_s == DEFAULT_REFRESH_INTERVAL_S

        assert app.dispatch(SetRefreshInterval(seconds=7.0)).ok

        deadline = time.monotonic() + 5.0     # bounded: never hang the suite
        while sensors._interval_s != 7.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sensors._interval_s == 7.0, (
            f"the sweep never heard about the change — still polling every "
            f"{sensors._interval_s}s while the broadcast runs at 7.0s"
        )
    finally:
        app.metrics_loop.stop()
