"""Motherboard / super-I/O temperatures — the sensors nothing claimed.

Every other temperature port is ROLE-typed: we know a ``coretemp`` reading is
the CPU and an ``nvme`` reading is a disk.  A board sensor has no role — it is
whatever the builder wired to that header — so it needs its own port and the
user has to be the one who picks it.

The gap was invisible because the chip was already half-read: ``hwmon.py``
opens an ``nct6xxx`` for its FANS and walked straight past a dozen
``tempN_input`` channels on the same node.  #259 asked for ``T_SENSOR1`` (an
``AUXTIN`` on ASUS boards) and #282 for a Fujitsu ``sch5636`` that our own
scanner never saw at all.
"""
from __future__ import annotations

from typing import Any

import pytest

from trcc.adapters.sensors import psutil_sources


class _Entry:
    """Shaped like ``psutil``'s ``shwtemp`` namedtuple, for the fields we read."""

    def __init__(self, label: str, current: float) -> None:
        self.label = label
        self.current = current


def _fake_chips(monkeypatch: pytest.MonkeyPatch, chips: dict[str, Any]) -> None:
    monkeypatch.setattr(
        psutil_sources.psutil, "sensors_temperatures", lambda: chips,
    )


def test_an_unconnected_header_reading_zero_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 0.0 is the documented signature of a header the board never wired.

    This desk's ``nct6798`` publishes twelve inputs and four of them
    (``PCH_CHIP_TEMP``, ``PCH_CPU_TEMP``, ``PCH_MCH_TEMP``,
    ``PCH_CHIP_CPU_MAX_TEMP``) read 0.0.  lm-sensors users mask those with
    per-board ``ignore`` directives we cannot ship, so without this filter every
    such user is handed four dead sensors to choose between.
    """
    _fake_chips(monkeypatch, {"nct6798": [
        _Entry("SYSTIN", 23.0),
        _Entry("PCH_CHIP_TEMP", 0.0),
        _Entry("AUXTIN0", 28.0),
    ]})
    keys = [s.key for s in psutil_sources.discover_board_temps()]
    assert keys == ["nct6798_systin", "nct6798_auxtin0"]


def test_a_cold_probe_is_not_mistaken_for_an_unconnected_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter is ``== 0.0``, never a plausibility RANGE.

    An external probe in a cold room reads a low number, and #259 is asking for
    exactly that sensor.  A "looks too low" heuristic would discard the feature
    while appearing to work everywhere it was tested.
    """
    _fake_chips(monkeypatch, {"nct6798": [_Entry("AUXTIN1", 4.0)]})
    assert [s.key for s in psutil_sources.discover_board_temps()] == [
        "nct6798_auxtin1"]


@pytest.mark.parametrize(
    "chip", ["coretemp", "k10temp", "amdgpu", "nvme", "spd5118", "jc42"])
def test_a_role_owned_chip_is_not_reported_twice(
    monkeypatch: pytest.MonkeyPatch, chip: str,
) -> None:
    """A DIMM must not appear as ``memory:temp`` AND as a nameless board sensor.

    These chips already have a typed port and a typed discovery; surfacing them
    here would duplicate every one of them under a second identity.
    """
    _fake_chips(monkeypatch, {chip: [_Entry("temp1", 42.0)]})
    assert psutil_sources.discover_board_temps() == []


def test_the_key_is_built_from_the_LABEL_the_user_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``T_SENSOR1`` is found by its label, not by its channel number.

    A bare ``temp7`` tells the user nothing, and two unlabelled chips would
    collide — so the chip name stays in the key as well.
    """
    _fake_chips(monkeypatch, {"nct6798": [
        _Entry("PECI Agent 0 Calibration", 26.0)]})
    source = psutil_sources.discover_board_temps()[0]
    assert source.key == "nct6798_peci_agent_0_calibration"
    assert source.name == "PECI Agent 0 Calibration (nct6798)"


def test_an_unlabelled_input_falls_back_to_its_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acpitz`` publishes no label; it must still be addressable."""
    _fake_chips(monkeypatch, {"acpitz": [_Entry("", 27.8)]})
    assert [s.key for s in psutil_sources.discover_board_temps()] == [
        "acpitz_temp1"]


def test_psutil_refusing_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sensors is a valid answer — a VM, a container, a locked-down kernel.

    The whole family must degrade to "none" rather than take the snapshot down
    with it.
    """
    def _boom() -> dict[str, Any]:
        raise OSError("no hwmon here")

    monkeypatch.setattr(
        psutil_sources.psutil, "sensors_temperatures", _boom)
    assert psutil_sources.discover_board_temps() == []
