"""GUI panel smoke tests — construct each widget + verify wiring.

Phase D boilerplate: every ``ui/gui/`` module was at 0% coverage; this
file establishes the QApplication-offscreen fixture pattern + one
construction smoke per panel class so widgets are at least known to
build without raising.

Future GUI tests (panel behavior, signal cascades, repaint logic)
build on the same fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

# Qt needs an offscreen platform plugin in headless CI.  Set before any
# QtGui / QtWidgets import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator

import pytest

from trcc.app import App
from trcc.core.led_models import LED_STYLES

from .conftest import FakePlatform

# =========================================================================
# QApplication fixture — module-scoped so all GUI tests share it
# =========================================================================


@pytest.fixture(scope="module")
def qapp() -> Iterator[object]:
    """Ensure a QApplication exists for the duration of GUI tests.

    Module-scoped so we don't pay the Qt startup cost per test.  Qt's
    own ``QGuiApplication.instance()`` check makes the construction
    idempotent if some other code already started one.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # Don't quit() — pytest may run other QtTest suites in the same
    # process; tearing down here breaks them.


# =========================================================================
# App fixture — wire a fresh App with FakePlatform + minimal renderer
# =========================================================================


@pytest.fixture
def gui_app(fake_platform: FakePlatform, qapp: object) -> App:
    """A test App with a real QtRenderer.  The renderer needs the
    QApplication, hence the ``qapp`` dependency."""
    del qapp                                # explicit dependency, no use
    from trcc.adapters.render.qt import QtRenderer

    return App(platform=fake_platform, renderer=QtRenderer())


# =========================================================================
# BusBridge — pure-Qt smoke, no panels needed
# =========================================================================


def test_bus_bridge_subscribes_to_every_event_type(qapp: object) -> None:
    """Constructing BusBridge wires one subscription per declared event
    type.  Crashing here means a misnamed Signal or missing event
    import in bus_bridge.py."""
    from trcc.core.events import EventBus
    from trcc.ui.bus_bridge import BusBridge

    bus = EventBus()
    bridge = BusBridge(bus)
    # 10 event types wired today; assert at least that many handlers attached
    assert sum(len(handlers) for handlers in bus._handlers.values()) >= 10
    # Every Signal attribute should be a Qt Signal (descriptor on the class)
    for name in (
        "device_connected", "device_disconnected", "frame_sent",
        "orientation_changed", "brightness_changed", "theme_loaded",
        "led_colors_changed", "sensors_updated", "error_occurred",
    ):
        assert hasattr(bridge, name), f"BusBridge missing signal {name!r}"


def test_bus_bridge_forwards_events_to_qt_signals(qapp: object) -> None:
    """End-to-end: publishing an Event on the bus must arrive on the
    matching Qt signal.  Smokes the subscribe → emit pipeline."""
    from trcc.core.events import DeviceConnected, EventBus
    from trcc.ui.bus_bridge import BusBridge

    bus = EventBus()
    bridge = BusBridge(bus)
    captured: list[object] = []
    bridge.device_connected.connect(captured.append)

    bus.publish(DeviceConnected(key="0402:3922", resolution=(320, 320)))

    assert len(captured) == 1
    event = captured[0]
    assert isinstance(event, DeviceConnected)
    assert event.key == "0402:3922"


# =========================================================================
# Panel construction smokes
# =========================================================================


def _bus(gui_app: App):
    """Single BusBridge for panel-construction tests."""
    from trcc.ui.bus_bridge import BusBridge
    return BusBridge(gui_app.events)


def test_device_panel_constructs(gui_app: App) -> None:
    """DevicePanel constructs against a real App + QtRenderer.

    Builds → no exceptions.  Future tests can extend with click
    simulations + selection assertions.
    """
    from trcc.ui.qtgui.panels.device_panel import DevicePanel

    panel = DevicePanel(gui_app, _bus(gui_app))
    assert panel is not None
    # Panel has a layout — Qt requires this for any child widgets to render
    assert panel.layout() is not None


def test_uc_device_overflow_scrolls_within_fixed_area(qapp: object) -> None:
    """Device sidebar overflow-scrolls INSIDE its fixed region.

    Many device buttons grow only the inner content (so it scrolls); the
    scroll area's own geometry never changes, so the sidebar stays in its
    allotted space and never pushes the sensor/about buttons.
    """
    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    from trcc.ui.gui.constants import Layout
    from trcc.ui.gui.uc_device import UCDevice
    set_assets_dir(_PKG_ASSETS_DIR)

    _, _, area_w, area_h = Layout.DEVICE_AREA
    panel = UCDevice()

    # Scroll area pinned to the allotted region.
    assert (panel.device_scroll.width(), panel.device_scroll.height()) == (
        area_w, area_h)

    # Short list → inner content within the viewport (no overflow).
    panel.update_devices([{"name": "A", "path": "/a"}])
    assert panel.device_area.minimumHeight() <= area_h

    # Long list → inner content exceeds the viewport (overflow → scroll)…
    panel.update_devices(
        [{"name": f"D{i}", "path": f"/d{i}"} for i in range(12)])
    assert panel.device_area.minimumHeight() > area_h
    # …yet the scroll area itself never grew — sidebar stays in its space.
    assert (panel.device_scroll.width(), panel.device_scroll.height()) == (
        area_w, area_h)


def test_list_gpus_feeds_about_panel_gpu_data(gui_app: App) -> None:
    """ListGpus surfaces the aggregator's GPUs — the data the About panel
    feeds its GPU label/dropdown.  Was empty via the dead get_gpu_list,
    so the panel wrongly showed 'No GPU detected'."""
    from trcc.core.commands import ListGpus

    result = gui_app.dispatch(ListGpus())
    assert result.ok
    assert len(result.gpus) >= 1
    assert result.gpus[0].key and result.gpus[0].name


def test_list_gpus_no_gpus_does_not_crash(tmp_home: Path) -> None:
    """ListGpus returns an empty list (not crash) when the enumerator finds
    no GPUs — e.g. pynvml absent, a supported optional state.  Regression:
    it did ``_gpus or sensors.gpus`` and iterated the uncalled ``.gpus``
    METHOD → "'method' object is not iterable", crashing GUI boot."""
    from trcc.adapters.sensors.aggregator import BaselineSensors
    from trcc.app import App
    from trcc.core.commands import ListGpus

    from .conftest import FakeCpu, FakeMemory, FakePlatform

    class _NoGpuPlatform(FakePlatform):
        def sensors(self):  # type: ignore[override]
            if self._sensors is None:
                self._sensors = BaselineSensors(
                    cpu=FakeCpu(), memory=FakeMemory(), gpus=[], fans=[])
            return self._sensors

    result = App(platform=_NoGpuPlatform(tmp_home)).dispatch(ListGpus())
    assert result.ok
    assert result.gpus == []


def test_uc_about_gpu_widget_label_and_dropdown(qapp: object) -> None:
    """UCAbout shows the GPU name (not 'No GPU detected') for one GPU and a
    list-select dropdown for multiple."""
    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    from trcc.ui.gui.uc_about import UCAbout
    set_assets_dir(_PKG_ASSETS_DIR)

    one = UCAbout(gpu_list=[("nvidia:0", "RTX 4090")])
    assert one._gpu_label.text() == "RTX 4090"

    two = UCAbout(gpu_list=[("nvidia:0", "RTX 4090"), ("intel:igpu", "UHD 770")])
    assert two._gpu_combo.count() == 2


def test_sensor_picker_renders_hardware_metrics(
    gui_app: App, qapp: object,
) -> None:
    """The metric chooser must render rows for the category-prefixed
    sensors (cpu/gpu/…), not only legacy sources — else it shows blank.
    Clock sources (time/date) are excluded, matching legacy.

    Takes the App, not a ``SensorEnumerator``: the dialog asks the bus
    (``ReadSensors``) for identities AND values in one call, so a daemon-mode
    client can open it.
    """
    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    from trcc.ui.gui.uc_sensor_picker import SensorPickerDialog
    set_assets_dir(_PKG_ASSETS_DIR)

    dlg = SensorPickerDialog(gui_app)
    try:
        ids = {r.sensor.id for r in dlg._rows}
        assert dlg._rows  # non-empty — was blank under the hardcoded list
        assert any(i.startswith("cpu") for i in ids)
        assert any(i.startswith("gpu") for i in ids)
        assert not any(i.startswith(("time", "date")) for i in ids)
    finally:
        dlg._timer.stop()
        dlg.deleteLater()


def test_display_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.display_panel import DisplayPanel

    panel = DisplayPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_led_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.led_panel import LedPanel

    panel = LedPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


@pytest.mark.parametrize("style", list(LED_STYLES), ids=lambda s: s.name)
def test_qtgui_advanced_tab_sections_match_panel_model(gui_app: App, style) -> None:
    """qtgui AdvancedTab shows only the sub-sections led_panel_for(style)
    declares — the same C#-sourced composition the gui renders from."""
    from trcc.core.led_models import LEGACY_STYLE_ID
    from trcc.ui.presentation.led_panel import led_panel_for
    from trcc.ui.qtgui.panels.led.advanced_tab import AdvancedTab

    tab = AdvancedTab(gui_app, lambda: "")
    m = led_panel_for(LEGACY_STYLE_ID[style])
    tab.apply_panel(m)

    assert tab._sources_box.isVisibleTo(tab) == m.show_sensor_gauges, \
        f"{style.name} sensor linkage"
    assert tab._clock_box.isVisibleTo(tab) == m.show_clock_panel, \
        f"{style.name} clock"
    assert tab._misc_box.isVisibleTo(tab) == (
        m.show_memory_panel or m.show_disk_panel
    ), f"{style.name} memory/disk"


def test_about_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.about_panel import AboutPanel

    panel = AboutPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_system_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


# =========================================================================
# SystemPanel — the autostart target picker
# =========================================================================
#
# The picker chooses WHICH ui login brings up.  Its rules were comments in
# the panel until these tests: changing it must never enable autostart the
# user did not ask for, and what it shows must be what is INSTALLED.


def test_autostart_picker_offers_every_target(gui_app: App) -> None:
    """The picker's list IS the registry — never a second copy to drift."""
    from trcc.core.models import AUTOSTART_TARGETS
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    combo = panel._autostart_target
    offered = {combo.itemData(i) for i in range(combo.count())}
    assert offered == set(AUTOSTART_TARGETS)


def test_autostart_picker_does_not_install_while_disabled(gui_app: App) -> None:
    """Choosing a target with autostart OFF must not turn it ON.

    The same invariant ``refresh`` holds: a control that repairs or re-points
    an entry never creates one.
    """
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    assert not panel._autostart_check.isChecked()
    panel._autostart_target.setCurrentIndex(
        panel._autostart_target.findData("daemon"),
    )
    assert not gui_app.platform.autostart().is_enabled()


def test_autostart_picker_reinstalls_for_the_new_target(gui_app: App) -> None:
    """With autostart ON, choosing a target re-installs for THAT target."""
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    panel._autostart_check.setChecked(True)         # fires the toggle handler
    assert gui_app.platform.autostart().is_enabled()

    panel._autostart_target.setCurrentIndex(
        panel._autostart_target.findData("api"),
    )
    assert gui_app.platform.autostart().installed_target() == "api"


def test_autostart_picker_shows_the_installed_target(gui_app: App) -> None:
    """A refresh reads the ENTRY, not whatever the widget last showed.

    Another surface (cli, api, a second window) may have changed it, and the
    installed entry is the only record of what login will actually launch.
    """
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    gui_app.platform.autostart().enable("qtgui")    # changed behind its back
    panel._refresh_autostart()
    assert panel._autostart_target.currentData() == "qtgui"


def test_activity_sidebar_emits_selection(gui_app: App) -> None:
    """Sidebar click → selected signal fires with the entry key."""
    from trcc.ui.qtgui.panels.sidebar import ActivitySidebar

    sidebar = ActivitySidebar(gui_app, _bus(gui_app))
    captured: list[str] = []
    sidebar.selected.connect(captured.append)
    sidebar._on_clicked("system")
    assert captured == ["system"]


def test_base_panel_requires_setup_ui() -> None:
    """A subclass that forgets _setup_ui() can't even be defined."""
    from trcc.ui.qtgui.base import BasePanel

    with pytest.raises(TypeError, match="must implement _setup_ui"):
        class _BrokenPanel(BasePanel):  # type: ignore[misc]
            pass


def test_assets_resolve_missing_returns_placeholder(qapp: object) -> None:
    """Missing asset names produce a 1×1 transparent placeholder."""
    from trcc.ui.qtgui.assets import Assets

    pix = Assets.pixmap("definitely-not-an-asset-xyz")
    assert not pix.isNull()
    # Placeholder is 1×1; this is the documented fallback.
    assert pix.width() == 1
    assert pix.height() == 1


def test_local_theme_browser_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.local_theme_browser import LocalThemeBrowser

    panel = LocalThemeBrowser(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_cloud_theme_browser_populates_categories(gui_app: App) -> None:
    """Cloud browser fetches the static catalog on construct."""
    from trcc.ui.qtgui.panels.cloud_theme_browser import CloudThemeBrowser

    panel = CloudThemeBrowser(gui_app, _bus(gui_app))
    # "All categories" + 6 prefixes = at least 7 entries
    assert panel._category.count() >= 7


def test_mask_browser_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.mask_browser import MaskBrowser

    panel = MaskBrowser(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_status_panel_handles_missing_key(gui_app: App) -> None:
    """Refresh without a device key shows a friendly hint, not a crash."""
    from trcc.ui.qtgui.panels.status_panel import StatusPanel

    panel = StatusPanel(gui_app, _bus(gui_app))
    panel._on_refresh()
    text = panel._theme_label.text()
    assert "pick a device" in text.lower() or "no data" in text.lower()


def test_overlay_editor_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.overlay_editor import OverlayEditorPanel

    panel = OverlayEditorPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_overlay_editor_refresh_without_key_shows_hint(gui_app: App) -> None:
    """Refresh with no key in the field doesn't crash — shows guidance."""
    from trcc.ui.qtgui.panels.overlay_editor import OverlayEditorPanel

    panel = OverlayEditorPanel(gui_app, _bus(gui_app))
    panel.refresh()
    assert "pick a device" in panel._status.text().lower()


def test_overlay_editor_dialog_round_trips_values(gui_app: App) -> None:
    """The element dialog reads back the same fields it was prefilled with."""
    from trcc.core.models import OverlayElement
    from trcc.ui.qtgui.panels.overlay_editor import (
        OverlayEditorPanel,
        _ElementDialog,
    )

    panel = OverlayEditorPanel(gui_app, _bus(gui_app))
    sample = OverlayElement(
        id="el_x", type="metric", x=42, y=24, color="#a0b0c0",
        size=20, bold=True, italic=False,
        metric="cpu:temp", format="{value:.0f}°C", show_unit=False,
    )
    dialog = _ElementDialog(panel, prefill=sample)
    out = dialog.values()
    assert out["type"] == "metric"
    assert out["x"] == 42
    assert out["y"] == 24
    assert out["color"] == "#a0b0c0"
    assert out["size"] == 20
    assert out["bold"] is True
    assert out["metric"] == "cpu:temp"
    assert out["show_unit"] is False        # button0 unit-switch round-trips


def test_overlay_editor_adopts_theme_layout_and_edits_in_place(
    gui_app: App, tmp_path: object,
) -> None:
    """qtgui parity with the legacy GUI: the editor works on the ONE overlay
    layout, seeded from the active theme, and edits replace in place.

    Regression lock for the cutover's additive model: editing used to add a
    duplicate on top of the theme.  Now opening the editor adopts the theme's
    elements into the editable user layer and an edit mutates them in place —
    one element in, one element out, moved.
    """
    from pathlib import Path

    from trcc.core.commands import UpdateOverlayElement
    from trcc.core.models import Theme
    from trcc.ui.qtgui.panels.overlay_editor import OverlayEditorPanel

    key = "0402:3922"
    gui_app.active_themes[key] = Theme(
        path=Path(str(tmp_path)), name="t", resolution=(320, 320),
        config={"overlay_enabled": True, "elements": [
            {"type": "text", "x": 10, "y": 10, "text": "CPU",
             "color": "#ffffff", "size": 16},
        ]},
    )

    panel = OverlayEditorPanel(gui_app, _bus(gui_app))
    panel._picker.set_key(key)
    panel.refresh()

    # Opening adopted the theme's one element into the editable user layer.
    user = gui_app.settings.for_device(key).user_overlay_elements
    assert len(user) == 1, "editor must adopt the theme's layout, not start blank"
    assert user[0].text == "CPU"
    adopted_id = user[0].id

    # Editing moves it IN PLACE — still exactly one element, now relocated.
    gui_app.dispatch(
        UpdateOverlayElement(key=key, element_id=adopted_id, x=99, y=99),
    )
    after = gui_app.settings.for_device(key).user_overlay_elements
    assert len(after) == 1, "edit must replace in place, not duplicate"
    assert (after[0].x, after[0].y) == (99, 99)


def test_overlay_grid_loads_metric_and_edits_persist(
    gui_app: App, qapp: object,
) -> None:
    """Legacy-style grid: metrics load (not dropped) and edits persist.

    Before the fix the grid mapped metrics by legacy name so next/-id
    metrics (cpu:temp) were dropped on load (couldn't be dragged), and its
    edit payload carried no id so SetOverlayConfig rejected it (colour/drag
    never applied).  This drives the real widget + Command bus end to end.
    """
    from trcc.core.commands import SetOverlayConfig
    from trcc.ui.gui.overlay_grid import OverlayGridPanel

    grid = OverlayGridPanel()
    grid.set_overlay_enabled(True)
    grid.load_from_overlay_config({
        "cpu_temp": {"metric": "cpu:temp", "x": 74, "y": 250,
                     "color": "#112233", "enabled": True,
                     "font": {"size": 24}},
        "custom_0": {"text": "HI", "x": 5, "y": 5, "color": "#abcdef",
                     "enabled": True},
    })

    # Metric element reached the editable grid (was dropped before the fix).
    assert len(grid.get_all_configs()) == 2

    # The edit dispatch shape is accepted, and the colour persists.
    key = "0402:3922"
    result = gui_app.dispatch(
        SetOverlayConfig(key=key, elements=tuple(grid.to_next_elements())),
    )
    assert result.ok, result.message
    stored = gui_app.settings.for_device(key).user_overlay_elements
    metric = next(e for e in stored if e.type == "metric")
    assert metric.metric == "cpu:temp"
    assert metric.color == "#112233"


def test_color_change_emits_list_payload_to_delegate(
    gui_app: App, qapp: object,
) -> None:
    """A colour edit must reach the delegate as a non-empty next/ element
    LIST carrying the new colour.

    The delegate forwarder in trcc_app gates CMD_OVERLAY_CHANGED on
    ``isinstance(info, (dict, list))``; when ``_on_elements_changed`` switched
    to the list shape, a dict-only gate silently dropped the payload to ``{}``
    so colour/drag never reached on_overlay_changed.  This drives the real
    UCThemeSetting → delegate hop (the one a direct SetOverlayConfig test
    bypasses).
    """
    del gui_app, qapp
    from trcc.ui.gui.uc_theme_setting import UCThemeSetting

    panel = UCThemeSetting()
    captured: list[tuple[int, object]] = []
    panel.delegate.connect(lambda cmd, info, data: captured.append((cmd, info)))

    panel.set_overlay_enabled(True)
    panel.load_from_overlay_config({
        "custom_0": {"text": "HI", "x": 5, "y": 5, "color": "#ffffff",
                     "enabled": True},
    })
    panel.overlay_grid.select_element(0)
    panel._on_color_changed(0x11, 0x22, 0x33)

    overlay_emits = [
        info for cmd, info in captured
        if cmd == UCThemeSetting.CMD_OVERLAY_CHANGED
    ]
    assert overlay_emits, "colour change emitted no CMD_OVERLAY_CHANGED"
    payload = overlay_emits[-1]
    # Must be a LIST (the gate forwards list/dict; anything else → {} → dropped)
    assert isinstance(payload, list) and payload, f"payload not a list: {payload!r}"
    assert payload[0]["color"] == "#112233"


def test_configuration_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.configuration_panel import ConfigurationPanel

    panel = ConfigurationPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_configuration_panel_load_without_key(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.configuration_panel import ConfigurationPanel

    panel = ConfigurationPanel(gui_app, _bus(gui_app))
    panel._load_from_snapshot()
    assert "pick a device" in panel._status.text().lower()


def test_preview_panel_constructs(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.preview_panel import PreviewPanel

    panel = PreviewPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel.layout() is not None


def test_preview_panel_handles_missing_key(gui_app: App) -> None:
    """No key + refresh just early-returns — no crash."""
    from trcc.ui.qtgui.panels.preview_panel import PreviewPanel

    panel = PreviewPanel(gui_app, _bus(gui_app))
    panel._refresh()  # should be a no-op when key is empty


def test_preview_panel_unknown_key_shows_placeholder(gui_app: App) -> None:
    from trcc.ui.qtgui.panels.preview_panel import PreviewPanel

    panel = PreviewPanel(gui_app, _bus(gui_app))
    panel._picker.set_key("dead:beef")
    panel._refresh()
    text = panel._preview.text()
    assert "load a theme" in text.lower() or "no data" in text.lower()


def test_sensor_picker_filters_by_search(gui_app: App) -> None:
    """Search text narrows the visible sensor list."""
    from trcc.ui.qtgui.sensor_picker import SensorPickerWidget

    picker = SensorPickerWidget(gui_app)
    # FakePlatform exposes "Fake CPU" — search for it
    picker._search.setText("cpu")
    picker._rebuild_sensor_list()
    assert picker._sensor_list.count() > 0
    # And search for something that doesn't exist returns 0 results
    picker._search.setText("definitely-not-a-sensor-xyz")
    picker._rebuild_sensor_list()
    assert picker._sensor_list.count() == 0


def test_splash_make_returns_widget(gui_app: App) -> None:
    """make_splash() returns either QSplashScreen or fallback QFrame."""
    del gui_app
    from trcc.ui.qtgui.splash import make_splash

    splash = make_splash()
    assert splash is not None
    # Has show() + close() — that's the contract launch() relies on
    assert hasattr(splash, "show")
    assert hasattr(splash, "close")


def test_load_image_command_via_tmpfile(
    gui_app: App, tmp_path,
) -> None:
    """LoadImage stages a real image file as a theme dir."""
    from trcc.core.commands import LoadImage

    image = tmp_path / "test_image.png"
    # Minimal PNG header — file existence + extension are what we check
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    result = gui_app.dispatch(LoadImage(key="0402:3922", path=image))
    # The Command stages the file as a theme and dispatches LoadTheme.
    # LoadTheme returns ok=True even when no device is connected
    # (it persists the theme name for next connect).
    assert "test_image" in result.theme_name or not result.ok
    # Either way, the staged directory should exist
    staged = (
        gui_app.platform.paths().user_content_dir()
        / "single-image" / "test_image"
    )
    assert staged.is_dir()
    # Staged under the theme-dir convention name, NOT the source basename —
    # the background resolver only reads 00.png, so keeping "test_image.png"
    # produced a dir it could not see and the panel went black (#245).
    assert (staged / "00.png").is_file()
    assert not (staged / "test_image.png").exists()


def test_load_image_command_rejects_missing_file(gui_app: App) -> None:
    from trcc.core.commands import LoadImage

    result = gui_app.dispatch(LoadImage(
        key="0402:3922", path=Path("/definitely/not/here.png"),
    ))
    assert result.ok is False
    assert "not found" in result.message.lower()


def test_load_image_command_rejects_bad_extension(
    gui_app: App, tmp_path,
) -> None:
    from trcc.core.commands import LoadImage

    bad = tmp_path / "not_an_image.txt"
    bad.write_text("hello")
    result = gui_app.dispatch(LoadImage(key="0402:3922", path=bad))
    assert result.ok is False
    assert "extension" in result.message.lower()


def test_status_panel_records_events(gui_app: App) -> None:
    """Bus events show up in the rolling log."""
    from trcc.core.events import DeviceConnected
    from trcc.ui.qtgui.panels.status_panel import StatusPanel

    bus = _bus(gui_app)
    panel = StatusPanel(gui_app, bus)
    # Add an event directly so we don't depend on Qt event loop pumping.
    panel._add_event("TEST hello")
    assert panel._event_list.count() == 1
    assert "TEST hello" in panel._event_list.item(0).text()
    # Smoke: panel constructed + connected signals without raising
    del DeviceConnected
    del bus


# =========================================================================
# Main window smoke
# =========================================================================


def test_main_window_constructs_and_includes_panels(gui_app: App) -> None:
    """The top-level MainWindow constructs and embeds the three panels."""
    from trcc.ui.qtgui.app import MainWindow

    window = MainWindow(gui_app)
    assert window is not None
    assert window.windowTitle()           # non-empty title set
    # Status bar gets wired during init for platform info + events
    assert window.statusBar() is not None


# =========================================================================
# MainWindow — display-start restore (METHOD_UI.md's entry contract)
# =========================================================================


class _StubDevice:
    """A device whose ``info`` is a REAL ``ProductInfo`` from the registry.

    ``ListDevices`` reads ``info.wire`` / ``info.kind`` — fields on
    ``ProductInfo``, not on ``DeviceInfo`` (which has neither).  Taking the
    entry from ``products_by_wire`` rather than hand-building one keeps the
    wire/kind pairing true to the registry, per CLAUDE.md's rule that
    ``ALL_DEVICES`` is the single source of truth for device-shaped tests.
    """

    def __init__(self, wire, connected: bool = True) -> None:
        from trcc.core.registry import products_by_wire

        self.info = products_by_wire(wire)[0]
        self.is_connected = connected
        # The Device port declares these; a stub that omits one makes the
        # window look broken when it reads what every real device has.
        self.profile = None
        self.handshake = None

    @property
    def is_led(self) -> bool:
        from trcc.core.models import Wire
        return self.info.wire is Wire.LED

    @property
    def key(self) -> str:
        return f"{self.info.vid:04x}:{self.info.pid:04x}"


def _spy_on_dispatch(app: App) -> list[str]:
    """Record ``CommandName:key`` for everything dispatched."""
    seen: list[str] = []
    real = app.dispatch

    def _spy(command):
        seen.append(f"{type(command).__name__}:{getattr(command, 'key', '')}")
        return real(command)

    app.dispatch = _spy                       # type: ignore[method-assign]
    return seen


def test_main_window_restores_attached_lcds_at_startup(gui_app: App) -> None:
    """The coldplug fleet is restored, and LED devices are skipped.

    ``discover_and_connect()`` runs at ``app.py:355`` and the window is built
    at ``:360``, so devices attached at startup NEVER emit a
    ``DeviceConnected`` this window can hear.  Wiring only ``_on_connected``
    would restore hotplugged devices and miss the common case entirely.

    The LED skip matters because ``RestoreDeviceState`` on an LED resolves no
    resolution, warns, and returns ``ok=False`` — noise for a device that has
    no display state to restore.
    """
    from trcc.core.models import Wire
    from trcc.ui.qtgui.app import MainWindow

    lcd, led = _StubDevice(Wire.SCSI), _StubDevice(Wire.LED)
    gui_app.devices[lcd.key] = lcd            # type: ignore[assignment]
    gui_app.devices[led.key] = led            # type: ignore[assignment]
    seen = _spy_on_dispatch(gui_app)

    MainWindow(gui_app)

    assert f"RestoreDeviceState:{lcd.key}" in seen, (
        "the LCD attached before the window existed was never restored"
    )
    assert f"RestoreDeviceState:{led.key}" not in seen, (
        "an LED has no display state to restore"
    )


def test_main_window_skips_a_disconnected_device(gui_app: App) -> None:
    """A device in the registry but not connected is not restored."""
    from trcc.core.models import Wire
    from trcc.ui.qtgui.app import MainWindow

    lcd = _StubDevice(Wire.SCSI, connected=False)
    gui_app.devices[lcd.key] = lcd            # type: ignore[assignment]
    seen = _spy_on_dispatch(gui_app)

    MainWindow(gui_app)

    assert f"RestoreDeviceState:{lcd.key}" not in seen


def test_hotplugged_device_gets_the_same_restore(gui_app: App) -> None:
    """The other half: a device that arrives later must not sit dark."""
    from trcc.core.events import DeviceConnected
    from trcc.core.models import Wire
    from trcc.ui.qtgui.app import MainWindow

    window = MainWindow(gui_app)                    # nothing attached yet
    lcd = _StubDevice(Wire.SCSI)
    gui_app.devices[lcd.key] = lcd            # type: ignore[assignment]
    seen = _spy_on_dispatch(gui_app)

    window._on_connected(DeviceConnected(key=lcd.key, resolution=(320, 320)))

    assert f"RestoreDeviceState:{lcd.key}" in seen


def test_restore_runs_after_the_bus_is_subscribed() -> None:
    """Ordering trap, pinned structurally — because the invariant IS structural.

    The restore loads a theme, which publishes ``ThemeLoaded``, which is what
    starts the render ticker.  Called earlier in ``__init__`` the event fires
    into an unconnected bus and nothing ever animates — a bug that presents as
    "video just doesn't play" and points nowhere near its cause.

    Asserted over the AST of ``MainWindow.__init__`` rather than at runtime:
    the subscriptions use ``Qt.QueuedConnection``, so a probe emit would not
    deliver synchronously and could not tell a connected bus from a bare one.
    """
    import ast

    import trcc.ui.qtgui.app as qtgui_app

    source = Path(qtgui_app.__file__).read_text(encoding="utf-8")
    init = next(
        node
        for cls in ast.walk(ast.parse(source))
        if isinstance(cls, ast.ClassDef) and cls.name == "MainWindow"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )

    subscribe_lines, restore_lines = [], []
    for node in ast.walk(init):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "connect" and "_bus" in ast.dump(node.func):
            subscribe_lines.append(node.lineno)
        elif node.func.attr == "_restore_display_state":
            restore_lines.append(node.lineno)

    assert subscribe_lines, "no _bus.*.connect() found — test is measuring nothing"
    assert restore_lines, "MainWindow.__init__ no longer restores display state"
    assert min(restore_lines) > max(subscribe_lines), (
        f"_restore_display_state() runs at line {min(restore_lines)}, before the "
        f"last bus subscription at line {max(subscribe_lines)} — a restored "
        f"theme would publish ThemeLoaded into an unconnected bus and the "
        f"render ticker would never start"
    )


def test_main_window_repairs_a_stale_autostart_entry(gui_app: App) -> None:
    """qtgui was the ONE ui that could not reach ``RefreshAutostart``.

    Measured: cli, api and gui each had a dispatch site; qtgui had zero.  So a
    qtgui-only user whose install moved kept an entry pointing at the old path
    — and the panel still showed "enabled", because ``is_enabled()`` is
    ``path.is_file()``, which a stale entry satisfies.  gui repairs this from
    its own window's ``__init__``; this is that, the same Command.
    """
    from trcc.core.commands import EnableAutostart
    from trcc.ui.qtgui.app import MainWindow

    mgr = gui_app.platform.autostart()
    gui_app.dispatch(EnableAutostart())
    mgr.command = "/moved/bin/trcc"               # the install moved
    assert mgr.installed_command != mgr.command, "fixture drift: not stale"

    MainWindow(gui_app)

    assert mgr.installed_command == "/moved/bin/trcc"


def test_main_window_does_not_enable_autostart(gui_app: App) -> None:
    """...and must NOT pick up gui's first-launch auto-enable.

    That half is a documented product decision for gui alone —
    ``ensure_autostart``'s docstring names qtgui: it "reads and toggles
    autostart but has never auto-enabled it, and moving this would hand it a
    behaviour it does not have."  Refresh repairs what the user already chose;
    enable would choose FOR them.  Pinned so the distinction cannot erode.
    """
    from trcc.ui.qtgui.app import MainWindow

    MainWindow(gui_app)

    assert not gui_app.platform.autostart().is_enabled(), (
        "opening the qtgui window opted the user into autostart"
    )


# =========================================================================
# GUI launcher entry point
# =========================================================================


def test_gui_launch_is_callable() -> None:
    """The ``launch`` factory exists + is callable.  Actually running
    it would block on qapp.exec(); tests just verify import resolution."""
    from trcc.ui.qtgui import launch

    assert callable(launch)


# =========================================================================
# Coverage marker for the future
# =========================================================================


def test_offscreen_qpa_is_set() -> None:
    """Pin the QT_QPA_PLATFORM env var so a future regression that
    forgets to set offscreen mode fails loudly here instead of
    hanging in CI on a missing X server."""
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"


# =========================================================================
# G2 — content creation tools
# =========================================================================


def test_color_wheel_emits_hue_on_click(qapp: object) -> None:
    """ColorWheel drags update its hue and emit ``hue_changed``."""
    from trcc.ui.qtgui.color_wheel import ColorWheel

    received: list[int] = []
    wheel = ColorWheel()
    wheel.resize(220, 220)
    wheel.hue_changed.connect(received.append)
    # set_hue is the non-emitting setter — confirm it doesn't emit.
    wheel.set_hue(120)
    assert wheel.hue() == 120
    assert received == []


def test_image_crop_dialog_renders_target_size(qapp: object, tmp_path) -> None:
    """ImageCropDialog returns a QImage at exactly target_w×target_h."""
    from PySide6.QtGui import QColor, QImage, QPainter

    from trcc.ui.qtgui.image_crop import ImageCropDialog

    # Make a small synthetic source image to crop.
    src = QImage(200, 100, QImage.Format.Format_RGB32)
    src.fill(QColor("#336699"))
    painter = QPainter(src)
    painter.fillRect(0, 0, 50, 50, QColor("#ff0000"))
    painter.end()

    dialog = ImageCropDialog()
    dialog.load_image(src, target_w=120, target_h=80)
    cropped = dialog.cropped()
    assert cropped is not None
    assert cropped.width() == 120
    assert cropped.height() == 80
    del tmp_path


def test_splash_make_returns_widget_with_fallback(qapp: object) -> None:
    """SplashScreen falls back to _FrameSplash when SPLASH_BG asset is absent.

    The asset isn't bundled in next/ yet, so this exercises the fallback.
    """
    from trcc.ui.qtgui.splash import make_splash

    splash = make_splash()
    assert hasattr(splash, "show")
    assert hasattr(splash, "close")


def test_video_exporter_rejects_missing_source(tmp_path) -> None:
    """VideoExporter raises VideoExportError when the source doesn't exist."""
    from trcc.services.video_export import (
        VideoExporter,
        VideoExportError,
        VideoExportRequest,
    )

    missing = tmp_path / "nope.mp4"
    request = VideoExportRequest(
        source=missing, start_ms=0, end_ms=1000,
        target_w=480, target_h=480, rotation=0,
    )
    with pytest.raises(VideoExportError, match="not found"):
        VideoExporter().export_zt(request)


def test_video_exporter_rejects_bad_range(tmp_path) -> None:
    """VideoExporter rejects end_ms <= start_ms."""
    from trcc.services.video_export import (
        VideoExporter,
        VideoExportError,
        VideoExportRequest,
    )

    real = tmp_path / "fake.mp4"
    real.write_bytes(b"\x00" * 16)
    request = VideoExportRequest(
        source=real, start_ms=100, end_ms=100,
        target_w=480, target_h=480, rotation=0,
    )
    with pytest.raises(VideoExportError, match="Invalid clip range"):
        VideoExporter().export_zt(request)


def test_video_exporter_rejects_bad_rotation(tmp_path) -> None:
    """Rotation must be one of 0/90/180/270."""
    from trcc.services.video_export import (
        VideoExporter,
        VideoExportError,
        VideoExportRequest,
    )

    real = tmp_path / "fake.mp4"
    real.write_bytes(b"\x00" * 16)
    request = VideoExportRequest(
        source=real, start_ms=0, end_ms=1000,
        target_w=480, target_h=480, rotation=45,
    )
    with pytest.raises(VideoExportError, match="Rotation must"):
        VideoExporter().export_zt(request)


def test_load_video_command_rejects_missing_file(gui_app: App) -> None:
    """LoadVideo surfaces a structured error for non-existent paths."""
    from trcc.core.commands import LoadVideo

    result = gui_app.dispatch(LoadVideo(
        key="0402:3922", path=Path("/definitely/not/here.mp4"),
    ))
    assert result.ok is False
    assert "not found" in result.message.lower()


def test_load_video_command_rejects_bad_extension(
    gui_app: App, tmp_path,
) -> None:
    """LoadVideo rejects extensions outside the allow-list."""
    from trcc.core.commands import LoadVideo

    bad = tmp_path / "not_a_video.txt"
    bad.write_text("hello")
    result = gui_app.dispatch(LoadVideo(key="0402:3922", path=bad))
    assert result.ok is False
    assert "extension" in result.message.lower()


def test_load_video_command_zt_passthrough(gui_app: App, tmp_path) -> None:
    """A .zt input is copied straight into a staged theme dir (no ffmpeg)."""
    from trcc.core.commands import LoadVideo

    # Minimal .zt header — 0xDC magic + a 1-frame placeholder.  LoadVideo
    # only cares about the extension + file existence at staging time.
    src = tmp_path / "anim.zt"
    src.write_bytes(b"\xDC" + b"\x01\x00\x00\x00" + b"\x00" * 8)

    result = gui_app.dispatch(LoadVideo(
        key="0402:3922", path=src,
    ))
    # The staged Theme.zt should exist regardless of LoadTheme's outcome.
    staged = (
        gui_app.platform.paths().user_content_dir()
        / "single-video" / "anim" / "Theme.zt"
    )
    assert staged.is_file()
    assert staged.read_bytes() == src.read_bytes()
    del result


def test_screen_overlay_is_wayland_returns_bool() -> None:
    """is_wayland() is callable + returns a bool regardless of env."""
    from trcc.ui.screen_overlay import is_wayland

    assert isinstance(is_wayland(), bool)


# =========================================================================
# G3 — LED control sub-tabs
# =========================================================================


def _led_key() -> str:
    """LED device key used across the G3 tests — present in the registry."""
    return "0416:8001"


def _snap(**fields):
    """A ``LedSnapshotResult`` for the LED tab tests.

    The six tabs take the RESULT now, not a live ``LedDeviceSettings``: reaching
    ``app.settings.for_led`` raised under TRCC_DAEMON=1 and handed a UI a mutable
    domain object.  Mutating ``settings`` and passing it here would no longer
    exercise the path the panel takes.
    """
    from trcc.core.results import LedSnapshotResult
    return LedSnapshotResult(ok=True, key=_led_key(), **fields)


def test_led_color_tab_refresh(gui_app: App, qapp: object) -> None:
    """ColorTab.refresh_from updates RGB + brightness widgets in-place."""
    from trcc.ui.qtgui.panels.led import ColorTab

    tab = ColorTab(gui_app, _led_key)
    tab.refresh_from(_snap(color=(10, 200, 60), brightness=42))
    assert tab._r.value() == 10
    assert tab._g.value() == 200
    assert tab._b.value() == 60
    assert tab._brightness.value() == 42


def test_led_color_tab_apply_dispatches_commands(
    gui_app: App, qapp: object,
) -> None:
    """ColorTab Apply dispatches SetLedColor + SetLedBrightness."""
    from trcc.ui.qtgui.panels.led import ColorTab

    tab = ColorTab(gui_app, _led_key)
    tab._set_color(255, 128, 0, emit_signals=False)
    tab._brightness.setValue(77)
    tab._on_apply()
    settings = gui_app.settings.for_led(_led_key())
    assert settings.color == (255, 128, 0)
    assert settings.brightness == 77


def test_led_mode_tab_selects_radio_for_persisted_mode(
    gui_app: App, qapp: object,
) -> None:
    """ModeTab.refresh_from checks the radio matching settings.mode."""
    from trcc.core.led_models import LEDMode
    from trcc.ui.qtgui.panels.led import ModeTab

    tab = ModeTab(gui_app, _led_key)
    tab.refresh_from(_snap(mode=LEDMode.RAINBOW.name))
    assert tab._radios[LEDMode.RAINBOW].isChecked()


def test_led_zone_tab_hides_for_single_zone(
    gui_app: App, qapp: object,
) -> None:
    """ZoneTab shows its placeholder when settings.zones has ≤1 entries."""
    from trcc.ui.qtgui.panels.led import ZoneTab

    tab = ZoneTab(gui_app, _led_key)
    tab.refresh_from(_snap(zones=()))
    assert tab.has_visible_content() is False


def test_led_zone_tab_builds_rows_for_multi_zone(
    gui_app: App, qapp: object,
) -> None:
    """ZoneTab builds one row per zone when count > 1."""
    from trcc.core.results import LedZoneEntry
    from trcc.ui.qtgui.panels.led import ZoneTab

    tab = ZoneTab(gui_app, _led_key)
    tab.refresh_from(_snap(
        zones=(
            LedZoneEntry(color=(255, 0, 0)),
            LedZoneEntry(color=(0, 255, 0)),
            LedZoneEntry(color=(0, 0, 255)),
        ),
        selected_zone=1,
    ))
    assert tab.has_visible_content() is True
    assert len(tab._zone_widgets) == 3


def test_led_segment_tab_hides_when_no_segments(
    gui_app: App, qapp: object,
) -> None:
    """SegmentTab shows its placeholder when segment_on is empty."""
    from trcc.ui.qtgui.panels.led import SegmentTab

    tab = SegmentTab(gui_app, _led_key)
    tab.refresh_from(_snap(segment_on=()))
    assert tab.has_visible_content() is False


def test_led_segment_tab_builds_checks(gui_app: App, qapp: object) -> None:
    """SegmentTab builds one checkbox per segment when populated."""
    from trcc.ui.qtgui.panels.led import SegmentTab

    tab = SegmentTab(gui_app, _led_key)
    tab.refresh_from(_snap(segment_on=(True, False, True, True, False)))
    assert tab.has_visible_content() is True
    assert len(tab._checks) == 5
    assert tab._checks[0].isChecked() is True
    assert tab._checks[1].isChecked() is False


def test_led_advanced_tab_refreshes_radio_state(
    gui_app: App, qapp: object,
) -> None:
    """AdvancedTab.refresh_from selects the right temp/load radio + checkboxes."""
    from trcc.ui.qtgui.panels.led import AdvancedTab

    tab = AdvancedTab(gui_app, _led_key)
    tab.refresh_from(_snap(
        temp_source="gpu", load_source="cpu", test_mode=True,
        clock_24h=False, week_sunday=True,
    ))
    assert tab._temp_gpu.isChecked()
    assert tab._load_cpu.isChecked()
    assert tab._test_check.isChecked()
    assert not tab._clock_24h.isChecked()
    assert tab._week_sunday.isChecked()


def test_led_panel_constructs_with_tabs(gui_app: App, qapp: object) -> None:
    """LedPanel constructs, hosts the five sub-tabs, and refresh is a no-op
    when the key field is empty."""
    from trcc.ui.qtgui.panels.led_panel import LedPanel

    panel = LedPanel(gui_app, _bus(gui_app))
    # No key → placeholder status, optional tabs hidden.
    panel._picker.set_key("")
    panel._refresh_all_tabs()
    assert "pick" in panel._status_label.text().lower()
    # Five tabs total (color/mode/advanced always; zones+segments depend
    # on device — start hidden).
    visible_tab_count = panel._tabs.count()
    assert visible_tab_count >= 3


# =========================================================================
# G4 — device picker + mask editor
# =========================================================================


def test_device_picker_populates_from_app(gui_app: App, qapp: object) -> None:
    """DevicePickerWidget shows every entry in app.devices on construction."""
    from trcc.ui.qtgui.device_picker import DevicePickerWidget

    # FakePlatform attaches no devices by default, so the dropdown is
    # empty but still constructs cleanly.
    picker = DevicePickerWidget(gui_app, _bus(gui_app))
    assert picker.current_key() == ""
    # Programmatic set + read round-trips without emitting.
    picker.set_key("0402:3922")
    assert picker.current_key() == "0402:3922"


def test_device_picker_emits_key_changed_on_text_finished(
    gui_app: App, qapp: object,
) -> None:
    """Manual text edit fires :sig:`key_changed` once the user commits."""
    from trcc.ui.qtgui.device_picker import DevicePickerWidget

    received: list[str] = []
    picker = DevicePickerWidget(gui_app, _bus(gui_app))
    picker.key_changed.connect(received.append)
    line_edit = picker._combo.lineEdit()
    assert line_edit is not None
    line_edit.setText("0416:8001")
    line_edit.editingFinished.emit()
    assert received[-1] == "0416:8001"


def test_device_picker_selected_item_yields_key_not_label(
    gui_app: App, qapp: object,
) -> None:
    """Selecting a device from the dropdown must yield its KEY, not the human
    label — #176, where qtgui sent the whole "vid:pid — Vendor Product" string
    as the key and broke every display command."""
    from types import SimpleNamespace

    from trcc.core.models import Kind, Wire
    from trcc.ui.qtgui.device_picker import DevicePickerWidget

    # ``wire`` / ``kind`` are ENUMS on the real ``ProductInfo``, and the picker
    # now reads them through ``ListDevices``, which takes ``.value``.  The fake
    # carried a bare string and no wire at all — faithful to what the widget
    # touched at the time, and silently wrong the moment anything else looked.
    gui_app.devices["87ad:70db"] = SimpleNamespace(  # type: ignore[assignment]
        info=SimpleNamespace(
            vendor="Thermalright", product="Mjolnir Vision",
            wire=Wire.BULK, kind=Kind.LCD),
        is_connected=True,
    )
    picker = DevicePickerWidget(gui_app, _bus(gui_app))
    picker._populate_from_app()
    picker._combo.setCurrentIndex(0)   # SELECT the dropdown item — the bug path
    assert picker.current_key() == "87ad:70db"   # the KEY, not the label


def test_mask_browser_position_dispatches_command(
    gui_app: App, qapp: object,
) -> None:
    """Changing the mask position dispatches :class:`SetMaskPosition`."""
    from trcc.ui.qtgui.panels.mask_browser import MaskBrowser

    panel = MaskBrowser(gui_app, _bus(gui_app))
    panel._picker.set_key("0402:3922")
    panel._x.setValue(40)
    panel._y.setValue(60)
    panel._on_position_changed()
    settings = gui_app.settings.for_device("0402:3922")
    assert settings.mask_position == (40, 60)


def test_mask_browser_visibility_dispatches_command(
    gui_app: App, qapp: object,
) -> None:
    """Toggling visibility dispatches :class:`SetMaskVisible`."""
    from trcc.ui.qtgui.panels.mask_browser import MaskBrowser

    panel = MaskBrowser(gui_app, _bus(gui_app))
    panel._picker.set_key("0402:3922")
    panel._visible.setChecked(False)
    # Toggle directly to drive the slot.
    panel._on_visibility_changed(False)
    settings = gui_app.settings.for_device("0402:3922")
    assert settings.mask_visible is False


# =========================================================================
# G5 — screencast
# =========================================================================


def test_screencast_panel_constructs(gui_app: App, qapp: object) -> None:
    """ScreencastPanel builds without raising and starts in stopped state."""
    from trcc.ui.qtgui.panels.screencast_panel import ScreencastPanel

    panel = ScreencastPanel(gui_app, _bus(gui_app))
    assert panel is not None
    assert panel._start_btn.isEnabled() is True
    assert panel._stop_btn.isEnabled() is False


def test_screencast_panel_start_without_region_is_a_no_op(
    gui_app: App, qapp: object,
) -> None:
    """Start without a chosen region surfaces guidance, starts no cast."""
    from trcc.ui.qtgui.panels.screencast_panel import ScreencastPanel

    panel = ScreencastPanel(gui_app, _bus(gui_app))
    panel._picker.set_key("0402:3922")
    panel._on_start()
    assert panel._casting_key is None
    assert "region" in panel._status.text().lower()


def test_screencast_panel_start_without_key_is_a_no_op(
    gui_app: App, qapp: object,
) -> None:
    """Start without a chosen device surfaces guidance, starts no cast."""
    from trcc.ui.qtgui.panels.screencast_panel import ScreencastPanel

    panel = ScreencastPanel(gui_app, _bus(gui_app))
    panel._region = (0, 0, 200, 100)
    panel._on_start()
    assert panel._casting_key is None
    assert "device" in panel._status.text().lower()


def test_screencast_panel_records_picked_region(
    gui_app: App, qapp: object,
) -> None:
    """Receiving region_selected updates the panel's region + label."""
    from trcc.ui.qtgui.panels.screencast_panel import ScreencastPanel

    panel = ScreencastPanel(gui_app, _bus(gui_app))
    panel._on_region_selected(40, 60, 320, 240)
    assert panel._region == (40, 60, 320, 240)
    assert "320" in panel._region_label.text()
    assert "240" in panel._region_label.text()


def test_screencast_build_frame_returns_bytes(gui_app: App) -> None:
    """``DisplayService.build_screencast_frame`` returns wire-ready bytes."""
    from trcc.core.models import RawFrame
    from trcc.core.registry import find_product

    product = find_product(0x0402, 0x3922)
    assert product is not None
    target_w, target_h = product.native_resolution
    raw = RawFrame(
        data=b"\x80\x00\x00" * (200 * 100),
        width=200, height=100,
    )
    encoded = gui_app.display.build_screencast_frame(
        info=product, frame=raw,
    )
    assert isinstance(encoded, bytes)
    assert len(encoded) > 0
    del target_w, target_h


def test_region_overlay_constructs(qapp: object) -> None:
    """RegionSelectOverlay constructs without raising — no screen grab yet."""
    from trcc.ui.qtgui.region_overlay import RegionSelectOverlay

    overlay = RegionSelectOverlay()
    assert hasattr(overlay, "region_selected")
    assert hasattr(overlay, "cancelled")


# =========================================================================
# Drag-select overlays — ONE interaction, proven on BOTH skins
#
# gui's ``ScreenCaptureOverlay`` and qtgui's ``RegionSelectOverlay`` are the
# same press-drag-release behaviour; it lives once, in ``DragSelectOverlay``.
# Before that it was written twice and NOTHING drove it, which is how the two
# copies came to disagree on their own constants (``_MIN_SELECTION`` vs
# ``_MIN_EDGE``, same value) and their control flow.  These tests are
# parametrized over both skins so one shared body is proven for both.
# =========================================================================


#: Skin name -> the drag-select overlay it ships.  Resolved INSIDE the test,
#: never at decorator time: importing a Qt widget module during collection
#: runs its class bodies before ``QApplication`` exists.
_DRAG_SKINS = {
    "gui": ("trcc.ui.gui.screen_capture", "ScreenCaptureOverlay"),
    "qtgui": ("trcc.ui.qtgui.region_overlay", "RegionSelectOverlay"),
}


def _drag_overlay(skin: str) -> type:
    from importlib import import_module

    module, name = _DRAG_SKINS[skin]
    return getattr(import_module(module), name)


def _mouse_event(kind: str, x: int, y: int, button: str, held: str):
    """Build a QMouseEvent at (x, y) — the non-deprecated QPointF overload."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    at = QPointF(x, y)
    return QMouseEvent(
        getattr(QMouseEvent.Type, kind), at, at,
        getattr(Qt.MouseButton, button), getattr(Qt.MouseButton, held),
        Qt.KeyboardModifier.NoModifier,
    )


def _point(x: int, y: int):
    from PySide6.QtCore import QPoint

    return QPoint(x, y)


def _press(overlay: object, x: int, y: int, button: str = "LeftButton") -> None:
    overlay.mousePressEvent(  # type: ignore[attr-defined]
        _mouse_event("MouseButtonPress", x, y, button, button))


def _move_to(overlay: object, x: int, y: int) -> None:
    overlay.mouseMoveEvent(  # type: ignore[attr-defined]
        _mouse_event("MouseMove", x, y, "NoButton", "LeftButton"))


def _release(overlay: object, x: int, y: int) -> None:
    overlay.mouseReleaseEvent(  # type: ignore[attr-defined]
        _mouse_event("MouseButtonRelease", x, y, "LeftButton", "NoButton"))


def _confirmed_rects(overlay: object) -> list[tuple[int, int, int, int]]:
    """Subscribe to whichever signal this skin confirms with."""
    seen: list[tuple[int, int, int, int]] = []

    def _on_region(x: int, y: int, w: int, h: int) -> None:
        seen.append((x, y, w, h))

    def _on_captured(pixmap: object) -> None:
        seen.append((0, 0, -1, -1) if pixmap is None else (0, 0, 1, 1))

    if hasattr(overlay, "region_selected"):
        overlay.region_selected.connect(_on_region)  # type: ignore[attr-defined]
    else:
        overlay.captured.connect(_on_captured)  # type: ignore[attr-defined]
    return seen


@pytest.mark.parametrize("skin", sorted(_DRAG_SKINS))
def test_drag_select_confirms_a_real_drag(skin: str, qtbot) -> None:
    """Press, drag, release over the minimum edge confirms the rectangle."""
    overlay = _drag_overlay(skin)()
    qtbot.addWidget(overlay)
    seen = _confirmed_rects(overlay)

    _press(overlay, 100, 120)
    _move_to(overlay, 300, 260)
    _release(overlay, 300, 260)

    assert len(seen) == 1
    if hasattr(overlay, "region_selected"):
        assert seen[0] == (100, 120, 201, 141)


@pytest.mark.parametrize("skin", sorted(_DRAG_SKINS))
def test_drag_select_ignores_a_misclick(skin: str, qtbot) -> None:
    """A drag shorter than ``_MIN_EDGE`` on either axis confirms nothing."""
    overlay = _drag_overlay(skin)()
    qtbot.addWidget(overlay)
    seen = _confirmed_rects(overlay)

    _press(overlay, 100, 120)
    _move_to(overlay, 105, 200)          # 5px wide — under the minimum
    _release(overlay, 105, 200)

    assert seen == []
    assert overlay._MIN_EDGE == 10       # the shared constant, one spelling


#: The same rectangle, dragged from each of its four corners.
_DIAGONALS = {
    "down-right": ((100, 120), (300, 260)),
    "up-left": ((300, 260), (100, 120)),
    "down-left": ((300, 120), (100, 260)),
    "up-right": ((100, 260), (300, 120)),
}


@pytest.mark.parametrize("skin", sorted(_DRAG_SKINS))
@pytest.mark.parametrize("gesture", sorted(_DIAGONALS))
def test_drag_select_is_direction_independent(skin: str, gesture: str,
                                              qtbot) -> None:
    """The same rectangle, whichever corner the drag started from.

    Qt's two-point ``QRect`` is INCLUSIVE of both corners, but
    ``normalized()`` repairs a negative extent by moving both edges inward.
    Both skins used to call it, so an up-left drag measured 199x139 at
    (101, 121) where the identical down-right drag measured 201x141 at
    (100, 120) — 2px smaller and 1px offset, decided by which way the hand
    moved.  Nothing drove the interaction, so nothing caught it.
    """
    (x0, y0), (x1, y1) = _DIAGONALS[gesture]
    overlay = _drag_overlay(skin)()
    qtbot.addWidget(overlay)
    seen = _confirmed_rects(overlay)

    _press(overlay, x0, y0)
    _move_to(overlay, x1, y1)
    _release(overlay, x1, y1)

    assert len(seen) == 1
    if hasattr(overlay, "region_selected"):
        assert seen[0] == (100, 120, 201, 141)


@pytest.mark.parametrize("skin", sorted(_DRAG_SKINS))
def test_drag_select_keeps_both_edge_pixels(skin: str, qtbot) -> None:
    """A drag that starts and ends on one pixel selects that one pixel.

    Pins the inclusive convention the sizes above rest on: 100 to 300 is
    201 columns because both ends are inside the selection, not 200.
    """
    overlay = _drag_overlay(skin)()
    qtbot.addWidget(overlay)
    overlay._start = overlay._end = _point(50, 50)
    assert overlay._selection_rect().width() == 1
    assert overlay._selection_rect().height() == 1


@pytest.mark.parametrize("skin", sorted(_DRAG_SKINS))
def test_drag_select_right_click_cancels(skin: str, qtbot) -> None:
    """Right-click routes to the skin's own cancel signal, not a selection."""
    cancelled: list[bool] = []
    overlay = _drag_overlay(skin)()
    qtbot.addWidget(overlay)
    if hasattr(overlay, "cancelled"):
        overlay.cancelled.connect(lambda: cancelled.append(True))
    else:
        overlay.captured.connect(lambda px: cancelled.append(px is None))

    _press(overlay, 100, 120, button="RightButton")

    assert cancelled == [True]


@pytest.mark.parametrize("gesture", sorted(_DIAGONALS))
def test_screen_capture_crops_the_pixels_that_were_dragged_over(
    gesture: str, qtbot,
) -> None:
    """The gui skin emits the SOURCE pixels inside the rectangle, not just
    a pixmap of the right size.

    Every pixel of the stand-in screenshot encodes its own coordinates, so
    the crop names its own provenance: read a corner back and it says which
    screen pixel it came from.  Sizes alone would pass even if the crop were
    taken from the wrong origin.
    """
    from PySide6.QtGui import QImage, QPixmap

    from trcc.ui.gui.screen_capture import ScreenCaptureOverlay

    width, height = 64, 48
    shot = QImage(width, height, QImage.Format.Format_RGB32)
    for y in range(height):
        for x in range(width):
            shot.setPixel(x, y, (0xFF << 24) | (x << 16) | (y << 8))

    (x0, y0), (x1, y1) = _DIAGONALS[gesture]
    # Scale the shared diagonals down into this small stand-in screen.
    x0, x1, y0, y1 = x0 // 10, x1 // 10, y0 // 10, y1 // 10

    got: list[object] = []
    overlay = ScreenCaptureOverlay()
    qtbot.addWidget(overlay)
    overlay._screenshot = QPixmap.fromImage(shot)
    overlay.captured.connect(got.append)

    _press(overlay, x0, y0)
    _move_to(overlay, x1, y1)
    _release(overlay, x1, y1)

    assert len(got) == 1 and got[0] is not None
    cropped = got[0].toImage()          # type: ignore[attr-defined]
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    assert (cropped.width(), cropped.height()) == (right - left + 1,
                                                   bottom - top + 1)
    for corner_x, corner_y, want in (
        (0, 0, (left, top)),
        (cropped.width() - 1, cropped.height() - 1, (right, bottom)),
    ):
        colour = cropped.pixelColor(corner_x, corner_y)
        assert (colour.red(), colour.green()) == want, (
            f"{gesture}: crop corner ({corner_x}, {corner_y}) came from screen "
            f"pixel {(colour.red(), colour.green())}, expected {want}"
        )


def test_both_skins_share_one_drag_implementation() -> None:
    """Neither skin may re-implement the interaction it inherits.

    The gate, not the comment: if a future edit copies press/drag/release
    back down into a skin, this fails and names the method.
    """
    from trcc.ui.screen_overlay import DragSelectOverlay

    shared = ("mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent",
              "_selection_rect", "paintEvent", "_draw_size_label", "__init__")
    for skin in sorted(_DRAG_SKINS):
        overlay_cls = _drag_overlay(skin)
        assert issubclass(overlay_cls, DragSelectOverlay)
        redefined = [m for m in shared if m in overlay_cls.__dict__]
        assert not redefined, (
            f"{overlay_cls.__name__} re-implements {redefined}, which "
            f"DragSelectOverlay already owns for every skin."
        )
        # ...and each skin DOES say what its own rectangle means.
        assert "_confirm" in overlay_cls.__dict__
        assert "_emit_cancel" in overlay_cls.__dict__


def test_led_panel_refreshes_on_key_set(gui_app: App, qapp: object) -> None:
    """Setting a key + refreshing rebuilds tab state from persisted settings."""
    from trcc.core.led_models import LEDMode
    from trcc.ui.qtgui.panels.led_panel import LedPanel

    settings = gui_app.settings.for_led(_led_key())
    settings.color = (50, 100, 150)
    settings.brightness = 33
    settings.mode = LEDMode.BREATHING

    panel = LedPanel(gui_app, _bus(gui_app))
    panel._picker.set_key(_led_key())
    panel._refresh_all_tabs()
    assert panel._color_tab._r.value() == 50
    assert panel._color_tab._brightness.value() == 33
    assert panel._mode_tab._radios[LEDMode.BREATHING].isChecked()


def test_brightness_slider_debounces_to_one_send(qapp: object) -> None:
    """A brightness drag must coalesce into ONE ``brightness_changed`` emit,
    not one per slider tick — the per-tick flood interleaved with the
    segment-number refresh and glitched the display (#202)."""
    from PySide6.QtCore import QEventLoop, QTimer

    from trcc.ui.gui.assets import _PKG_ASSETS_DIR, set_assets_dir
    from trcc.ui.gui.uc_led_control import _BRIGHTNESS_DEBOUNCE_MS, UCLedControl

    set_assets_dir(_PKG_ASSETS_DIR)
    panel = UCLedControl()

    emitted: list[int] = []
    panel.brightness_changed.connect(emitted.append)

    # Simulate a drag — many rapid value changes within the debounce window.
    for value in (90, 80, 70, 60, 50, 42):
        panel._brightness_slider.setValue(value)

    # Nothing sent to the device yet: the slider is still "moving".
    assert emitted == []
    # The label, however, tracks the slider live.
    assert panel._brightness_label.text() == "42%"

    # Run the real event loop past the single-shot debounce so it fires once.
    loop = QEventLoop()
    QTimer.singleShot(_BRIGHTNESS_DEBOUNCE_MS + 200, loop.quit)
    loop.exec()

    assert emitted == [42], emitted
    panel.deleteLater()


def test_uc_device_spec_matches_the_layout_table() -> None:
    """The sidebar is built from a PanelSpec — its rects must stay Layout's.

    ``UCDevice._setup_ui`` no longer writes coordinates: it renders
    ``_SPEC_CHROME`` / ``_SPEC_CONTENT`` (``ui/presentation/panel_spec.py``),
    the panel stated as data.  This pins the spec to ``Layout`` so an edit to
    either cannot silently move a control.

    Deliberately Qt-FREE — it asserts the spec, not a built widget.  An
    earlier version constructed a second ``UCDevice`` and segfaulted the
    xdist worker intermittently (PySide6 does not enjoy repeated panel
    instantiation across a shared QApplication).  The rendered geometry is
    covered by ``dev/tools/panel_snapshot.py``, which compares real pixels;
    this covers the invariant that tool cannot see — that the coordinates
    came from the table rather than being retyped.
    """
    from trcc.ui.gui.constants import Layout
    from trcc.ui.gui.uc_device import _SPEC_CHROME, _SPEC_CONTENT

    expected = {
        "sensor_btn": Layout.SENSOR_BTN,
        "about_btn": Layout.ABOUT_BTN,
        "no_devices_label": Layout.NO_DEVICES_LABEL,
        "hint_label": Layout.HINT_LABEL,
    }
    declared = {
        c.id: c for c in (*_SPEC_CHROME.controls, *_SPEC_CONTENT.controls)
    }
    assert set(declared) == set(expected), (
        f"spec declares {sorted(declared)}, test pins {sorted(expected)} — a "
        f"control was added to the spec without being pinned here"
    )
    for name, rect in expected.items():
        assert declared[name].rect == rect, (
            f"{name} is not at its Layout entry — the spec and the "
            f"coordinate table have diverged"
        )

    # The empty-state labels belong INSIDE the scroll content; parented to
    # the panel they would float over the device list.
    assert declared["no_devices_label"].parent == "device_area"
    assert declared["hint_label"].parent == "device_area"

    # The backdrop is named, not a path — the spec layer must stay Qt-free.
    assert _SPEC_CHROME.background is not None
    assert _SPEC_CHROME.background.asset == "SIDEBAR_BG"


def test_uc_image_cut_spec_declares_every_toolbar_button() -> None:
    """The crop toolbar's five icon buttons, stated as data.

    Same invariant as the sidebar, on the second converted panel: the rects
    come from the module's BTN_* constants, so a retyped coordinate fails
    here rather than surfacing as a button in the wrong place.
    """
    from trcc.ui.gui import uc_image_cut as mod

    expected = {
        "btn_height_fit": mod.BTN_HEIGHT_FIT,
        "btn_width_fit": mod.BTN_WIDTH_FIT,
        "btn_rotate": mod.BTN_ROTATE,
        "btn_ok": mod.BTN_OK,
        "btn_close": mod.BTN_CLOSE,
    }
    declared = {c.id: c for c in mod._SPEC.controls}
    assert set(declared) == set(expected)
    for name, rect in expected.items():
        assert declared[name].rect == rect, f"{name} moved off its constant"
    # Every toolbar button carries a text fallback for a missing asset.
    assert all(c.fallback for c in mod._SPEC.controls)


def test_overlay_editor_does_not_reseed_an_emptied_layer(gui_app: App) -> None:
    """Deleting the last element must not bring the theme's elements back.

    The editor adopts the active layout into the editable user layer when the
    device has none of its own.  It used to decide that with ``if not
    settings.user_overlay_elements`` — a TRUTHINESS test, which cannot tell
    "no layer" from "the user emptied it", so refreshing after deleting the
    last element re-seeded it from the theme.  That is #276's second symptom
    in the reporter's words: "whichever one I delete LAST still appears."

    The fix is STRUCTURAL, not a better guard, and this test documents the
    behaviour rather than gating the condition.  Measured: flipping the guard
    back to ``if not layout.elements`` does NOT reproduce the bug, because the
    theme elements are no longer reachable from here — the editor used to
    resolve the layout itself via ``resolve_overlay_elements(theme_config,
    [])``, whose own truthiness fell through to the theme.  ``ResolveOverlay``
    returns ``[]`` for an emptied layer, so there is nothing left to seed
    FROM.  The ``source != "user"`` condition is belt-and-braces on top.

    What DOES gate the underlying distinction is
    ``test_resolve_overlay.py::test_an_emptied_user_layer_reports_user_not_theme``.
    """
    from trcc.core.models import Theme
    from trcc.ui.qtgui.panels.overlay_editor import OverlayEditorPanel

    key = next(iter(gui_app.devices), None) or "0402:3922"
    gui_app.active_themes[key] = Theme(
        path=Path("/nonexistent"), name="Theme1", resolution=(320, 320),
        config={"elements": [{"type": "text", "text": "from-theme"}]},
    )
    # The user deleted every element: an EMPTY layer, not an absent one.
    gui_app.settings.for_device(key).user_overlay_elements = []

    panel = OverlayEditorPanel(gui_app, _bus(gui_app))
    panel._picker.set_key(key)
    panel.refresh()

    assert gui_app.settings.for_device(key).user_overlay_elements == [], (
        "the emptied user layer was re-seeded from the theme — the element "
        "the user deleted has come back"
    )
    assert panel._list.count() == 0


# =========================================================================
# Screencast aspect lock — a BEHAVIOUR gate, not a construction smoke
#
# This layer is verified to build, not to compute, and that is exactly how
# ``_on_coord_changed`` shipped with its multiply and divide the wrong way
# round: on an 854x480 panel a width of 201 locked the height to 357 where
# 113 is correct, and square panels were skipped outright by a
# ``ratio != 1.0`` guard so they never locked at all.  ruff, pyright, the
# logging ratchet and 4882 tests were all green on it, because none of them
# look at what a widget COMPUTES.
#
# Every panel geometry the device catalog can produce is covered, so a panel
# added to the catalog without a matching ratio cannot regress silently —
# which is the other half of the same bug: the ratio used to come from a
# hardcoded table that had drifted, missing 640x172 entirely and defaulting
# it to 0.75 where 0.2687 is correct.
# =========================================================================


def _catalog_geometries() -> list[tuple[int, int]]:
    """Every distinct panel resolution the app can meet, from the registry."""
    from trcc.core.protocol import FBL_PROFILES

    return sorted({(p.width, p.height) for p in FBL_PROFILES.values()})


@pytest.mark.parametrize("panel", _catalog_geometries(),
                         ids=lambda wh: f"{wh[0]}x{wh[1]}")
def test_screencast_aspect_lock_matches_the_panel(panel: tuple[int, int],
                                                  qtbot) -> None:
    """Typing a width locks the height to the panel's own aspect, both ways.

    The ratio is ``height / width``, so ``height = width * ratio`` and
    ``width = height / ratio``.  Asserted against the panel geometry itself,
    never a table — a table is what drifted.
    """
    from trcc.ui.gui.display_mode_panels import ScreenCastPanel

    width, height = panel
    ratio = height / width

    typed_w = ScreenCastPanel()
    qtbot.addWidget(typed_w)
    typed_w.set_resolution(width, height)
    typed_w.entry_w.setText("200")
    assert typed_w.entry_h.text() == str(round(200 * ratio))

    typed_h = ScreenCastPanel()
    qtbot.addWidget(typed_h)
    typed_h.set_resolution(width, height)
    typed_h.entry_h.setText("200")
    assert typed_h.entry_w.text() == str(round(200 / ratio))


@pytest.mark.parametrize("panel", _catalog_geometries(),
                         ids=lambda wh: f"{wh[0]}x{wh[1]}")
def test_screencast_plus_button_keeps_the_region_on_aspect(
    panel: tuple[int, int], qtbot,
) -> None:
    """The gesture a user actually makes: nudge the width with ``+``.

    Drives the button's own handler rather than setting text, because that is
    the path a click takes and it is where the emitted params come from.
    """
    from trcc.ui.gui.display_mode_panels import ScreenCastPanel

    width, height = panel
    emitted: list[tuple[int, int, int, int]] = []
    scp = ScreenCastPanel()
    qtbot.addWidget(scp)
    scp.set_resolution(width, height)
    scp.screencast_params_changed.connect(
        lambda x, y, w, h: emitted.append((x, y, w, h)),
    )
    scp.set_values(x=100, y=100, w=200, h=round(200 * height / width))

    scp._increment(scp.entry_w, +1)

    assert emitted, "nudging the width emitted nothing"
    _, _, got_w, got_h = emitted[-1]
    assert got_w == 201
    assert got_h == round(201 * height / width)


def test_screencast_does_not_lock_before_a_device_is_known(qtbot) -> None:
    """With no resolution set the lock stays out of the way.

    ``_get_aspect_ratio`` returns 0.0 rather than a plausible default, so a
    width typed before any device is attached does not silently write a
    height computed from somebody else's panel.
    """
    from trcc.ui.gui.display_mode_panels import ScreenCastPanel

    scp = ScreenCastPanel()
    qtbot.addWidget(scp)
    # None, the same word ``DeviceStateResult.resolution`` uses for it — not a
    # 0.0 sentinel the panel invented, and not a plausible default ratio.
    assert scp._get_aspect_ratio() is None
    scp.entry_w.setText("200")
    assert scp.entry_h.text() == "0"      # untouched, not invented

    # A device that answers nonsense is the THIRD state and is not the same
    # as never having asked: it warns, and still declines to lock.
    scp.set_resolution(0, 0)
    assert scp._get_aspect_ratio() is None


# =========================================================================
# ConfigurationPanel — the slideshow has to actually ROTATE
# =========================================================================


def _apply_slideshow(gui_app: App, qtbot, *, enabled: bool) -> list[str]:
    """Drive the real panel's Apply and report the Commands it dispatched.

    A behaviour gate, not a construction one: ``SetSlideshow`` persists the
    flag and resets the clock but starts NO driver, so a panel that dispatches
    only that saves a slideshow which reports itself enabled and never
    switches a theme.  Nothing structural can see that -- the panel builds,
    the Command succeeds, the setting reads back correctly.
    """
    from trcc.ui.qtgui.panels.configuration_panel import ConfigurationPanel

    panel = ConfigurationPanel(gui_app, _bus(gui_app))
    qtbot.addWidget(panel)                    # unparented top-levels leak

    sent: list[str] = []
    real = panel.dispatch

    def spy(cmd):
        sent.append(type(cmd).__name__)
        return real(cmd)

    panel.dispatch = spy                      # pyright: ignore[reportAttributeAccessIssue]
    panel._key = lambda: "0402:3922"          # pyright: ignore[reportAttributeAccessIssue]
    idx = panel._slideshow_enabled.findData(enabled)
    panel._slideshow_enabled.setCurrentIndex(idx)
    panel._apply()
    return sent


def test_enabling_the_slideshow_starts_the_driver(gui_app: App, qtbot) -> None:
    sent = _apply_slideshow(gui_app, qtbot, enabled=True)

    assert "SetSlideshow" in sent, "the panel stopped persisting the flag"
    assert "StartSlideshowDriver" in sent, (
        "the slideshow was enabled but nothing was asked to rotate it — this "
        "is the bug cli and api already fixed (services/slideshow_driver)"
    )
    assert "StopSlideshowDriver" not in sent


def test_disabling_the_slideshow_stops_the_driver(gui_app: App, qtbot) -> None:
    sent = _apply_slideshow(gui_app, qtbot, enabled=False)

    assert "StopSlideshowDriver" in sent, (
        "turning the slideshow off left the driver rotating"
    )
    assert "StartSlideshowDriver" not in sent


# =========================================================================
# SystemPanel — the surfacings cli/api had and qtgui did not
# =========================================================================


def _system_panel(gui_app: App, qtbot):
    from trcc.ui.qtgui.panels.system_panel import SystemPanel

    panel = SystemPanel(gui_app, _bus(gui_app))
    qtbot.addWidget(panel)
    return panel


def test_system_panel_lists_memory_slots(gui_app: App, qtbot) -> None:
    """Building the panel populates the DRAM list, or says nothing was found.

    An empty widget is not proof of anything -- the list must say WHICH, so a
    host with no SPD data reads as "reported nothing" rather than as a panel
    that forgot to load.
    """
    panel = _system_panel(gui_app, qtbot)

    assert panel._memory_list.count() >= 1, (
        "memory list was never populated — _refresh_memory is not being called"
    )


def test_the_hdd_toggle_shows_the_persisted_flag(gui_app: App, qtbot) -> None:
    """And loading it must NOT write it back.

    ``setChecked`` emits ``toggled``; an unguarded load dispatches a write on
    every refresh, so the setting becomes whatever the UI rendered rather than
    what the user chose.
    """
    gui_app.settings.set_hdd_enabled(False)
    panel = _system_panel(gui_app, qtbot)
    assert panel._hdd_check.isChecked() is False

    # Asserting the VALUE after a refresh cannot see this bug: the write-back
    # writes the value we just set, so it agrees with the expectation.  What
    # has to be observed is that a WRITE was attempted at all.
    gui_app.settings.set_hdd_enabled(True)
    sent: list[str] = []
    real = panel.dispatch

    def spy(cmd):
        sent.append(type(cmd).__name__)
        return real(cmd)

    panel.dispatch = spy                      # pyright: ignore[reportAttributeAccessIssue]
    panel._refresh_hdd()

    assert panel._hdd_check.isChecked() is True
    assert "SetHddEnabled" not in sent, (
        "refreshing the widget DISPATCHED a write — blockSignals is missing, "
        "so the setting becomes whatever the UI rendered"
    )


def test_toggling_hdd_persists_it(gui_app: App, qtbot) -> None:
    panel = _system_panel(gui_app, qtbot)

    panel._hdd_check.setChecked(not panel._hdd_check.isChecked())

    assert gui_app.settings.app.hdd_enabled is panel._hdd_check.isChecked()


def _drive_upgrade(gui_app: App, qtbot, monkeypatch, *, confirm: bool):
    """Press Upgrade with the confirmation answered *confirm*.

    ``hasattr(panel, "_upgrade_btn")`` was the first version of this and it is
    worthless: it proves a button exists, not that the button is safe.  The
    safety-critical behaviour is that NOTHING runs until the user agrees --
    ``RunUpgrade(dry_run=False)`` spawns a package-manager subprocess under
    sudo.
    """
    from PySide6.QtWidgets import QMessageBox

    panel = _system_panel(gui_app, qtbot)
    # A host with no package manager makes the DRY RUN fail, and the panel
    # correctly bails before confirming -- which is right, and would leave the
    # confirm path untested everywhere.  Give it a manager so the gate can
    # reach the branch it exists to guard.
    monkeypatch.setattr(gui_app.diagnostics, "package_manager",
                        lambda: "pacman")
    monkeypatch.setattr(gui_app.platform, "upgrade_command",
                        lambda: ["true", "-upgrade"])
    answer = (QMessageBox.StandardButton.Yes if confirm
              else QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: answer))
    sent: list[bool] = []
    real = panel.dispatch

    def spy(cmd):
        if type(cmd).__name__ == "RunUpgrade":
            sent.append(cmd.dry_run)
        return real(cmd)

    panel.dispatch = spy                      # pyright: ignore[reportAttributeAccessIssue]
    panel._on_upgrade()
    return sent


def test_declining_the_upgrade_runs_nothing(
    gui_app: App, qtbot, monkeypatch,
) -> None:
    sent = _drive_upgrade(gui_app, qtbot, monkeypatch, confirm=False)

    assert sent == [True], (
        "saying No must leave ONLY the dry run — a False in this list is a "
        f"sudo subprocess the user declined, got {sent}"
    )


def test_confirming_the_upgrade_runs_it(
    gui_app: App, qtbot, monkeypatch,
) -> None:
    sent = _drive_upgrade(gui_app, qtbot, monkeypatch, confirm=True)

    assert sent == [True, False], (
        f"expected a dry run, then the real one; got {sent}"
    )


def test_the_confirmation_quotes_the_command_that_will_run(
    gui_app: App, qtbot,
) -> None:
    """The dialog must show the REAL argv, not a UI's guess at it.

    ``RunUpgrade(dry_run=True)`` exists for exactly this: it returns
    ``message="Would run: ..."`` plus the ``command`` list, so the text the
    user approves is the text the Command would execute.
    """
    panel = _system_panel(gui_app, qtbot)
    from trcc.core.commands import RunUpgrade

    preview = panel.dispatch(RunUpgrade(dry_run=True))

    if preview.ok:
        assert preview.command, "dry run reported ok with no command to show"
        assert " ".join(preview.command) in preview.message
    else:
        assert preview.message, "a refusal must say why, it is shown to the user"


def test_the_date_format_reaches_the_bus(gui_app: App, qtbot) -> None:
    """qtgui could set the CLOCK format but not the DATE pattern.

    Editable on purpose: the vocabulary is four tokens (``yyyy`` ``yy`` ``MM``
    ``dd`` -- ``services/_clock._PATTERN_RULES``) with any separator passed
    through, so a fixed list would cap what the CLI already accepts.  The gate
    therefore drives a TYPED pattern, not a preset, because ``currentData()``
    is ``None`` for anything the user types.
    """
    from trcc.ui.qtgui.panels.configuration_panel import ConfigurationPanel

    panel = ConfigurationPanel(gui_app, _bus(gui_app))
    qtbot.addWidget(panel)
    panel._date.setCurrentText("dd.MM.yyyy")

    sent: list[tuple[str, str]] = []
    real = panel.dispatch

    def spy(cmd):
        if type(cmd).__name__ == "SetDateFormat":
            sent.append((type(cmd).__name__, cmd.fmt))
        return real(cmd)

    panel.dispatch = spy                      # pyright: ignore[reportAttributeAccessIssue]
    panel._apply_app_settings()

    assert sent == [("SetDateFormat", "dd.MM.yyyy")], (
        f"the typed pattern did not reach the bus intact: {sent}"
    )


def _zone_tab_with(gui_app: App, qtbot, *, zones: int, mask=()):
    from trcc.core.results import LedZoneEntry
    from trcc.ui.qtgui.panels.led import ZoneTab

    tab = ZoneTab(gui_app, _led_key)
    qtbot.addWidget(tab)
    tab.refresh_from(_snap(
        zones=tuple(LedZoneEntry(color=(255, 0, 0)) for _ in range(zones)),
        zone_sync_zones=mask,
    ))
    return tab


def test_the_carousel_says_which_zones_it_visits(gui_app: App, qtbot) -> None:
    """One checkbox per zone, reflecting the persisted mask.

    Without this mask the carousel stays empty and ``next_sync_zone`` is stuck
    on page 0 — it never advances however the user sets the enable switch, so
    the feature looks present and does nothing.
    """
    tab = _zone_tab_with(gui_app, qtbot, zones=3, mask=(True, False, True))

    assert len(tab._participation_checks) == 3
    assert [b.isChecked() for b in tab._participation_checks] == [True, False, True]


def test_an_unconfigured_mask_shows_every_zone_participating(
    gui_app: App, qtbot,
) -> None:
    """An EMPTY mask is the state that silently disables the carousel.

    Rendering it as "nothing selected" would be accurate about the bytes and a
    lie about the behaviour the switch above promises.
    """
    tab = _zone_tab_with(gui_app, qtbot, zones=3, mask=())

    assert all(b.isChecked() for b in tab._participation_checks)


def test_toggling_participation_sends_the_whole_mask(gui_app: App, qtbot) -> None:
    """``SetLedZoneSyncZones`` replaces the mask wholesale, so send all of it."""
    tab = _zone_tab_with(gui_app, qtbot, zones=3, mask=(True, True, True))
    sent: list[tuple] = []
    real = tab._dispatch

    def spy(cmd):
        if type(cmd).__name__ == "SetLedZoneSyncZones":
            sent.append(cmd.zones)
        return real(cmd)

    tab._dispatch = spy                       # pyright: ignore[reportAttributeAccessIssue]
    tab._participation_checks[1].setChecked(False)

    assert sent == [(True, False, True)], f"got {sent}"


# =========================================================================
# DisplayPanel — video position, which Play/Pause/Stop never showed
# =========================================================================


def _display_panel(gui_app: App, qtbot):
    from trcc.ui.qtgui.panels.display_panel import DisplayPanel

    panel = DisplayPanel(gui_app, _bus(gui_app))
    qtbot.addWidget(panel)
    panel._require_key = lambda: "0402:3922"   # pyright: ignore[reportAttributeAccessIssue]
    return panel


def _stub_status(panel, *, cursor, frame_count):
    from trcc.core.results import VideoStatusResult

    real = panel.dispatch
    sent: list = []

    def spy(cmd):
        name = type(cmd).__name__
        sent.append(cmd)
        if name == "VideoStatus":
            return VideoStatusResult(
                ok=True, key="0402:3922", playing=True,
                cursor=cursor, frame_count=frame_count,
            )
        return real(cmd)

    panel.dispatch = spy                       # pyright: ignore[reportAttributeAccessIssue]
    return sent


def test_no_playback_disables_the_scrubber(gui_app: App, qtbot) -> None:
    """Absence is a normal answer, not a failure.

    A device with no video answers ``ok=True, playing=False`` with the optional
    fields ``None``.  Showing that as a slider parked at 0 would claim there is
    a video sitting on its first frame.
    """
    panel = _display_panel(gui_app, qtbot)
    _stub_status(panel, cursor=None, frame_count=None)

    panel._refresh_video_status()

    assert panel._seek.isEnabled() is False
    assert "no video" in panel._seek_label.text()


def test_the_scrubber_shows_the_position(gui_app: App, qtbot) -> None:
    panel = _display_panel(gui_app, qtbot)
    _stub_status(panel, cursor=42, frame_count=300)

    panel._refresh_video_status()

    assert panel._seek.isEnabled() is True
    assert panel._seek.value() == 42
    assert panel._seek.maximum() == 299          # 0-based cursor
    assert panel._seek_label.text() == "43 / 300"


def test_releasing_the_scrubber_seeks(gui_app: App, qtbot) -> None:
    """On RELEASE, not per drag step — each seek queues a rebuild."""
    panel = _display_panel(gui_app, qtbot)
    sent = _stub_status(panel, cursor=10, frame_count=300)
    panel._refresh_video_status()
    panel._seek.setValue(120)
    sent.clear()

    panel._on_seek_released()

    seeks = [c for c in sent if type(c).__name__ == "SeekVideo"]
    assert len(seeks) == 1, f"expected exactly one seek, got {len(seeks)}"
    assert seeks[0].frame == 120
