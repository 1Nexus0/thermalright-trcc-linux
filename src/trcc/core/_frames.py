"""Row-padding primitive shared by every frame producer.

Why centralised: the project had three copies of this one subtle loop —
``adapters/screencast/pipewire.py`` (named ``unpad_rows``, documented and
tested), ``adapters/render/qt.py`` (an inline ``b"".join`` inside
``qimage_to_raw_rgb24``) and ``adapters/screencast/qt.py`` (an inline
``bytearray`` loop inside ``_pixmap_to_raw_frame``).  All three were verified
to compute the same answer, and the second one's own docstring argued it
avoided "two copies of the stride handling … the one genuinely subtle part"
while the third sat one directory away.  Three correct copies of a subtle
algorithm is the shape a silent divergence grows in.

It lives in ``core`` rather than beside any one producer because the two Qt
sites cannot reach the PipeWire adapter for it: that would be an
adapter→adapter dependency, and it would drag GStreamer + dbus imports into
the render path, which has no business knowing either exists.

Pure-stdlib, no project imports.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def unpad_rows(data: bytes, width: int, height: int, stride: int) -> bytes:
    """Drop per-row padding so the result is tightly packed RGB24.

    Both Qt and GStreamer align each scanline, so a row occupies ``stride``
    bytes of which only ``width * 3`` are pixels.  A consumer that assumes
    ``width * 3`` reads the padding as pixels and every row starts a little
    further left than the one above — the picture shears diagonally.  It is
    invisible whenever the padding happens to be zero, which is why it
    survived: it only bites when ``width * 3`` is not a multiple of 4.

    MEASURED against the BUFFER (``GstVideoMeta`` / ``QImage``), not against a
    caps-derived default — the earlier citation named ``GstVideo.VideoInfo``,
    which returns the format's aligned stride and disagreed with the buffer on
    half the backends tested (see ``_row_stride`` in the PipeWire adapter).  On
    GNOME a 1366-wide RGB frame really does stride **4100** against
    ``width * 3 == 4098``, and 854 — a real TRCC panel width — pads 2562 to
    **2564**.  So this is device geometry, not only screencast.

    A tight buffer is returned unchanged, so the common case costs one
    comparison.

    *stride* is a CLAIM about *data*, and the two can disagree.  When the
    buffer is shorter than the claim, slicing it row by row runs off the end
    and silently returns a short, sheared frame — which is exactly the failure
    this function exists to prevent, arrived at from the other direction.  So
    the claim is checked against the data and the data wins, loudly.
    """
    row = width * 3
    if stride == row:
        return data
    if height and len(data) < stride * height:
        log.warning(
            "unpad_rows: buffer is %d byte(s), but stride %d x %d rows needs "
            "%d — trusting the buffer, not the claim", len(data), stride,
            height, stride * height)
        stride = len(data) // height
        if stride <= row:
            return data
    log.debug("unpad_rows: stride=%d row=%d (%d byte(s) of padding per row)",
              stride, row, stride - row)
    return b"".join(data[y * stride:y * stride + row] for y in range(height))
