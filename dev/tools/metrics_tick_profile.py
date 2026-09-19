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
_total: dict[str, float] = defaultdict(float)
_count: dict[str, int] = defaultdict(int)


def _record(stage: str, secs: float) -> None:
    with _lock:
        _total[stage] += secs
        _count[stage] += 1


def _timed(stage: str, fn):
    """Wrap *fn* so every call adds its wall time to *stage*."""
    def wrapper(*a, **k):
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            _record(stage, time.perf_counter() - t0)
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
        t0 = time.perf_counter()
        for handler in list(self._handlers[type(event)]):
            h0 = time.perf_counter()
            try:
                handler(event)
            except Exception:
                logging.getLogger(__name__).exception("handler failed")
            _record(f"  subscriber: {_handler_name(handler)}",
                    time.perf_counter() - h0)
        _record("publish SensorsUpdated (all subscribers)",
                time.perf_counter() - t0)

    ev.EventBus.publish = publish


def report_forever() -> None:
    started = time.monotonic()
    while True:
        time.sleep(REPORT_S)
        elapsed = time.monotonic() - started
        with _lock:
            rows = sorted(_total.items(), key=lambda kv: -kv[1])
            counts = dict(_count)
        print(f"\n=== metrics tick breakdown after {elapsed:.0f}s ===", flush=True)
        print(f"{'stage':<52}{'calls':>7}{'ms/call':>10}{'ms/s':>9}", flush=True)
        for stage, total in rows:
            n = counts[stage]
            print(f"{stage:<52}{n:>7}{1000*total/max(n,1):>10.2f}"
                  f"{1000*total/elapsed:>9.2f}", flush=True)


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
