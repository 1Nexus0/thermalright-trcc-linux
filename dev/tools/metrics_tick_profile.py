"""Where does ONE metrics tick's time go?  Measured inside the real GUI.

The refresh interval is the biggest single user-facing CPU lever on this app:
measured on a 320x320 SCSI panel playing a 15 fps video with the window in the
tray, the whole process costs

    1 s interval   13.28%   of one core
    2 s interval   10.90%
    10 s interval   8.03%

which is a straight line -- about **5.8% of a core per Hz**, i.e. ~58 ms of CPU
for every tick.  The sysfs sweep is only **16.6 ms** of that (timed directly,
steady state), so roughly two thirds of a tick is spent somewhere nobody has
ever looked.  Guessing produced two wrong answers already: the GUI fan-out is
ALREADY visibility-gated (``trcc_app.py`` checks ``isVisible()`` per panel), and
per-frame logging was fixed without moving this number.

So: time every stage, in the shipping process, at the real cadence.

    PYTHONPATH=src python3.12 dev/tools/metrics_tick_profile.py           # tray
    PYTHONPATH=src python3.12 dev/tools/metrics_tick_profile.py --shown

Needs a real device.  Prints a breakdown every REPORT_S to stdout and keeps
running; Ctrl-C or SIGTERM to stop.  Timings are wall time on the calling
thread -- the sweep runs on the poll thread and the publish on the loop thread,
so their totals are NOT additive with each other, only within a stage.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

REPORT_S = 20.0

_lock = threading.Lock()
_cpu: dict[str, float] = defaultdict(float)
_wall: dict[str, float] = defaultdict(float)
_count: dict[str, int] = defaultdict(int)


def _thread_cpu() -> float:
    """CPU time burned by THIS thread.

    ``perf_counter`` was the first cut and it is the wrong clock here: the
    sweep runs on the poll thread while the render loop composites at 15 fps,
    so wall time counts every moment the thread sat descheduled.  Measured the
    same sweep two ways -- 47-53 ms wall inside the GUI against 16.4 ms of
    actual CPU -- and the wall figure got quoted beside CPU%-derived numbers,
    which made the sweep look like the biggest block when it is not.
    """
    return time.clock_gettime(time.CLOCK_THREAD_CPUTIME_ID)


def _record(stage: str, cpu: float, wall: float) -> None:
    with _lock:
        _cpu[stage] += cpu
        _wall[stage] += wall
        _count[stage] += 1


def _timed(stage: str, fn):
    """Wrap *fn* so every call adds its CPU (and wall) time to *stage*."""
    def wrapper(*a, **k):
        c0, w0 = _thread_cpu(), time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            _record(stage, _thread_cpu() - c0, time.perf_counter() - w0)
    return wrapper


def _handler_name(handler) -> str:
    """A stable, readable name for a bus subscriber."""
    owner = getattr(handler, "__self__", None)
    if owner is not None:
        return f"{type(owner).__name__}.{getattr(handler, '__name__', '?')}"
    return getattr(handler, "__qualname__", repr(handler))[:58]


def install_probes() -> None:
    """Patch every stage of the tick.  Import-time order matters: patch the
    CLASS, not an instance, so the objects the composition root builds later
    are already wrapped."""
    from trcc.adapters.sensors import aggregator as agg
    from trcc.core import events as ev
    from trcc.core import ports
    from trcc.services import metrics_personalize as mp

    agg.BaselineSensors._poll_once = _timed(          # poll thread
        "sweep: BaselineSensors._poll_once", agg.BaselineSensors._poll_once)
    ports.SensorEnumerator.read_all = _timed(
        "read_all (cache)", ports.SensorEnumerator.read_all)
    ports.SensorEnumerator.snapshot = _timed(
        "snapshot (build DTO)", ports.SensorEnumerator.snapshot)
    mp.personalize_readings = _timed(
        "personalize_readings", mp.personalize_readings)
    mp.personalize_metrics = _timed(
        "personalize_metrics", mp.personalize_metrics)

    real_publish = ev.EventBus.publish

    def publish(self, event):                          # per-subscriber timing
        name = type(event).__name__
        if name != "SensorsUpdated":
            return real_publish(self, event)
        c0, w0 = _thread_cpu(), time.perf_counter()
        for handler in list(self._handlers[type(event)]):
            hc, hw = _thread_cpu(), time.perf_counter()
            try:
                handler(event)
            except Exception:
                logging.getLogger(__name__).exception("handler failed")
            _record(f"  subscriber: {_handler_name(handler)}",
                    _thread_cpu() - hc, time.perf_counter() - hw)
        _record("publish SensorsUpdated (all subscribers)",
                _thread_cpu() - c0, time.perf_counter() - w0)

    ev.EventBus.publish = publish

    # The bus forwarder emits a QUEUED Qt signal, so everything below runs on
    # the GUI thread AFTER publish() has returned -- outside the per-subscriber
    # timing, and therefore missing from the first version of this tool.
    from trcc.ui.gui import trcc_app as ta
    from trcc.ui.gui import uc_system_info as usi

    ta.TRCCApp._on_bus_sensors_updated = _timed(
        "GUI: TRCCApp._on_bus_sensors_updated",
        ta.TRCCApp._on_bus_sensors_updated)
    ta.TRCCApp._fan_out_metrics = _timed(
        "GUI:   _fan_out_metrics", ta.TRCCApp._fan_out_metrics)
    usi.UCSystemInfo.update_from_metrics = _timed(
        "GUI:     UCSystemInfo.update_from_metrics",
        usi.UCSystemInfo.update_from_metrics)


def report_forever() -> None:
    started = time.monotonic()
    while True:
        time.sleep(REPORT_S)
        elapsed = time.monotonic() - started
        with _lock:
            rows = sorted(_cpu.items(), key=lambda kv: -kv[1])
            counts, wall = dict(_count), dict(_wall)
        print(f"\n=== metrics tick breakdown after {elapsed:.0f}s ===", flush=True)
        print(f"{'stage':<46}{'calls':>6}{'cpu ms/call':>13}"
              f"{'wall ms/call':>14}{'cpu ms/s':>10}", flush=True)
        for stage, total in rows:
            n = max(counts[stage], 1)
            print(f"{stage:<46}{counts[stage]:>6}{1000*total/n:>13.2f}"
                  f"{1000*wall[stage]/n:>14.2f}{1000*total/elapsed:>10.2f}",
                  flush=True)


def main() -> int:
    install_probes()
    from trcc.adapters.infra.logging import configure_logging
    from trcc.adapters.system import current_platform
    from trcc.core.logs import levels_for
    from trcc.ui.gui import launch

    ladder = levels_for(0)                     # what a user runs: no -v
    configure_logging(current_platform().paths().log_file(),
                      level=ladder.file, stderr_level=ladder.terminal,
                      per_frame=ladder.per_frame)
    threading.Thread(target=report_forever, daemon=True).start()
    return launch(start_hidden="--shown" not in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
