#!/usr/bin/env python3
"""Verify Wayland screen capture end to end, on a box that is not running it.

Brings up a REAL Wayland compositor and a REAL xdg-desktop-portal on a PRIVATE
D-Bus session, drives the shipping ``PipeWireScreenCapture``, and checks the
pixels against a target this file authors.  No VM, no logging out, and nothing
touching the developer's own desktop -- it runs happily from an X11 session.

**Why this exists.**  "Wayland capture needs a reporter" was true for months
and then stopped being true.  In one afternoon this rig found the stride bug
(``GstVideoMeta`` vs caps, a 427 px shear at 854 -- a shipping panel width),
caught a ``bus_name=`` change that silently stopped the portal answering at
all, and disproved a bug report of mine that was really my own harness calling
``start()`` twice.  See ``memory/project_the_wayland_handshake_works.md``.

**Requires** (Fedora names; nothing is installed for you):

    sway  xdg-desktop-portal-wlr  grim          # the wlroots stack
    mutter  xdg-desktop-portal-gnome            # the GNOME stack
    python3-dbus  python3-gobject               # already a runtime dep

    python3.12 dev/wayland_capture_rig.py                 # sway, 854x480
    python3.12 dev/wayland_capture_rig.py --width 1366    # exercise the stride
    python3.12 dev/wayland_capture_rig.py --compositor gnome

Exit 0 when every claim holds, 1 otherwise, 2 when the rig could not set the
scene (which is NOT a product failure and must not be read as one).

FOUR RULES, each learned by getting it wrong:

1. **Backend before frontend.**  An ``xdg-desktop-portal`` that starts first
   registers WITHOUT ScreenCast and then holds the bus name, so the interface
   is simply absent and the error says nothing.
2. **Reap stale sockets by LISTENER, not by name.**  A killed compositor
   leaves ``wayland-N`` behind; the next one reuses the freed name, so "a new
   socket appeared" never fires.  ``ss -lx`` knows which are really bound.
3. **Drive the adapter through ``_ensure_session()``, never ``start()``.**
   ``_ensure_session`` already starts the session on a background thread.
   Calling ``start()`` as well runs TWO portal sessions, doubles every handler
   and raises ``Can only start once`` -- which I reported as an adapter bug
   before finding it was this.
4. **A damage source is part of the instrument.**  A compositor with nothing
   moving on it correctly sends ONE frame and then nothing.  Measured without
   one, the healthiest stack on earth looks stalled; that mistake produced a
   confident, wrong "GNOME stalls after the first frame".
5. **Keep this process Qt-free.**  A ``QGuiApplication`` here installs Qt's own
   GLib main-context integration and the portal handshake then never finishes:
   the backend logs "create session method invoked" and nothing more, because
   the adapter's MainLoop thread never dispatches the Response and so never
   sends SelectSources.  It presents as a 25 s consent timeout on a stack
   configured never to prompt.  Everything Qt runs in a subprocess.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#: Left/right halves of the target.  A sharp vertical EDGE, not a flat colour:
#: a stride shear displaces each row by a couple of BYTES, accumulating down
#: the image, and a flat fill hides that completely because the row start is
#: still that row's colour.  An edge turns the same defect into a slant whose
#: drift is measurable in PIXELS.
LEFT = (220, 30, 30)
RIGHT = (30, 60, 220)
#: Seconds to watch for frames once the session is up.
WATCH = 8.0


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _have(*tools: str) -> list[str]:
    """Which of *tools* are missing from PATH (plus the portal libexecs)."""
    missing = [t for t in tools if shutil.which(t) is None]
    return missing


class Rig:
    """An isolated compositor + portal stack that cleans up after itself."""

    def __init__(self, compositor: str, width: int, height: int,
                 workdir: Path) -> None:
        self.compositor = compositor
        self.w, self.h = width, height
        self.dir = workdir
        self.env = dict(os.environ)
        self.procs: list[subprocess.Popen] = []
        self.output = "HEADLESS-1" if compositor == "sway" else "Meta-0"

    # -- lifecycle ----------------------------------------------------

    def _spawn(self, cmd: list[str], log: str, env: dict) -> subprocess.Popen:
        fh = (self.dir / log).open("wb")
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def _private_bus(self) -> None:
        out = _run(["dbus-daemon", "--session", "--print-address=1",
                    "--print-pid=1", "--fork"]).stdout.split()
        self.env["DBUS_SESSION_BUS_ADDRESS"] = out[0]
        self._bus_pid = int(out[1])

    def _bus_has(self, name: str, timeout: float = 12.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if name in _run(["busctl", "--address",
                             self.env["DBUS_SESSION_BUS_ADDRESS"],
                             "list"]).stdout:
                return True
            time.sleep(0.4)
        return False

    @staticmethod
    def _live_sockets() -> set[str]:
        """Wayland sockets with a real listener -- see rule 2."""
        rt = os.environ.get("XDG_RUNTIME_DIR", "")
        bound = _run(["ss", "-lx"]).stdout
        return {p.name for p in Path(rt).glob("wayland-[0-9]*")
                if not p.name.endswith(".lock") and str(p) in bound}

    def up(self) -> None:
        self._private_bus()
        if self.compositor == "sway":
            self._up_sway()
        else:
            self._up_mutter()

    def _up_sway(self) -> None:
        cfgdir = self.dir / "cfg" / "xdg-desktop-portal-wlr"
        cfgdir.mkdir(parents=True, exist_ok=True)
        # chooser_type=none is the headless mode: xdpw picks the output itself
        # instead of spawning a picker, which is what makes this repeatable
        # rather than a ritual with a mouse.
        (cfgdir / "config").write_text(
            f"[screencast]\nchooser_type=none\noutput_name={self.output}\n"
            f"max_fps=30\n")
        (self.dir / "sway.cfg").write_text(
            f"output {self.output} mode {self.w}x{self.h}\n"
            f"default_border none\n")

        before = self._live_sockets()
        env = {**self.env, "WLR_BACKENDS": "headless",
               "WLR_LIBINPUT_NO_DEVICES": "1", "WLR_HEADLESS_OUTPUTS": "1",
               "XDG_CURRENT_DESKTOP": "sway",
               "XDG_CONFIG_HOME": str(self.dir / "cfg")}
        env.pop("WAYLAND_DISPLAY", None)
        self._spawn(["sway", "-c", str(self.dir / "sway.cfg")], "sway.log", env)

        end = time.time() + 12
        while time.time() < end:
            new = self._live_sockets() - before
            if new:
                self.env["WAYLAND_DISPLAY"] = new.pop()
                break
            time.sleep(0.4)
        else:
            raise RuntimeError("sway never created a wayland socket")
        self.env["XDG_CURRENT_DESKTOP"] = "sway"
        self.env["XDG_CONFIG_HOME"] = str(self.dir / "cfg")
        # -c is passed EXPLICITLY rather than trusting XDG_CONFIG_HOME: the
        # whole run hinges on chooser_type=none being read, and an unread
        # config fails as a consent dialog nobody can click -- i.e. as a
        # 25 s timeout that names neither the config nor the chooser.
        self._start_portal("/usr/libexec/xdg-desktop-portal-wlr",
                           "org.freedesktop.impl.portal.desktop.wlr",
                           extra=["-c", str(cfgdir / "config"), "-l", "INFO"])

    def _up_mutter(self) -> None:
        self.env["XDG_CURRENT_DESKTOP"] = "GNOME"
        self._spawn(["mutter", "--headless", "--virtual-monitor",
                     f"{self.w}x{self.h}"], "mutter.log", self.env)
        if not self._bus_has("org.gnome.Mutter.ScreenCast"):
            raise RuntimeError("mutter never exposed ScreenCast")
        rt = os.environ.get("XDG_RUNTIME_DIR", "")
        for p in sorted(Path(rt).glob("wayland-[0-9]*")):
            if not p.name.endswith(".lock"):
                self.env["WAYLAND_DISPLAY"] = p.name
        self._start_portal("/usr/libexec/xdg-desktop-portal-gnome",
                           "org.freedesktop.impl.portal.desktop.gnome")

    def _start_portal(self, backend: str, name: str,
                      extra: list[str] | None = None) -> None:
        # Rule 1: backend first, and WAIT for its name, or the frontend wins
        # the race and registers without ScreenCast.
        self._spawn([backend, *(extra or [])], "portal-backend.log",
                    self.env)
        if not self._bus_has(name):
            raise RuntimeError(f"{name} never appeared on the bus")
        self._spawn(["/usr/libexec/xdg-desktop-portal", "--verbose"],
                    "portal.log", self.env)
        time.sleep(4)

    def source_types(self) -> str:
        return _run(["busctl", "--address",
                     self.env["DBUS_SESSION_BUS_ADDRESS"], "get-property",
                     "org.freedesktop.portal.Desktop",
                     "/org/freedesktop/portal/desktop",
                     "org.freedesktop.portal.ScreenCast",
                     "AvailableSourceTypes"]).stdout.strip()

    def start_target(self) -> None:
        """The target surface, which is also the damage source (rule 4).

        A compositor with nothing moving correctly sends ONE frame and stops,
        so a run without this cannot tell healthy from stalled.
        """
        src = self.dir / "animator.py"
        src.write_text(_ANIMATOR.format(left=LEFT, right=RIGHT))
        self._spawn([sys.executable, str(src)], "animator.log",
                    {**self.env, "QT_QPA_PLATFORM": "wayland"})
        time.sleep(3)

    def down(self) -> None:
        for p in self.procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1.5)
        for p in self.procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.kill(self._bus_pid, signal.SIGTERM)
        except (ProcessLookupError, AttributeError):
            pass


_ANIMATOR = '''\
import sys
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget
LEFT, RIGHT = {left}, {right}
class Target(QWidget):
    """Paints the TARGET and supplies the damage, in ONE surface.

    These began as a swaybg wallpaper plus a separate small window, which does
    not work: sway TILES, so a new window fills the workspace however it was
    resized, and the captured frame came back 100% animator with the target
    nowhere in it -- which reads as edge_x=0, i.e. as a shear the adapter had
    not caused.  One surface that is both cannot be covered by the other.
    """
    def __init__(self):
        super().__init__(); self.i = 0
        t = QTimer(self); t.timeout.connect(self.tick); t.start(33)
    def tick(self):
        self.i += 1; self.update()
    def paintEvent(self, _):
        p, w, h = QPainter(self), self.width(), self.height()
        p.fillRect(0, 0, w // 2, h, QColor(*LEFT))
        p.fillRect(w // 2, 0, w - w // 2, h, QColor(*RIGHT))
        # The damage: a block sweeping across the TOP BAND only, so the edge
        # stays clean everywhere the run actually measures it.
        p.fillRect(self.i % max(1, w - 60), 4, 50, max(8, h // 8),
                   QColor(255, 255, 255))
app = QApplication(sys.argv)
t = Target(); t.showFullScreen(); sys.exit(app.exec())
'''


def edge_at(data: bytes, w: int, y: int) -> int:
    base = y * w * 3
    for x in range(w):
        p = data[base + x * 3: base + x * 3 + 3]
        if sum(abs(a - b) for a, b in zip(p, LEFT, strict=True)) > 90:
            return x
    return -1


def verify(rig: Rig, edge: int) -> bool | None:
    """Drive the SHIPPING adapter and check every claim.

    ``None`` means the session never started, which is a DIFFERENT outcome
    from failing a claim and must not be reported as a product failure.
    """
    # The adapter runs IN THIS PROCESS and resolves the bus from os.environ,
    # so the private address has to land there -- setting it only on the
    # children's env sends the adapter to the developer's REAL session bus,
    # where the ScreenCast interface does not exist.  The symptom is a portal
    # that the rig can introspect and the adapter cannot reach.
    os.environ.update(rig.env)

    from trcc.adapters.screencast.pipewire import PipeWireScreenCapture
    from trcc.core.models import RawFrame
    from trcc.core.ports import ScreenCapture

    class Fallback(ScreenCapture):
        def grab_region(self, x, y, w, h):
            return RawFrame(data=b"\0" * (w * h * 3), width=w, height=h)

    cap = PipeWireScreenCapture(Fallback(), config_dir=rig.dir / "conf")
    cap._ensure_session()                      # rule 3: NEVER also call start()
    end = time.time() + 30
    while time.time() < end:
        s = cap._session
        if s is not None and getattr(s, "is_running", False):
            break
        time.sleep(0.25)
    else:
        print("  the portal session never started", file=sys.stderr)
        return None

    seen: set[str] = set()
    last = None
    t0 = time.time()
    while time.time() - t0 < WATCH:
        f = cap._session.grab_frame()
        if f is not None:
            last = f
            seen.add(hashlib.sha1(f[2]).hexdigest()[:10])
        time.sleep(0.04)
    cap.stop()

    if last is None:
        print("  CLAIM 1  frames arrive        : NO")
        return False
    w, h, data = last
    row, packed = w * 3, w * h * 3
    # Rows below the animator's corner, so the damage source is
    # never mistaken for a shear in the target.
    rows = list(range(h // 3, h, max(1, h // 24)))
    found = [x for x in (edge_at(data, w, y) for y in rows) if x >= 0]
    drift = (max(found) - min(found)) if found else -1
    bright = sum(1 for i in range(0, min(len(data), 300_000), 3) if data[i] > 20)

    # Keep what we actually saw.  A CLAIM 3 failure is a picture problem and
    # the number alone cannot say whether the shear is real, the wallpaper
    # never painted, or something is sitting on top of the target.
    shot = rig.dir / "captured.ppm"
    shot.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + data[:packed])

    ok_packed = len(data) == packed
    ok_shear = bool(found) and drift <= 1 and abs(found[0] - edge) <= 1
    print(f"  CLAIM 1  frames arrive        : YES  {w}x{h}, {len(data)} bytes")
    print(f"  CLAIM 2  has content          : {'YES' if bright else 'NO'}"
          f"  ({bright} bright samples)")
    print(f"  CLAIM 3  no stride shear      : {'YES' if ok_shear else 'NO'}"
          f"  edge_x={found[0] if found else '?'} drift={drift}px "
          f"(expect {edge})")
    print(f"  CLAIM 4  tightly packed       : {'YES' if ok_packed else 'NO'}"
          f"  (got {len(data)}, packed {packed}, row={row}, "
          f"padded={'yes' if row % 4 else 'no'})")
    print(f"  frames in {WATCH:.0f}s              : {len(seen)} distinct")
    if len(seen) <= 1:
        print("  NOTE: one frame only.  On wlroots this is the known "
              "INTERMITTENT xdg-desktop-portal-wlr stall (measured 211/1/1 "
              "over three identical runs), not a TRCC regression -- re-run.")
    return ok_packed and ok_shear and bool(bright)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--compositor", choices=("sway", "gnome"), default="sway",
                    help="sway grants without a dialog (chooser_type=none) and "
                         "is the only one that verifies unattended; gnome "
                         "always prompts, so it exits 2 unless a human clicks "
                         "Share")
    ap.add_argument("--width", type=int, default=854,
                    help="854 and 1366 are PADDED (w*3 %% 4 != 0) and exercise "
                         "the stride path; 1280 does not")
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--no-target", action="store_true",
                    help="skip the target surface — nothing then repaints, so "
                         "ONE frame is CORRECT and the run cannot tell healthy "
                         "from stalled (rule 4).  Diagnostic only.")
    args = ap.parse_args()

    need = ("sway", "grim") if args.compositor == "sway" else ("mutter",)
    missing = _have("dbus-daemon", "busctl", "ss", *need)
    backend = ("/usr/libexec/xdg-desktop-portal-wlr"
               if args.compositor == "sway"
               else "/usr/libexec/xdg-desktop-portal-gnome")
    if not Path(backend).exists():
        missing.append(Path(backend).name)
    if missing:
        print(f"SKIP: not installed: {', '.join(missing)}", file=sys.stderr)
        return 2

    row = args.width * 3
    print(f"{args.compositor} {args.width}x{args.height}  row={row}  "
          f"padded={'YES — exercises the stride path' if row % 4 else 'no'}")

    ok = False
    work = Path(tempfile.mkdtemp(prefix="trcc-wayland-rig-"))
    rig = Rig(args.compositor, args.width, args.height, work)
    try:
        edge = args.width // 2          # the target paints it there
        rig.up()
        print(f"  portal AvailableSourceTypes : {rig.source_types()}")
        if not args.no_target:
            rig.start_target()
        alive = [p.pid for p in rig.procs if p.poll() is None]
        print(f"  stack alive                 : {len(alive)} process(es)")
        ok = verify(rig, edge)
        if ok is None:
            if args.compositor == "gnome":
                print("SKIP: GNOME's portal ALWAYS asks for consent and "
                      "nothing here can click it.\n"
                      "      Use --compositor sway, whose xdg-desktop-portal-wlr "
                      "takes chooser_type=none\n"
                      "      and grants without a dialog.  A GNOME run needs a "
                      "human at the screen.", file=sys.stderr)
                return 2
            print("FAIL: the session never started", file=sys.stderr)
            return 1
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    except RuntimeError as e:
        print(f"SKIP: could not set the scene: {e}", file=sys.stderr)
        return 2
    finally:
        rig.down()
        # Keep the scene on failure: sway.log, portal.log, portal-backend.log
        # and the generated configs are the whole diagnosis, and a harness you
        # cannot look inside after a red run is a harness you stop trusting.
        if ok:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"  logs + configs kept in {work}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
