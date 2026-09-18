"""DataInstallService.ensure_all — per-resolution + per-orientation archives.

A non-square panel uses both orientations (854x480 landscape AND 480x854
portrait): themes, web (cloud backgrounds), and masks all need the rotated
counterpart, or a rotated device has no oriented catalog to load.

``conftest._stub_data_install`` (autouse) noops ``ensure_all`` so no test hits
the network — so we capture the REAL method at import (before the patch) and
call it directly.
"""
from __future__ import annotations

from pathlib import Path

from trcc.services.data_install import DataInstallService

from .conftest import FakePlatform

_REAL_ENSURE_ALL = DataInstallService.ensure_all


class _RecordingInstaller:
    """Records every archive_name install() is asked for; installs nothing."""

    def __init__(self) -> None:
        self.archives: list[str] = []

    def install(self, *, archive_name: str, target_dir: Path,
                subpath: str | None = None) -> bool:
        self.archives.append(archive_name)
        return True


def _run(tmp_home: Path, resolution: tuple[int, int]) -> list[str]:
    paths = FakePlatform(tmp_home).paths()
    rec = _RecordingInstaller()
    _REAL_ENSURE_ALL(DataInstallService(paths, rec), resolution)  # type: ignore[arg-type]
    return rec.archives


def test_ensure_all_installs_all_three_for_the_native_resolution(
    tmp_home: Path,
) -> None:
    archives = _run(tmp_home, (854, 480))
    assert "theme854480.7z" in archives
    assert "854480.7z" in archives        # web / cloud backgrounds
    assert "zt854480.7z" in archives      # masks


def test_ensure_all_installs_themes_web_and_masks_for_rotated_orientation(
    tmp_home: Path,
) -> None:
    """Non-square → the rotated orientation gets ALL THREE, themes included —
    the bug was themes being skipped, so a portrait panel had no theme/cloud
    backgrounds in its oriented dir."""
    archives = _run(tmp_home, (854, 480))
    assert "theme480854.7z" in archives, "portrait themes must be installed"
    assert "480854.7z" in archives        # portrait web / cloud backgrounds
    assert "zt480854.7z" in archives      # portrait masks


def test_ensure_all_square_does_not_install_a_rotated_counterpart(
    tmp_home: Path,
) -> None:
    archives = _run(tmp_home, (320, 320))
    assert archives == ["theme320320.7z", "320320.7z", "zt320320.7z"]


def test_http_data_installer_implements_the_core_data_installer_port() -> None:
    """Step 5: the concrete installer subclasses the core DataInstaller ABC."""
    from trcc.adapters.repo.data_install import HttpDataInstaller
    from trcc.core.ports import DataInstaller

    class _FakeHttp:
        def fetch(self, url, timeout_s=30.0):
            return b""

    assert issubclass(HttpDataInstaller, DataInstaller)
    assert isinstance(HttpDataInstaller(http=_FakeHttp()), DataInstaller)


# =========================================================================
# WHO asks for an install — a guess must not fetch the wrong library
# =========================================================================


def test_discovery_installs_nothing_for_a_panel_it_cannot_identify(
    tmp_home: Path, monkeypatch,
) -> None:
    """``DiscoverDevices`` must not install for a resolution it GUESSED.

    It never handshakes, so the only resolution available to it is the static
    registry row.  For ``0416:5302`` that row is honest about not knowing --
    one USB id covers at least 240x320, 320x240 and 1280x480, and only the PM
    byte tells them apart.

    A guess is not inert.  While the row said ``(240, 320)``, discovery
    fetched theme240320 for a 1280x480 Trofeo Vision and the browser then
    pointed there, two minutes AFTER the handshake reported ``(1280, 480)``:
    "locked at 240x320" (#300, and the same shape in #244 / #257 / #267 /
    #268).  The panel's real data still arrives -- ``ConnectDevice`` installs
    for the HANDSHAKE resolution.

    Asserting on what was SUBMITTED, not on what landed on disk: the install
    itself is stubbed suite-wide, so a disk assertion would pass either way.
    """
    from trcc.app import App
    from trcc.core.commands import DiscoverDevices
    from trcc.core.models import DeviceInfo

    platform = FakePlatform(tmp_home)
    monkeypatch.setattr(
        platform, "scan_devices",
        lambda: [DeviceInfo(vid=0x0416, pid=0x5302)],
    )
    app = App(platform=platform)

    submitted: list[tuple] = []
    real = app.data_install_runner.submit
    monkeypatch.setattr(
        app.data_install_runner, "submit",
        lambda *a, **k: (submitted.append(a), real(*a, **k))[1],
    )

    result = app.dispatch(DiscoverDevices())

    assert result.ok is True
    assert [prod.key for prod in result.products] == ["0416:5302"], (
        "the device must still be DISCOVERED — only the guessed install goes"
    )
    assert submitted == [], (
        f"discovery installed for a guessed resolution: {submitted}. "
        f"0416:5302 spans 240x320 / 320x240 / 1280x480 and is identified by "
        f"its PM byte, which discovery never reads (#300)."
    )
