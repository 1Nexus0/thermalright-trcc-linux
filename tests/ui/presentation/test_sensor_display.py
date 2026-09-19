"""sensor_display — pure-Python tests (NO Qt, NO QApplication).

Locks the value-format ladder (shared by the picker + the system-info panel),
the discover()→grouped-SensorInfo adaptation, and the catalog×broadcast merge
that lets a view ride the bus instead of polling ``ReadSensors`` on a timer.
"""
from __future__ import annotations

from trcc.core.models import SensorReading
from trcc.ui.presentation.sensor_display import (
    apply_live_values,
    format_sensor_value,
    group_sensors,
)


def _reading(sensor_id: str, *, unit: str = "°C", label: str = "",
             category: str = "x", value: float = 0.0) -> SensorReading:
    return SensorReading(sensor_id=sensor_id, category=category,
                         value=value, unit=unit, label=label)


# ── format_sensor_value ──────────────────────────────────────────────────


def test_format_celsius_swaps_symbol_on_temp_unit() -> None:
    assert format_sensor_value(60.4, "°C", temp_unit=0) == "60°C"
    assert format_sensor_value(60.4, "°C", temp_unit=1) == "60°F"
    assert format_sensor_value(60.4, "°C") == "60°C"          # default = celsius


def test_format_fahrenheit_reading_keeps_its_degree_sign() -> None:
    """A personalised reading declares °F itself — the ladder must know it.

    ``ReadSensors`` rewrites ``unit`` to "°F" when the user picks Fahrenheit.
    Without this branch that fell past every case to the unit-less default and
    a 122°F CPU rendered as "122.0" in the sensor picker.
    """
    assert format_sensor_value(122.0, "°F") == "122°F"
    assert format_sensor_value(122.0, "°F", temp_unit=0) == "122°F"
    assert format_sensor_value(122.0, "°F", temp_unit=1) == "122°F"


def test_format_integer_unit_ladder() -> None:
    assert format_sensor_value(37.6, "%") == "38%"
    assert format_sensor_value(1200.0, "RPM") == "1200RPM"
    assert format_sensor_value(65.2, "W") == "65W"
    assert format_sensor_value(4200.0, "MHz") == "4200MHz"


def test_format_volts_two_decimals() -> None:
    assert format_sensor_value(1.2, "V") == "1.20V"


def test_format_rate_units_one_decimal() -> None:
    assert format_sensor_value(12.34, "MB") == "12.3MB"
    assert format_sensor_value(5.0, "MB/s") == "5.0MB/s"
    assert format_sensor_value(3.21, "KB/s") == "3.2KB/s"


def test_format_unknown_unit_falls_to_one_decimal() -> None:
    assert format_sensor_value(7.25, "??") == "7.2"


# ── group_sensors ────────────────────────────────────────────────────────


def test_group_adapts_reading_to_sensorinfo() -> None:
    [(header, sensors)] = group_sensors([
        _reading("cpu:temp", label="CPU Temp", unit="°C", category="temp"),
    ])
    assert header == "CPU"
    s = sensors[0]
    assert (s.id, s.name, s.source, s.unit, s.category) == (
        "cpu:temp", "CPU Temp", "cpu", "°C", "temp")


def test_group_source_inference_and_name_fallback() -> None:
    [(header, sensors)] = group_sensors([_reading("hwmon:coretemp:t1")])
    assert header == "HWMON"                 # unknown source → upper
    assert sensors[0].source == "hwmon"
    assert sensors[0].name == "hwmon:coretemp:t1"   # empty label → id

    [(header2, _)] = group_sensors([_reading("bare")])
    assert header2 == "SYSTEM"               # no prefix → "system"


def test_group_orders_known_sources_first() -> None:
    groups = group_sensors([
        _reading("fan:1", label="F"),
        _reading("gpu:temp", label="G"),
        _reading("cpu:temp", label="C"),
    ])
    assert [h for h, _ in groups] == ["CPU", "GPU", "Fans"]


def test_group_drops_clock_sources() -> None:
    groups = group_sensors([
        _reading("time:now", label="Time"),
        _reading("date:today", label="Date"),
        _reading("cpu:temp", label="C"),
    ])
    assert [h for h, _ in groups] == ["CPU"]


# ── apply_live_values ────────────────────────────────────────────────────


def test_live_values_replace_catalog_values() -> None:
    """Identity is kept from the catalog; only the number moves."""
    catalog = [_reading("cpu:temp", label="CPU", category="temperature"),
               _reading("cpu:usage", unit="%", label="CPU", category="load")]

    out = apply_live_values(catalog, {"cpu:temp": 61.0, "cpu:usage": 12.5})

    assert [(r.sensor_id, r.value, r.unit, r.category, r.label) for r in out] == [
        ("cpu:temp", 61.0, "°C", "temperature", "CPU"),
        ("cpu:usage", 12.5, "%", "load", "CPU"),
    ]


def test_a_sensor_absent_from_the_broadcast_is_dropped_not_zeroed() -> None:
    """The user disabling HDD drops ``disk:*`` from the broadcast entirely.

    A view that zeroed them instead would show a 0°C SSD forever — which is a
    READING, and wrong.  Same semantics ``ReadSensors`` applies to the same
    dict, so the bus path and the one-shot path agree about what exists.

    MUTATION CHECK: drop the ``if reading.sensor_id in values`` filter and this
    fails with a KeyError, naming the absent sensor.
    """
    catalog = [_reading("cpu:temp"), _reading("disk:temp", label="SSD")]

    out = apply_live_values(catalog, {"cpu:temp": 40.0})

    assert [r.sensor_id for r in out] == ["cpu:temp"]


def test_a_catalog_read_under_celsius_renders_under_fahrenheit() -> None:
    """And back, because the user can flip the toggle twice.

    The catalog is cached per view and refreshed on view-switch; the broadcast
    self-describes its unit.  Without this the panel keeps labelling a 122°F
    reading "°C" until the user navigates away and back.

    MUTATION CHECK: pass ``unit=reading.unit`` straight through and the first
    assertion fails, naming the reading that lied about its unit.
    """
    catalog = [_reading("cpu:temp", unit="°C")]

    hot = apply_live_values(catalog, {"cpu:temp": 122.0}, temp_unit="F")
    assert (hot[0].value, hot[0].unit) == (122.0, "°F")

    back = apply_live_values(hot, {"cpu:temp": 50.0}, temp_unit="C")
    assert (back[0].value, back[0].unit) == (50.0, "°C")


def test_live_values_ignore_a_sensor_the_catalog_never_had() -> None:
    """A broadcast key with no catalog row is not invented.

    The catalog is the identity source: a row needs a label, a category and a
    unit, and the broadcast carries none of them.  A sensor that appears at
    runtime arrives on the next explicit refresh, not as an unlabelled row.
    """
    out = apply_live_values([_reading("cpu:temp")],
                           {"cpu:temp": 40.0, "gpu:temp": 70.0})

    assert [r.sensor_id for r in out] == ["cpu:temp"]
