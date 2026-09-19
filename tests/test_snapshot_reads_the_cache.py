"""``snapshot()`` must build its DTO from the readings it already has.

It used to call ``read_all()`` and then re-read every source from hardware
anyway -- ``cpu.temp()``, ``cpu.usage()``, ``cpu.power()``, each GPU's four
quantities, every fan's rpm, ``memory.percent()``.  So one metrics tick read
the sensors TWICE.

Two costs, both measured on a real box (320x320 SCSI panel, 2 s interval):

* **25.01 ms per tick, 43% of the whole tick.**  The sweep itself is 47 ms;
  the bus fan-out, by contrast, is 0.87 ms.
* **The second read is WRONG, and it is the one the GUI shows.**  ``cpu:usage``
  and ``cpu:power`` are deltas -- a percentage since the last call, and RAPL
  energy over elapsed time.  ``_poll_once`` reads them over the full 2 s
  interval; ``snapshot()`` re-reads microseconds later, over a window of
  nothing.  Over 8 steady ticks the LCD overlay (fed from the cache) read
  11.7% +/- 0.53, while the GUI panel (fed from this DTO) read 12.5% +/- 3.34
  -- 6.3x noisier -- and CPU power spiked to 27.8 W against a true 13.0 W.

``RenderLed`` already fixed exactly this bug one layer higher: its docstring
records that "re-polling the sensors every tick resampled instantaneous
readings and made the displayed metric flicker".  It reads
``app.last_raw_snapshot`` now.  The flicker was pushed down here, not removed.
"""
from __future__ import annotations

import pytest

from trcc.adapters.sensors.aggregator import BaselineSensors

from .conftest import FakeCpu, FakeGpu, FakeMemory


class _Reads:
    """Mixin: records every hardware read made through it.

    Three sources need identical treatment, so the recording lives here once
    rather than in twelve near-identical method bodies.
    """

    def __init__(self) -> None:
        self.reads: list[str] = []

    def _seen(self, name: str, value: float | None) -> float | None:
        self.reads.append(name)
        return value


class CountingCpu(_Reads, FakeCpu):
    def __init__(self) -> None:
        _Reads.__init__(self)
        FakeCpu.__init__(self)

    def temp(self):
        return self._seen("temp", super().temp())

    def usage(self):
        return self._seen("usage", super().usage())

    def freq(self):
        return self._seen("freq", super().freq())

    def power(self):
        return self._seen("power", super().power())


class CountingMemory(_Reads, FakeMemory):
    def used(self):
        return self._seen("used", super().used())

    def available(self):
        return self._seen("available", super().available())

    def total(self):
        return self._seen("total", super().total())

    def percent(self):
        return self._seen("percent", super().percent())


class CountingGpu(_Reads, FakeGpu):
    def __init__(self, index: int = 0) -> None:
        _Reads.__init__(self)
        FakeGpu.__init__(self, index, discrete=True, vendor="nvidia")

    def temp(self):
        return self._seen("temp", super().temp())

    def usage(self):
        return self._seen("usage", super().usage())

    def clock(self):
        return self._seen("clock", super().clock())

    def power(self):
        return self._seen("power", super().power())

    def fan(self):
        return self._seen("fan", super().fan())


@pytest.fixture
def spied() -> tuple[BaselineSensors, CountingCpu, CountingMemory, CountingGpu]:
    cpu, mem, gpu = CountingCpu(), CountingMemory(), CountingGpu()
    return BaselineSensors(cpu=cpu, memory=mem, gpus=[gpu], fans=[]), cpu, mem, gpu


def test_snapshot_reads_no_source_the_poll_already_read(spied) -> None:
    """THE GATE: after the poll has filled the cache, ``snapshot()`` must not
    touch hardware again.

    Counting READS, not values: a value assertion passes whether the number
    came from the cache or from a second identical read, which is precisely
    how this survived unnoticed.
    """
    sensors, cpu, mem, gpu = spied
    sensors.read_all()                  # the poll — this one SHOULD read
    for spy in (cpu, mem, gpu):
        spy.reads.clear()

    sensors.snapshot()

    assert (cpu.reads, mem.reads, gpu.reads) == ([], [], []), (
        "snapshot() re-read the hardware after read_all() had already "
        f"filled the cache — cpu={cpu.reads} memory={mem.reads} gpu={gpu.reads}. "
        "Delta-based sources (cpu:usage, cpu:power) measure since the LAST "
        "call, so a second read microseconds later reports a window of "
        "nothing: measured 6.3x noisier, with CPU power reading 27.8 W "
        "against a true 13.0 W."
    )


def test_snapshot_scalars_equal_the_cache(spied) -> None:
    """The DTO and the flat readings are two views of ONE sample.

    They disagreed on real hardware — the overlay rendered one number and the
    sensors panel another, on the same tick, for the same metric.
    """
    sensors, *_ = spied
    readings = sensors.read_all()
    m = sensors.snapshot()

    for field, key in (
        ("cpu_temp", "cpu:temp"), ("cpu_percent", "cpu:usage"),
        ("cpu_freq", "cpu:freq"), ("cpu_power", "cpu:power"),
        ("gpu_temp", "gpu:primary:temp"), ("gpu_usage", "gpu:primary:usage"),
        ("gpu_clock", "gpu:primary:clock"), ("gpu_power", "gpu:primary:power"),
        ("mem_percent", "memory:percent"), ("mem_available", "memory:available"),
    ):
        assert getattr(m, field) == pytest.approx(readings[key]), (
            f"HardwareMetrics.{field} disagrees with readings[{key!r}] — "
            "the DTO and the flat view must be the same sample"
        )
