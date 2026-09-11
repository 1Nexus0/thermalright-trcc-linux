"""The static-background preference: which file gets wired for a video.

``PlayVideo`` is the funnel every video background goes through, so the choice
between the video and its still stand-in lives behind the ``ContentStore`` port
(``FileContentStore.still_for``) — a pure path decision, exercised here without
an App, a device, or the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from trcc.adapters.theme.filesystem import FileContentStore
from trcc.app import App
from trcc.core.commands import ConnectDevice, SetStaticBackground
from trcc.core.commands.theme import _cloud_asset
from trcc.services.media import MediaService, Playback


def _touch(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def _store() -> FileContentStore:
    return FileContentStore()


def test_still_prefers_the_png(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    png = _touch(tmp_path / "a001.png")
    _touch(tmp_path / "a001.jpg")
    assert _store().still_for(video) == png


def test_still_falls_back_to_jpg(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    jpg = _touch(tmp_path / "a001.jpg")
    assert _store().still_for(video) == jpg


def test_gif_is_not_a_still(tmp_path: Path) -> None:
    """``.gif`` is ANIMATED (models.MEDIA) — it would keep the loop busy."""
    video = _touch(tmp_path / "a001.mp4")
    _touch(tmp_path / "a001.gif")
    assert _store().still_for(video) is None


def test_no_still_at_all(tmp_path: Path) -> None:
    assert _store().still_for(_touch(tmp_path / "a001.mp4")) is None


def test_cloud_asset_default_keeps_the_video(tmp_path: Path) -> None:
    """No flag → the MP4, even when a still is sitting next to it."""
    video = _touch(tmp_path / "a001.mp4")
    _touch(tmp_path / "a001.png")
    assert _cloud_asset(_store(), video, static=False) == video


def test_cloud_asset_static_picks_the_still(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    png = _touch(tmp_path / "a001.png")
    assert _cloud_asset(_store(), video, static=True) == png


def test_cloud_asset_static_without_a_still_falls_back(tmp_path: Path) -> None:
    """ffmpeg can fail silently, so a missing still must not break the load."""
    video = _touch(tmp_path / "a001.mp4")
    assert _cloud_asset(_store(), video, static=True) == video


def test_video_for_finds_the_sibling_video(tmp_path: Path) -> None:
    png = _touch(tmp_path / "a001.png")
    video = _touch(tmp_path / "a001.mp4")
    assert _store().video_for(png) == video


def test_video_for_is_none_without_a_video(tmp_path: Path) -> None:
    png = _touch(tmp_path / "a001.png")
    _touch(tmp_path / "a001.gif")
    assert _store().video_for(png) is None


def test_set_static_background_flips_the_current_background(
    fake_platform, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch re-applies what is already on screen — video → still → video,
    with no theme reload in between (the round trip the toggle used to need)."""
    key = "0402:3922"
    app = App(platform=fake_platform)
    resp = bytearray(0xE100)
    resp[0] = 100
    app.platform.scsi.read_script.append(bytes(resp))  # type: ignore[attr-defined]
    assert app.dispatch(ConnectDevice(key=key)).ok

    def fake_load(self, device_key: str, path: Path,
                  size: tuple[int, int] | None, **kwargs):
        playback = Playback(
            frames=[b"\xff\xd8\xff\xe0" * 8] * 3, fps=kwargs.get("fps", 15),
        )
        self._playbacks[device_key] = playback
        return playback

    monkeypatch.setattr(MediaService, "load_video", fake_load)

    video = tmp_home / "a001.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    still = tmp_home / "a001.png"
    QImage(8, 8, QImage.Format.Format_RGB888).save(str(still), "PNG")
    app.settings.set_background_path(key, str(video))

    app.dispatch(SetStaticBackground(key=key, enabled=True))
    assert app.settings.for_device(key).background_path == str(still)

    app.dispatch(SetStaticBackground(key=key, enabled=False))
    assert app.settings.for_device(key).background_path == str(video)
