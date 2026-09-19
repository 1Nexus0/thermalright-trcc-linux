"""PeriodicUpdater — one restartable QTimer, owned by a widget.

Both Qt skins give their panels a ``start_periodic_updates`` /
``stop_periodic_updates`` pair, and both had written the same restart dance:
create the timer on first use, and on a repeat call stop it, drop the previous
connection, then reconnect at the new cadence.  Getting that wrong leaks a
connection and fires the callback twice per tick.

It is shared by *composition*, not inheritance: ``ui/gui`` and ``ui/qtgui``
have deliberately different ``BasePanel`` designs (an MVC delegate panel vs an
App+bus injected one), so a common base would force a merge neither wants.
A panel owns an updater; its two public methods stay exactly where they were.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


class PeriodicUpdater:
    """A single restartable timer belonging to *owner*.

    Parented to the owning widget, so Qt tears the timer down with the panel
    and a closed panel can't keep ticking.
    """

    __slots__ = ("_owner", "_timer", "_wanted")

    def __init__(self, owner: QWidget) -> None:
        log.debug("__init__: owner=%s", owner)
        self._owner = owner
        self._timer: QTimer | None = None
        # Whether the OWNER wants ticks at all, as opposed to whether the
        # timer happens to be running.  ``suspend``/``resume`` move the
        # second without touching the first, so a panel hidden and shown
        # again resumes while one that deliberately stopped stays stopped.
        self._wanted = False

    def start(self, interval_ms: int, callback: Callable[[], None]) -> None:
        """Call *callback* every *interval_ms* ms on the Qt main thread.

        Safe to re-call with a new cadence: the previous connection is dropped
        first, so the callback fires once per tick, not once per start().
        """
        log.info(
            "%s.start_periodic_updates: interval_ms=%d callback=%s",
            type(self._owner).__name__, interval_ms,
            getattr(callback, "__qualname__", repr(callback)),
        )
        if self._timer is None:
            self._timer = QTimer(self._owner)
        else:
            self._timer.stop()
            try:
                self._timer.timeout.disconnect()
            except RuntimeError:
                # Nothing was connected — a fresh timer, nothing to undo.
                pass
        self._timer.timeout.connect(callback)
        self._wanted = True
        self._timer.start(interval_ms)

    def stop(self) -> None:
        """Stop ticking for good.  A no-op if never started.

        Clears the WANT, so a later ``resume`` will not restart it — that is
        what separates this from :meth:`suspend`.
        """
        log.info("%s.stop_periodic_updates: active=%s",
                 type(self._owner).__name__, self.is_active)
        self._wanted = False
        if self._timer is not None:
            self._timer.stop()

    def suspend(self) -> None:
        """Pause ticking WITHOUT forgetting that the owner wants it.

        For a panel that has left the screen: qtgui shows one panel at a time
        in a ``QStackedWidget``, and measured 2026-09-19 the hidden ones kept
        dispatching ``BuildPreview`` (a real composite) and ``ReadSensors`` at
        exactly the same rate as the visible one.
        """
        log.debug("%s.suspend: active=%s wanted=%s",
                  type(self._owner).__name__, self.is_active, self._wanted)
        if self._timer is not None:
            self._timer.stop()

    def resume(self) -> None:
        """Tick again at the cadence already set, if the owner still wants it.

        ``QTimer.start()`` with no argument reuses the interval, and ``stop``
        does not drop the connection, so nothing needs re-plumbing here.
        """
        log.debug("%s.resume: wanted=%s", type(self._owner).__name__, self._wanted)
        if self._wanted and self._timer is not None:
            self._timer.start()

    @property
    def is_active(self) -> bool:
        """True while the timer is running."""
        log.debug("is_active")
        return self._timer is not None and self._timer.isActive()
