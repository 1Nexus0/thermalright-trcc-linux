"""QueueWorker — one daemon thread draining submitted jobs, oldest first.

**Why this exists.**  ``ThreadDataInstallRunner`` and ``ThreadVideoExportRunner``
were the same class twice: measured **75% identical lines** after renaming the
payload (73 vs 65).  Everything that differed was a value -- the thread's name,
what to do with an item, the wording of one shutdown warning -- and everything
that was machinery was duplicated: spawn-on-first-use under a lock, the
``_STOP`` sentinel that wakes a blocked ``get()``, the join with a timeout, the
refusal to accept work after shutdown.

**Why no instrument caught it.**  ``dev/tools/dup_bodies.py`` matches by
``(base, method name, body)`` among siblings of ONE base, and these two are
siblings of nothing -- they implement different ABCs.  It reports 15 clusters
tree-wide and cannot report this one by construction.  A cross-port duplicate
is a blind spot of the tool, not an absence.

**Composition, not a shared base class.**  The two ports have different
``submit`` signatures (``(resolution, variant, mask_variant)`` vs
``(token, request)``) and already subclass their own ABCs; a common base would
either flatten those signatures or drag both into one MRO for no gain.  Each
runner keeps its typed ``submit`` and packs its own payload.

**What is NOT here: deduplication.**  ``DataInstallRunner`` claims each request
once and forever (``_SubmitOnce``) because re-running ``ensure_all`` re-walks
six directories for nothing.  ``VideoExportRunner`` documents the opposite --
"exporting the same clip twice is a thing a user may legitimately ask for" --
so the policy stays with the runner that has one.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

#: Wakes the worker out of a blocking ``get()`` at shutdown.
_STOP = object()

T = TypeVar("T")


class QueueWorker(Generic[T]):
    """Serialises submitted jobs onto one lazily-spawned daemon thread.

    Serialisation is the contract, not an implementation detail:
    ``VideoExportRunner`` states that two concurrent ffmpeg runs "finish no
    sooner together than in turn and make the UI's progress meaningless".
    ``tests/adapters/infra/test_data_install_runner.py`` asserts the overlap
    rather than the thread count, so any future worker that serialises some
    other way still passes.
    """

    __slots__ = ("_handle", "_join_timeout", "_lock", "_name", "_queue",
                 "_stall_hint", "_stop", "_thread")

    def __init__(
        self,
        name: str,
        handle: Callable[[T], None],
        *,
        stall_hint: str,
        join_timeout: float = 2.0,
    ) -> None:
        """*name* is the OS thread name — a test asserts on it, so it is
        identity, not decoration.  *stall_hint* names what owns the thread
        when a shutdown times out ("mid-download", "mid-encode").
        """
        log.info("QueueWorker(%s).__init__: join_timeout=%.1fs", name,
                 join_timeout)
        self._name = name
        self._handle = handle
        self._stall_hint = stall_hint
        self._join_timeout = join_timeout
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, item: T) -> bool:
        """Queue *item*.  False when the worker is already shut down.

        Teardown races a UI closing mid-job, and dropping the work beats
        raising into whatever submitted it.
        """
        if self._stop.is_set():
            log.warning("QueueWorker(%s).submit: after shutdown — ignored",
                        self._name)
            return False
        self._start()
        self._queue.put(item)
        log.debug("QueueWorker(%s).submit: queued", self._name)
        return True

    def _start(self) -> None:
        """Spawn the worker on FIRST use — most runs never submit anything.

        The early return is what keeps one thread on the queue.  Without it a
        second thread drains the same queue and two jobs run at once, and
        ``shutdown`` joins only the last thread it stored.
        """
        with self._lock:
            if self._thread is not None:
                return
            log.info("QueueWorker(%s)._start: spawning", self._name)
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        log.info("QueueWorker(%s): started", self._name)
        while not self._stop.is_set():
            item = self._queue.get()
            if item is _STOP or self._stop.is_set():
                break
            self._handle(item)
        log.info("QueueWorker(%s): stopped", self._name)

    def shutdown(self) -> None:
        """Stop the worker and drop anything still queued.  Idempotent."""
        log.info("QueueWorker(%s).shutdown", self._name)
        self._stop.set()
        thread = self._thread
        if thread is None:
            log.debug("QueueWorker(%s).shutdown: never started", self._name)
            return
        self._queue.put(_STOP)   # break a blocking get() so the join is prompt
        thread.join(timeout=self._join_timeout)
        if thread.is_alive():
            # The job owns the thread until its own timeout.  It is a daemon,
            # so it cannot hold the process open.
            log.warning(
                "QueueWorker(%s).shutdown: did not stop within %.1fs "
                "(likely %s) — abandoning it as a daemon",
                self._name, self._join_timeout, self._stall_hint,
            )
        self._thread = None
