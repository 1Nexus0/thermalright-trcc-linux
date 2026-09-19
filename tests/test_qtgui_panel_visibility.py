"""A hidden qtgui panel must stop polling.

qtgui shows ONE panel at a time in a ``QStackedWidget`` (``app.py:102``), but
its panels poll on plain ``QTimer``s that nothing ever stops:
``stop_periodic_updates`` had **zero callers** and there were no
``showEvent`` / ``hideEvent`` hooks anywhere in the skin.  ``ui/gui`` gates its
work on visibility (``trcc_app.py:342`` — ``isVisible() and not
minimized_to_taskbar``); qtgui did not gate at all.

**Measured 2026-09-19 on the mock fleet, switching only which panel is shown:**

    showing PreviewPanel   build_frame 7.90/s  BuildPreview 2.02/s
    showing LedPanel       build_frame 7.95/s  BuildPreview 2.04/s
    showing AboutPanel     build_frame 7.93/s  BuildPreview 2.03/s

Identical — the hidden panels kept dispatching ``BuildPreview`` (a real
composite, ``device.py:865``) and ``ReadSensors`` for the life of the process.

**Why the fix is on ``BasePanel`` and NOT on ``PeriodicUpdater``**, which would
look like the DRYer place: ``app.py:181`` holds ``self._video: dict[str,
PeriodicUpdater]``, and video playback drives the PHYSICAL DEVICE.  Gating the
updater itself would freeze a playing panel the moment the window is hidden —
a far worse bug than the one being fixed.  Gating on ``BasePanel`` structurally
cannot reach those, because they are owned by the window, not by a panel.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QStackedWidget

from tests.mock_platform import MockPlatform
from trcc.app import App
from trcc.ui.bus_bridge import BusBridge
from trcc.ui.qtgui.base import BasePanel

_SPECS = [{"type": "lcd", "vid": "0402", "pid": "3922", "fbl": 100}]


class _Ticking(BasePanel):
    """A panel that polls, so the gate measures the seam and not a stub."""

    def __init__(self, app: App, bus: BusBridge) -> None:
        self.ticks = 0
        super().__init__(app, bus)
        self.start_periodic_updates(10, self._tick)

    def _setup_ui(self) -> None:
        """Required by ``BasePanel``; this gate is about the timer, not widgets."""

    def _tick(self) -> None:
        self.ticks += 1


def _stacked(qtbot, tmp_path: Path) -> tuple[App, QStackedWidget, list[_Ticking]]:
    """Two panels in a real stack — the widget qtgui actually uses."""
    app = App(MockPlatform(_SPECS, tmp_path))
    bus = BusBridge(app.events)
    stack = QStackedWidget()
    panels = [_Ticking(app, bus), _Ticking(app, bus)]
    for panel in panels:
        stack.addWidget(panel)
    qtbot.addWidget(stack)
    stack.show()
    qtbot.waitExposed(stack)
    return app, stack, panels


def test_a_hidden_panel_stops_ticking(qtbot, tmp_path: Path) -> None:
    """Switching the stack must silence the panel that left the screen.

    MUTATION CHECK: remove ``hideEvent`` from ``BasePanel`` and this fails
    with the hidden panel's tick count still climbing.
    """
    app, stack, panels = _stacked(qtbot, tmp_path)
    try:
        stack.setCurrentIndex(1)
        qtbot.wait(60)
        before = panels[0].ticks
        qtbot.wait(150)
        assert panels[0].ticks == before, (
            f"the hidden panel ticked {panels[0].ticks - before} more time(s) "
            "after leaving the screen — every qtgui panel polls forever, "
            "whatever the user is looking at"
        )
    finally:
        app.close()


def test_the_shown_panel_keeps_ticking(qtbot, tmp_path: Path) -> None:
    """The counterpart: gating must not silence the VISIBLE panel.

    A fix that stopped everything would pass the test above while breaking the
    feature, so the two are asserted as a pair.
    """
    app, stack, panels = _stacked(qtbot, tmp_path)
    try:
        stack.setCurrentIndex(1)
        qtbot.wait(60)
        before = panels[1].ticks
        qtbot.waitUntil(lambda: panels[1].ticks > before, timeout=2000)
        assert panels[1].ticks > before, "the visible panel stopped updating"
    finally:
        app.close()


def test_a_panel_shown_again_resumes(qtbot, tmp_path: Path) -> None:
    """Coming back must restart it, or the panel is dead after one switch.

    Stopping is half a fix; a user switching away and back is the common case.
    """
    app, stack, panels = _stacked(qtbot, tmp_path)
    try:
        stack.setCurrentIndex(1)
        qtbot.wait(60)
        stack.setCurrentIndex(0)
        qtbot.wait(60)
        before = panels[0].ticks
        qtbot.waitUntil(lambda: panels[0].ticks > before, timeout=2000)
        assert panels[0].ticks > before, (
            "the panel never resumed after being shown again — it is now "
            "permanently frozen, which is worse than the polling it replaced"
        )
    finally:
        app.close()


def test_the_sensor_picker_stops_polling_when_hidden(qtbot, tmp_path: Path) -> None:
    """``SensorPickerWidget`` is the same defect outside ``BasePanel``.

    It is a plain ``QWidget`` with a RAW ``QTimer``, embedded in a panel
    (``dashboard_box.py:52``) and a dialog (``overlay_editor.py:489``), so the
    ``BasePanel`` hooks cannot reach it.  Measured after those hooks landed,
    ``ReadSensors`` still ran at **0.48/s** with every host hidden — this
    widget is the whole remainder, and the number halved rather than going to
    zero because it is the second of two 2 s pollers.

    **It is hidden by an ANCESTOR, and that is the whole difficulty.**  Qt
    delivers ``hideEvent`` to the widget the stack hides -- the panel -- and
    NOT to its descendants.  A first version of this gate called
    ``picker.hide()`` directly, passed, and the real skin kept polling at
    0.52/s with every host off screen: the test exercised a path the app never
    takes.  So it hides the PARENT, which is what a stack switch does.

    MUTATION CHECK: remove the ``isVisible()`` guard from ``_tick`` and this
    fails with the dispatch count still climbing.
    """
    from trcc.ui.qtgui.sensor_picker import SensorPickerWidget

    app = App(MockPlatform(_SPECS, tmp_path))
    try:
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        host = QWidget()
        QVBoxLayout(host)
        picker = SensorPickerWidget(app, host)
        host.layout().addWidget(picker)
        qtbot.addWidget(host)
        host.show()
        qtbot.waitExposed(host)

        # Count the WORK — ReadSensors dispatches — not the timer ticks.  A
        # first version wrapped ``_refresh`` and counted calls to the wrapper,
        # so it went on counting after the guard inside ``_refresh`` started
        # skipping the dispatch: the gate failed while the skin was measured
        # clean at 0.00/s.  Same lesson as `9431f517`.
        calls: list[int] = []
        real_dispatch = app.dispatch

        def counting_dispatch(cmd):
            if type(cmd).__name__ == "ReadSensors":
                calls.append(1)
            return real_dispatch(cmd)

        app.dispatch = counting_dispatch   # type: ignore[method-assign]
        picker._updates.start(10, picker._tick)

        qtbot.waitUntil(lambda: bool(calls), timeout=2000)
        host.hide()                      # the ANCESTOR, as a stack switch does
        qtbot.wait(60)
        before = len(calls)
        qtbot.wait(150)

        assert len(calls) == before, (
            f"the hidden sensor picker dispatched ReadSensors "
            f"{len(calls) - before} more time(s) — it polls on a 2 s timer "
            "inside hosts the user cannot see"
        )
    finally:
        app.close()
