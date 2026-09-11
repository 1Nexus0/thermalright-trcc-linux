"""The static-background preference: which file gets wired for a video.

``PlayVideo`` is the funnel every video background goes through, so the choice
between the video and its still stand-in lives in ``models.still_for`` — a pure
path decision, exercised here without an App, a device, or the network.
"""
from __future__ import annotations

from pathlib import Path

from trcc.core.commands.theme import _cloud_asset
from trcc.core.models import still_for


def _touch(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def test_still_prefers_the_png(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    png = _touch(tmp_path / "a001.png")
    _touch(tmp_path / "a001.jpg")
    assert still_for(video) == png


def test_still_falls_back_to_jpg(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    jpg = _touch(tmp_path / "a001.jpg")
    assert still_for(video) == jpg


def test_gif_is_not_a_still(tmp_path: Path) -> None:
    """``.gif`` is ANIMATED (models.MEDIA) — it would keep the loop busy."""
    video = _touch(tmp_path / "a001.mp4")
    _touch(tmp_path / "a001.gif")
    assert still_for(video) is None


def test_no_still_at_all(tmp_path: Path) -> None:
    assert still_for(_touch(tmp_path / "a001.mp4")) is None


def test_cloud_asset_default_keeps_the_video(tmp_path: Path) -> None:
    """No flag → the MP4, even when a still is sitting next to it."""
    video = _touch(tmp_path / "a001.mp4")
    _touch(tmp_path / "a001.png")
    assert _cloud_asset(video, static=False) == video


def test_cloud_asset_static_picks_the_still(tmp_path: Path) -> None:
    video = _touch(tmp_path / "a001.mp4")
    png = _touch(tmp_path / "a001.png")
    assert _cloud_asset(video, static=True) == png


def test_cloud_asset_static_without_a_still_falls_back(tmp_path: Path) -> None:
    """ffmpeg can fail silently, so a missing still must not break the load."""
    video = _touch(tmp_path / "a001.mp4")
    assert _cloud_asset(video, static=True) == video
