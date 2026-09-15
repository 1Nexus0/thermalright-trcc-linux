"""BackgroundSlot — what each device is currently showing, behind everything.

*"a background is a background, the media the background uses should not
matter, it still needs to composite the metrics mask if there is one"*

One slot per device holding one surface.  The media is not part of the type:
a theme's ``00.png``, a decoded video frame, a cloud override and (later) a
screen capture are all just *the picture*, and the compositor draws mask and
metrics over whatever is in the slot without asking where it came from.

**The slot holds the source's NATIVE surface plus its origin — it is not
pre-fitted.**  Fitting needs the canvas, which belongs to the consumer (it
moves with orientation), and the rule that chooses HOW to fit needs the
origin, which only the producer knows.  A slot of pre-fitted surfaces would
copy that rule into every producer; putting the origin in the slot instead
keeps one fit rule in one place.  This is the same fault that shipped a black
panel once already, where the origin was read off the active theme's directory
while the background came from somewhere else entirely.

**Stored WITH the token that identifies it, and validated on read.**  That is
the one detail that makes this a slot rather than a trap.  ``SceneCache`` and
``BgMaskCache`` already do exactly this — hold a value beside the key that
produced it, and check on the way out — and a bare per-device value is the one
variant that cannot tell you it is stale.  Without it, rendering theme X while
the slot holds theme Y's background silently paints Y under X's mask and X's
metrics, with no exception, no log line, and a cache entry to make it stick.
"""
from __future__ import annotations

import logging
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

from ..core.logs import per_frame

log = logging.getLogger(__name__)
#: ``push`` and ``current`` run once per RENDERED FRAME.  On the ``trcc.frame``
#: family their ``.debug()`` short-circuits in ``isEnabledFor`` and the record
#: is never built -- per-frame emitters were 92% of all records and ~90% of the
#: CPU regression measured since v9.9.2.  ``-vvv`` turns them on.
frame_log = per_frame(__name__)


@dataclass(frozen=True, slots=True)
class Background:
    """One background surface and the origin that decides how it is fitted.

    ``surface`` is an opaque ``Renderer`` handle, the same contract
    ``RenderContent`` uses; ``None`` means "nothing to paint", which the
    compositor renders as the canvas it started with.

    ``is_user_content`` is the fit axis: a user upload arrives at its native
    resolution and honours ``DeviceSettings.fit_mode``, while program/cloud
    content is authored for the canvas and takes the C# native-or-black width
    test.  It rides WITH the surface because only whoever resolved the surface
    knows which tree it came out of.
    """

    surface: Any
    is_user_content: bool = False


class BackgroundSlot:
    """The current background per device — push it, read it back by token.

    Lives on ``App`` rather than on ``DisplayService`` because
    ``App._wire_display`` builds a NEW ``DisplayService`` every time a renderer
    is attached, discarding all of its per-device state.  ``MediaService``,
    ``Settings`` and ``active_themes`` sit on ``App`` for that same reason: a
    background that vanished when the GUI attached its renderer would be a
    blank panel with no error.
    """

    __slots__ = ("_slots",)

    def __init__(self) -> None:
        log.debug("BackgroundSlot: created")
        self._slots: dict[str, tuple[Hashable, Background]] = {}

    def push(self, key: str, token: Hashable, background: Background) -> None:
        """Make *background* the current picture for *key*, tagged *token*."""
        frame_log.debug("push: key=%s token=%s is_user=%s has_surface=%s",
                        key, token, background.is_user_content,
                        background.surface is not None)
        self._slots[key] = (token, background)

    def current(self, key: str, token: Hashable) -> Background | None:
        """The background for *key* IF it is still the one *token* names.

        ``None`` covers both "nothing pushed" and "what is here is something
        else" — the caller resolves either way, so a stale slot degrades to
        the work it was avoiding rather than to a wrong picture.
        """
        held = self._slots.get(key)
        if held is None:
            frame_log.debug("current: key=%s — nothing pushed", key)
            return None
        held_token, background = held
        if held_token != token:
            frame_log.debug(
                "current: key=%s — held token %s is not %s, so the source "
                "changed; resolving again", key, held_token, token)
            return None
        return background

    def clear(self, key: str) -> None:
        """Drop *key*'s background — disconnect, or a scene invalidation."""
        if self._slots.pop(key, None) is not None:
            log.debug("clear: key=%s dropped", key)
