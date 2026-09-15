"""PipeWire / xdg-desktop-portal screen capture — the Wayland backend.

Uses the org.freedesktop.portal.ScreenCast D-Bus API to capture screen content
on GNOME, KDE and other Wayland compositors, where the X11 paths in
:mod:`.qt` return black.

**This lived in ``ui/gui`` until 2026-09-15, for no reason.**  It imports
``logging``, ``threading``, ``dbus`` and ``gi`` -- no Qt, no ``ui``, not even
``trcc`` -- and had one consumer, a function-local import in ``trcc_app``.  A
capture backend is an adapter; it sat in the view layer because that is where
somebody happened to be working when they fixed Wayland capture.  The cost was
that only the GUI got the fix: the CLI, the REST route and qtgui had no
Wayland capture at all, and ``StartScreencastDriver`` could not become the one
capture loop because deleting the GUI's timer would have taken PipeWire with
it.

**Verified here; NOT verified end to end.**  The dev box is X11/XFCE and its
``xdg-desktop-portal`` is ``inactive (dead)`` and will not start, so the
portal handshake cannot run.  What that DOES make testable is the path every
non-Wayland user takes, and it was measured rather than assumed: with the
bindings installed, ``start()`` fails in 0.0s instead of waiting out its
30-second timeout, ``grab_frame()`` returns ``None``, ``stop()`` is safe, and
ten start/stop cycles leave zero threads behind.  The handshake itself needs
one run on a Wayland desktop, because the portal's consent dialog needs a
human by design.

Flow:
  1. CreateSession() — create a portal session
  2. SelectSources() — request screen capture (triggers user consent dialog)
  3. Start() — begin streaming via PipeWire
  4. GStreamer pipeline reads PipeWire node → extracts frames

Dependencies (optional, graceful degradation):
  - dbus-python (or dbus-next)
  - PyGObject with GStreamer bindings (gi.repository: Gst, GstApp, GLib)

When the bindings are missing, ``PIPEWIRE_AVAILABLE`` is False and
:class:`PipeWireScreenCapture` answers purely from its fallback -- the Qt
chain -- which is exactly what every face does today.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from ...core._frames import unpad_rows
from ...core.logs import per_frame
from ...core.models import RawFrame
from ...core.ports import ScreenCapture

log = logging.getLogger(__name__)
#: ``_on_new_sample`` fires once per CAPTURED FRAME and logged at INFO.
frame_log = per_frame(__name__)

# Try importing portal/GStreamer dependencies
PIPEWIRE_AVAILABLE = False
_IMPORT_ERROR = ""

try:
    import dbus  # pyright: ignore[reportMissingImports]
    import gi  # pyright: ignore[reportMissingImports]
    from dbus.mainloop.glib import DBusGMainLoop  # pyright: ignore[reportMissingImports]
    gi.require_version('Gst', '1.0')
    gi.require_version('GstApp', '1.0')
    gi.require_version('GstVideo', '1.0')
    # Both ignores are needed and neither is redundant: without PyGObject
    # installed this is a MISSING import, and with it installed the
    # ``gi.repository`` submodules are generated at run time, so a checker
    # that can see the package still cannot see its attributes.
    # Both ignores are needed: without PyGObject this is a MISSING import,
    # and with it installed the ``gi.repository`` submodules are generated at
    # run time, so a checker that sees the package still cannot see its
    # attributes -- hence one per NAME, where pyright reports them.
    from gi.repository import (  # noqa: F401 # pyright: ignore[reportMissingImports]
        GLib,  # pyright: ignore[reportAttributeAccessIssue]
        Gst,  # pyright: ignore[reportAttributeAccessIssue]
        GstApp,  # pyright: ignore[reportAttributeAccessIssue]
        GstVideo,  # pyright: ignore[reportAttributeAccessIssue]
    )
    Gst.init(None)
    PIPEWIRE_AVAILABLE = True
except (ImportError, ValueError) as e:
    _IMPORT_ERROR = str(e)
    log.info("PipeWire capture not available: %s", e)


# Portal D-Bus constants
_PORTAL_BUS = 'org.freedesktop.portal.Desktop'
_PORTAL_PATH = '/org/freedesktop/portal/desktop'
_SCREENCAST_IFACE = 'org.freedesktop.portal.ScreenCast'
_REQUEST_IFACE = 'org.freedesktop.portal.Request'


def _row_stride(buf, caps, width: int) -> int:
    """The stride the BUFFER declares — not the one the caps imply.

    ``VideoInfo.new_from_caps`` returns the format's DEFAULT (aligned) stride,
    which is not always how the buffer is laid out.  ``GstVideoMeta``, when the
    buffer carries one, describes the actual layout and is authoritative.

    MEASURED on four backend/width pairs, comparing each claim against
    ``buffer.get_size()``::

        backend  width   row    caps   meta   buffer      truth
        wlr        854   2562   2564   2562   1_229_760   tight
        wlr       1366   4098   4100   4098   3_147_264   tight
        gnome      854   2562   2564   2564   1_230_720   padded
        gnome     1366   4098   4100   4100   3_148_800   padded

    **GstVideoMeta matched the buffer 4/4; VideoInfo 2/4.**
    ``xdg-desktop-portal-wlr`` hands a tightly packed buffer while the caps
    advertise the aligned stride, so unpadding on the caps claim slices at the
    wrong offsets and CREATES the diagonal shear ``unpad_rows`` exists to
    remove — a 427 px drift at 854 wide, which is a shipping TRCC panel width.
    GNOME is genuinely padded and was never affected.

    ``unpad_rows`` was never wrong here; it was being fed a lie.
    """
    meta = GstVideo.buffer_get_video_meta(buf)
    if meta is not None:
        frame_log.debug("_row_stride: GstVideoMeta says %d", meta.stride[0])
        return meta.stride[0]
    try:
        stride = GstVideo.VideoInfo.new_from_caps(caps).stride[0]
    except Exception as e:                           # pragma: no cover - guard
        log.debug("_row_stride: no meta and no VideoInfo (%s) — tight rows", e)
        return width * 3
    log.debug("_row_stride: no GstVideoMeta; falling back to caps stride %d",
              stride)
    return stride


class PipeWireScreenCast:
    """Portal-based screen capture using PipeWire + GStreamer.

    Usage:
        cast = PipeWireScreenCast()
        if cast.start():
            # Session started, portal dialog shown to user
            frame = cast.grab_frame()  # Returns (width, height, bytes) or None
            ...
            cast.stop()

    Thread safety:
        - GLib main loop runs in a background thread for D-Bus signals
        - grab_frame() is thread-safe (uses a lock)
        - start()/stop() should be called from the main thread
    """

    def __init__(self, restore_token: str | None = None,
                 on_restore_token: Any = None):
        # The portal's half of ``persist_mode``.  Hand back the token it gave
        # us last time and it re-grants silently; omit it and the user is
        # asked again, however many times they have already said yes.
        log.debug("__init__: restore_token=%s on_restore_token=%s", restore_token, on_restore_token)
        self._restore_token = restore_token
        self._on_restore_token = on_restore_token
        self._session_path = None
        self._pipewire_fd = None
        self._node_id = None
        self._pipeline = None
        self._appsink = None
        self._glib_loop = None
        self._glib_thread = None
        self._frame_lock = threading.Lock()
        self._latest_frame = None  # (width, height, bytes_rgb)
        self._running = False
        self._session_ready = threading.Event()
        self._session_failed = threading.Event()

    @property
    def available(self) -> bool:
        """Check if PipeWire capture dependencies are available."""
        frame_log.debug("available")
        return PIPEWIRE_AVAILABLE

    @property
    def is_running(self) -> bool:
        log.debug("is_running")
        return self._running

    def start(self, timeout: float = 30.0) -> bool:
        """Start a portal screen capture session.

        This will trigger a user consent dialog managed by the compositor.
        The user must approve screen sharing before capture begins.

        Args:
            timeout: Seconds to wait for user to approve the portal dialog.

        Returns:
            True if capture session started successfully.
        """
        if not PIPEWIRE_AVAILABLE:
            log.warning("PipeWire not available: %s", _IMPORT_ERROR)
            return False

        if self._running:
            return True

        self._session_ready.clear()
        self._session_failed.clear()

        try:
            self._start_glib_loop()
            self._create_session()
        except Exception as e:
            log.error("Failed to create portal session: %s", e)
            self._cleanup()
            return False

        # Wait for portal dialog approval or failure
        if self._session_ready.wait(timeout):
            self._running = True
            return True

        if self._session_failed.is_set():
            log.error("Portal session was denied or failed")
        else:
            log.error("Portal session timed out (user didn't respond)")

        self._cleanup()
        return False

    def stop(self):
        """Stop capture and clean up all resources."""
        log.debug("stop")
        self._running = False
        self._cleanup()

    def grab_frame(self):
        """Get the latest captured frame.

        Returns:
            Tuple of (width, height, rgb_bytes) or None if no frame available.
            rgb_bytes is raw RGB pixel data (3 bytes per pixel).
        """
        frame_log.debug("grab_frame")
        with self._frame_lock:
            return self._latest_frame

    # --- Internal: D-Bus portal flow ---

    def _start_glib_loop(self):
        """Start GLib main loop in background thread for D-Bus signals."""
        log.debug("_start_glib_loop")
        DBusGMainLoop(set_as_default=True)
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(
            target=self._glib_loop.run, daemon=True)
        self._glib_thread.start()

    def _await_response(self, bus, request_path, handler) -> None:
        """Subscribe to one Request's single ``Response``, then unsubscribe.

        A ``org.freedesktop.portal.Request`` emits ``Response`` EXACTLY once
        and is then destroyed, so the receiver that waited for it is spent.
        Nothing removed ours, and dbus-python keeps a match rule per receiver
        for the life of the connection.

        MEASURED -- receivers alive on the session bus after each capture
        session, with removal disabled and then enabled::

            session   1   2   3
            leaking   3   6   9      <- three Requests, none released
            fixed     0   0   0

        Three per session, forever, on a bus where every rule is evaluated
        against every signal.  A screencast theme restarts its session on each
        apply, so this grows with use rather than with time.

        The ``fired`` check is not belt-and-braces: the Response is dispatched
        on the GLib loop THREAD while this one is still returning from
        ``add_signal_receiver``, so it can land before ``holder`` is filled.
        Removing afterwards in that case is why the match is dropped in two
        places rather than one.

        Do NOT add ``bus_name=`` to scope the match: measured, it stops the
        Response being delivered at all and ``start()`` times out after 25 s.
        """
        log.debug("_await_response: subscribing to %s", request_path)
        holder: list = []
        fired = threading.Event()

        def dispatch(response, results):
            if fired.is_set():
                log.debug("_await_response: duplicate Response on %s ignored",
                          request_path)
                return
            fired.set()
            if holder:
                holder[0].remove()
            handler(response, results)

        match = bus.add_signal_receiver(
            dispatch,
            signal_name='Response',
            dbus_interface=_REQUEST_IFACE,
            path=request_path,
        )
        holder.append(match)
        if fired.is_set():
            match.remove()

    def _create_session(self):
        """Step 1: CreateSession on the ScreenCast portal."""
        log.debug("_create_session")
        bus = dbus.SessionBus()
        portal = bus.get_object(_PORTAL_BUS, _PORTAL_PATH)
        screencast = dbus.Interface(portal, _SCREENCAST_IFACE)

        # Unique token for this session
        import random
        token = f"trcc_{random.randint(100000, 999999)}"
        session_token = f"trcc_session_{random.randint(100000, 999999)}"

        request_path = screencast.CreateSession(
            dbus.Dictionary({
                'handle_token': dbus.String(token),
                'session_handle_token': dbus.String(session_token),
            }, signature='sv')
        )

        self._await_response(bus, request_path, self._on_create_session_response)

    def _on_create_session_response(self, response, results):
        """Handle CreateSession response."""
        log.info("_on_create_session_response")
        if response != 0:
            log.error("CreateSession failed with response %d", response)
            self._session_failed.set()
            return

        self._session_path = str(results.get('session_handle', ''))
        if not self._session_path:
            log.error("No session handle in CreateSession response")
            self._session_failed.set()
            return

        log.info("Portal session created: %s", self._session_path)
        self._select_sources()

    def _select_sources(self):
        """Step 2: SelectSources — request monitor capture."""
        log.debug("_select_sources")
        bus = dbus.SessionBus()
        portal = bus.get_object(_PORTAL_BUS, _PORTAL_PATH)
        screencast = dbus.Interface(portal, _SCREENCAST_IFACE)

        import random
        token = f"trcc_src_{random.randint(100000, 999999)}"

        request_path = screencast.SelectSources(
            dbus.ObjectPath(self._session_path),
            dbus.Dictionary(self._select_options(token), signature='sv')
        )

        self._await_response(bus, request_path, self._on_select_sources_response)

    def _select_options(self, token: str) -> dict:
        """Options for ``SelectSources``, replaying a stored token if we have one.

        ``persist_mode: 2`` asks the portal to remember the grant, and the
        portal answers with a ``restore_token`` on Start.  Both halves are
        required and only the first was here: we requested persistence and
        then dropped the token, so every launch asked again -- for a
        screencast theme meant to resume on boot, the difference between "it
        comes back" and "it asks permission every time you log in".
        """
        options: dict = {
            'handle_token': dbus.String(token),
            'types': dbus.UInt32(1),       # 1 = MONITOR (not window)
            'multiple': dbus.Boolean(False),
            'persist_mode': dbus.UInt32(2),  # 2 = persist until revoked
        }
        if self._restore_token:
            log.info("_select_options: replaying a stored restore token — the "
                     "portal should re-grant without asking")
            options['restore_token'] = dbus.String(self._restore_token)
        else:
            log.info("_select_options: no stored token — the portal will ask")
        return options

    def _remember_restore_token(self, results) -> None:
        """Keep the token the portal issued, so the next run is not asked.

        The portal may decline to issue one (the user chose "this time only",
        or the backend does not implement persistence), and that is not an
        error -- it simply means the next start prompts.
        """
        token = results.get('restore_token')
        if not token:
            log.info("_remember_restore_token: portal issued none — the next "
                     "start will ask again")
            return
        log.info("_remember_restore_token: storing a fresh restore token")
        self._restore_token = str(token)
        if self._on_restore_token is not None:
            self._on_restore_token(self._restore_token)

    def _on_select_sources_response(self, response, results):
        """Handle SelectSources response."""
        log.info("_on_select_sources_response")
        if response != 0:
            log.error("SelectSources failed with response %d", response)
            self._session_failed.set()
            return

        log.info("Sources selected, starting stream...")
        self._start_stream()

    def _start_stream(self):
        """Step 3: Start — begin PipeWire stream (triggers consent dialog)."""
        log.debug("_start_stream")
        bus = dbus.SessionBus()
        portal = bus.get_object(_PORTAL_BUS, _PORTAL_PATH)
        screencast = dbus.Interface(portal, _SCREENCAST_IFACE)

        import random
        token = f"trcc_start_{random.randint(100000, 999999)}"

        request_path = screencast.Start(
            dbus.ObjectPath(self._session_path),
            dbus.String(''),  # parent_window (empty = no parent)
            dbus.Dictionary({
                'handle_token': dbus.String(token),
            }, signature='sv')
        )

        self._await_response(bus, request_path, self._on_start_response)

    def _on_start_response(self, response, results):
        """Handle Start response — get PipeWire node ID and start pipeline."""
        log.info("_on_start_response")
        if response != 0:
            log.error("Start failed with response %d (user denied?)",
                         response)
            self._session_failed.set()
            return

        self._remember_restore_token(results)

        streams = results.get('streams', [])
        if not streams:
            log.error("No streams in Start response")
            self._session_failed.set()
            return

        # streams is array of (node_id, properties)
        self._node_id = int(streams[0][0])
        log.info("PipeWire node ID: %d", self._node_id)

        # Get PipeWire file descriptor
        try:
            bus = dbus.SessionBus()
            portal = bus.get_object(_PORTAL_BUS, _PORTAL_PATH)
            screencast = dbus.Interface(portal, _SCREENCAST_IFACE)
            self._pipewire_fd = screencast.OpenPipeWireRemote(
                dbus.ObjectPath(self._session_path),
                dbus.Dictionary({}, signature='sv'),
            ).take()
        except Exception as e:
            log.error("OpenPipeWireRemote failed: %s", e)
            self._session_failed.set()
            return

        # Start GStreamer pipeline
        try:
            self._start_gstreamer()
            self._session_ready.set()
        except Exception as e:
            log.error("GStreamer pipeline failed: %s", e)
            self._session_failed.set()

    # --- Internal: GStreamer pipeline ---

    def _start_gstreamer(self):
        """Create and start GStreamer pipeline to read PipeWire frames."""
        # Pipeline: pipewiresrc → videoconvert → RGB → appsink
        pipeline_str = (
            f"pipewiresrc fd={self._pipewire_fd} path={self._node_id} "
            f"do-timestamp=true keepalive-time=1000 ! "
            f"videoconvert ! "
            f"video/x-raw,format=RGB ! "
            f"appsink name=sink emit-signals=true max-buffers=2 drop=true"
        )

        self._pipeline = Gst.parse_launch(pipeline_str)
        self._appsink = self._pipeline.get_by_name('sink')
        self._appsink.connect('new-sample', self._on_new_sample)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start GStreamer pipeline")

        log.info("GStreamer pipeline started")

    def _on_new_sample(self, sink):
        """GStreamer callback: new frame available from PipeWire."""
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()

        struct = caps.get_structure(0)
        width = struct.get_int('width')[1]
        height = struct.get_int('height')[1]
        stride = _row_stride(buf, caps, width)

        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        try:
            # Un-padded at the SOURCE so every consumer -- the gui's own tick
            # and the ScreenCapture adapter below -- gets tightly packed RGB24
            # and neither has to know what a stride is.
            rgb_bytes = unpad_rows(bytes(map_info.data), width, height, stride)
            frame_log.debug("_on_new_sample: %dx%d stride=%d -> %d byte(s)",
                            width, height, stride, len(rgb_bytes))
            with self._frame_lock:
                self._latest_frame = (width, height, rgb_bytes)
        finally:
            buf.unmap(map_info)

        return Gst.FlowReturn.OK

    # --- Internal: Cleanup ---

    def _cleanup(self):
        """Stop pipeline, close FD, quit GLib loop."""
        if self._pipeline:
            try:
                self._pipeline.set_state(Gst.State.NULL)
            except Exception as e:
                # GStreamer/GObject errors don't share a Python base — broad + log.
                log.debug("pipewire cleanup: pipeline.set_state raised: %s", e)
            self._pipeline = None
            self._appsink = None

        if self._pipewire_fd is not None:
            try:
                import os
                os.close(self._pipewire_fd)
            except OSError as e:
                log.debug("pipewire cleanup: fd close raised: %s", e)
            self._pipewire_fd = None

        if self._session_path:
            try:
                bus = dbus.SessionBus()
                session = bus.get_object(_PORTAL_BUS, self._session_path)
                session_iface = dbus.Interface(
                    session, 'org.freedesktop.portal.Session')
                session_iface.Close()
            except Exception as e:
                # dbus exceptions don't share a clean Python base — broad + log.
                log.debug("pipewire cleanup: portal Close raised: %s", e)
            self._session_path = None

        if self._glib_loop and self._glib_loop.is_running():
            try:
                self._glib_loop.quit()
            except (RuntimeError, AttributeError) as e:
                log.debug("pipewire cleanup: glib_loop.quit raised: %s", e)
            self._glib_loop = None

        self._node_id = None
        self._latest_frame = None

    def __del__(self):
        log.debug("__del__")
        self.stop()


def crop_rgb24(
    data: bytes, src_w: int, src_h: int,
    x: int, y: int, width: int, height: int,
) -> RawFrame:
    """Cut a region out of a tightly packed RGB24 full-screen buffer.

    Clamped to the source, because the portal hands back whatever the user
    chose to share and a region picked against a different geometry would
    otherwise index past the end.
    """
    log.debug("crop_rgb24: data=%s src_w=%s", data, src_w)
    x0, y0 = max(0, min(x, src_w)), max(0, min(y, src_h))
    x1, y1 = max(x0, min(x + width, src_w)), max(y0, min(y + height, src_h))
    out_w, out_h = x1 - x0, y1 - y0
    row = src_w * 3
    rows = [data[r * row + x0 * 3:r * row + x1 * 3] for r in range(y0, y1)]
    return RawFrame(data=b"".join(rows), width=out_w, height=out_h)


class PipeWireScreenCapture(ScreenCapture):
    """The Wayland backend, behind the stateless port, with a fallback.

    The port asks for a rectangle NOW; the portal is a session that must be
    created, consented to and streamed.  The gui already reconciled those two
    and its policy is kept rather than reinvented: **start the session in the
    background and serve the fallback until it is ready.**

    So ``grab_region`` never blocks.  That also dissolves a recorded
    collision -- ``start(timeout=30.0)`` against ``AppProxy``'s 30 s IPC
    timeout -- because nothing waits on the portal any more.

    When the bindings are absent or the portal refuses, every call is the
    fallback, which is precisely what each face does today.  The session is
    created on the first grab, never at construction: building the platform's
    capture source must not raise a consent dialog at somebody who never asked
    for a screencast.
    """

    #: Where the portal's restore token is kept, under the caller's config dir.
    TOKEN_FILE = "portal-restore-token"
    #: Seconds of an unchanging frame before we say the stream stalled.
    #: Well above any real cadence (the panel refreshes at 15-30 fps),
    #: so a slow source is never called a stall.
    STALL_AFTER = 5.0

    def __init__(
        self,
        fallback: ScreenCapture,
        *,
        config_dir: Path | None = None,
        session_factory: Any = None,
        start_timeout: float = 30.0,
    ) -> None:
        log.info("PipeWireScreenCapture: available=%s fallback=%s config_dir=%s",
                 PIPEWIRE_AVAILABLE, type(fallback).__name__, config_dir)
        self._fallback = fallback
        self._factory = session_factory or PipeWireScreenCast
        self._start_timeout = start_timeout
        # The directory is passed in rather than resolved here: this adapter
        # must not import the system package (which imports this one back),
        # and both callers already hold a ``Paths``.  Without one the token
        # lives for the run only, which is the old behaviour.
        self._token_path = (config_dir / self.TOKEN_FILE
                            if config_dir is not None else None)
        self._session: Any = None
        self._started = False
        self._lock = threading.Lock()
        #: Last frame OBJECT handed out, held so identity comparison is safe.
        self._last_frame: Any = None
        self._last_change = 0.0
        self._stall_warned = False

    def grab_region(self, x: int, y: int, width: int, height: int) -> RawFrame:
        """The portal's frame when the session is up, else the fallback's."""
        log.debug("grab_region: x=%s y=%s", x, y)
        frame = self._portal_region(x, y, width, height)
        if frame is not None:
            return frame
        return self._fallback.grab_region(x, y, width, height)

    def _portal_region(
        self, x: int, y: int, width: int, height: int,
    ) -> RawFrame | None:
        session = self._ensure_session()
        if session is None or not session.is_running:
            return None
        latest = session.grab_frame()
        self._watch_for_stall(latest)
        if latest is None:
            frame_log.debug("_portal_region: session up but no frame yet")
            return None
        src_w, src_h, data = latest
        return crop_rgb24(data, src_w, src_h, x, y, width, height)

    def _watch_for_stall(self, latest: Any) -> None:
        """Say so, ONCE, when a running session stops delivering new frames.

        ``start()`` returning True means the portal granted capture, not that
        pixels are flowing, and the two come apart in practice.

        MEASURED on xdg-desktop-portal-wlr 0.8.4 + sway 1.11, three identical
        runs at one resolution with a 30 fps client repainting on screen and
        the damage independently confirmed by ``grim``::

            run 1   211 frames / 10s  (~21 fps)   healthy
            run 2     1 frame  / 10s              stalled after the first
            run 3     1 frame  / 10s              stalled after the first

        So it is INTERMITTENT, not deterministic -- the same code and the same
        compositor either stream or deliver one buffer and stop.  ``grim``
        (``zwlr_screencopy``) captured 6/6 distinct images throughout, so the
        compositor keeps producing and the fault is in the portal's
        ``ext-image-copy-capture-v1`` path, not in the frames existing.

        Intermittence is exactly why this warns rather than being left to a
        reporter to characterise: two runs in three look like a broken panel.

        The user-visible result is a panel frozen on its first frame, and
        nothing in the log said so: the only line here was a per-frame DEBUG
        that a default run never writes.  The failure is upstream, but silence
        about it is ours, and a reporter cannot tell the two apart.

        Staleness is a HELD REFERENCE compared by identity, never ``id()``:
        CPython recycles ids of dead objects, which silently broke a cache key
        in this project before.  A reference we hold cannot be recycled.
        """
        now = time.monotonic()
        if latest is not self._last_frame or not self._last_change:
            self._last_frame = latest
            self._last_change = now
            self._stall_warned = False
            return
        if self._stall_warned or now - self._last_change < self.STALL_AFTER:
            return
        log.warning(
            "screencast: the portal session is running but has produced no "
            "new frame for %.1fs — the panel is showing a frozen image. On "
            "wlroots compositors this is a known upstream stall in "
            "xdg-desktop-portal-wlr's ext-image-copy-capture path, not TRCC.",
            now - self._last_change)
        self._stall_warned = True

    def _ensure_session(self) -> Any:
        """Create and start the session ONCE, off the calling thread."""
        if not PIPEWIRE_AVAILABLE:
            return None
        with self._lock:
            if self._started:
                return self._session
            self._started = True
            self._session = self._factory(
                restore_token=self._read_token(),
                on_restore_token=self._write_token,
            )
            log.info("PipeWireScreenCapture: starting the portal session in "
                     "the background; the fallback answers until it is up")
            threading.Thread(
                target=self._start_session, daemon=True,
                name="trcc-portal-start",
            ).start()
            return self._session

    def _start_session(self) -> None:
        session = self._session
        if session is None:
            return
        if not session.start(timeout=self._start_timeout):
            log.warning("PipeWireScreenCapture: portal session did not start "
                        "— staying on the fallback for this run")
            self._session = None

    def _read_token(self) -> str | None:
        """The token from a previous run, if one was kept."""
        if self._token_path is None or not self._token_path.exists():
            return None
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("_read_token: could not read %s (%s) — the portal "
                        "will ask again", self._token_path, e)
            return None
        log.info("_read_token: restore token found at %s", self._token_path)
        return token or None

    def _write_token(self, token: str) -> None:
        """Keep *token* for the next run.

        Owner-only, because it is a capability: anyone who can read it can ask
        the portal to re-grant this app's screen access without a prompt.  A
        failure here costs a prompt next time, never a capture.
        """
        if self._token_path is None:
            log.info("_write_token: no config dir — the token lives for this "
                     "run only, so the next start will ask again")
            return
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(token, encoding="utf-8")
            self._token_path.chmod(0o600)
            log.info("_write_token: stored at %s", self._token_path)
        except OSError as e:
            log.warning("_write_token: could not store at %s (%s) — the "
                        "portal will ask again next run", self._token_path, e)

    def stop(self) -> None:
        """Tear the session down; the next grab starts a fresh one."""
        log.info("PipeWireScreenCapture.stop")
        with self._lock:
            session, self._session, self._started = self._session, None, False
        if session is not None:
            session.stop()
