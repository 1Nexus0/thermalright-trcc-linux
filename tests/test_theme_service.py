"""FileContentStore — JSON-first, DC fallback, auto-migrate."""
from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

import pytest

from trcc.adapters.theme.filesystem import FileContentStore
from trcc.core.errors import ThemeError

from .test_dc_reader import _build_dc


def test_raises_on_missing_dir(tmp_path: Path) -> None:
    svc = FileContentStore()
    with pytest.raises(ThemeError, match="does not exist"):
        svc.load(tmp_path / "nonexistent")


def test_raises_on_file_path(tmp_path: Path) -> None:
    svc = FileContentStore()
    (tmp_path / "afile").write_text("oops")
    with pytest.raises(ThemeError, match="not a directory"):
        svc.load(tmp_path / "afile")


def test_raises_on_dir_without_config(tmp_path: Path) -> None:
    svc = FileContentStore()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ThemeError, match="No trcc.json or config1.dc"):
        svc.load(empty)


def test_loads_json_theme(tmp_path: Path) -> None:
    theme = tmp_path / "ThemeA"
    theme.mkdir()
    (theme / "trcc.json").write_text(json.dumps({
        "name": "JSON Theme",
        "overlay_enabled": True,
        "elements": [],
    }), encoding="utf-8")

    svc = FileContentStore()
    t = svc.load(theme)

    assert t.name == "JSON Theme"
    assert t.config["overlay_enabled"] is True


def test_dc_is_read_without_writing_a_json_beside_it(tmp_path: Path) -> None:
    """A DC theme loads from its DC and leaves the directory as it found it.

    The old behaviour wrote a derived ``trcc.json`` on first load so later
    loads skipped the binary path.  That bought 2.5 ms across a 120-theme
    listing and cost correctness: the derived copy became the authority, so
    every later fix to the DC codec stopped at the themes already converted.
    """
    theme = tmp_path / "DcTheme"
    theme.mkdir()
    (theme / "config1.dc").write_bytes(_build_dc())

    svc = FileContentStore()
    t = svc.load(theme)

    assert t.name == "DcTheme"
    assert not (theme / "trcc.json").exists(), (
        "loading a DC theme must not write a derived trcc.json beside it"
    )
    assert sorted(p.name for p in theme.iterdir()) == ["config1.dc"]


def test_dc_wins_over_a_derived_json_and_the_artifact_goes(
    tmp_path: Path,
) -> None:
    """Both files present → the DC is the config, the JSON is removed.

    A directory can only carry both because the old migration put them there:
    ``SaveTheme`` stages a clean dir and writes no DC, and ``export`` bundles
    no DC so neither does ``import_``.  Measured on a real install: 125 such
    pairs, ZERO authored.  The JSON here claims a name and an overlay flag
    that BOTH differ from the DC, so serving the stale copy cannot pass.
    """
    theme = tmp_path / "Both"
    theme.mkdir()
    (theme / "trcc.json").write_text(json.dumps({
        "name": "StaleCopy", "elements": [], "overlay_enabled": False,
    }), encoding="utf-8")
    (theme / "config1.dc").write_bytes(_build_dc())

    t = FileContentStore().load(theme)

    assert t.name == "Both", "the stale JSON was served instead of the DC"
    assert t.config["overlay_enabled"] is True, (
        "overlay_enabled came from the stale JSON, not from the DC"
    )
    assert not (theme / "trcc.json").exists(), (
        "the derived artifact survived, so it will be served again"
    )


def test_an_authored_manifest_alone_is_still_the_config(
    tmp_path: Path,
) -> None:
    """No DC in the directory → the manifest is the theme, untouched.

    This is every theme we author — ``SaveTheme`` and ``import_`` both produce
    a manifest and no DC.  Nothing about the rule above may reach them.
    """
    theme = tmp_path / "Authored"
    theme.mkdir()
    (theme / "trcc.json").write_text(json.dumps({
        "name": "Authored", "width": 320, "height": 320,
        "elements": [], "overlay_enabled": False,
    }), encoding="utf-8")

    t = FileContentStore().load(theme)

    assert t.name == "Authored"
    assert t.config["overlay_enabled"] is False
    assert (theme / "trcc.json").exists()


def test_removing_an_authored_manifest_beside_a_dc_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """``width`` marks a manifest somebody MEANT — removing one says so loudly.

    Only a DC parse's output should ever be found beside a DC.  ``width`` is
    written unconditionally by the sole authored writer and a DC parse cannot
    produce it, so it is the one reliable mark (verified across a real
    install: 9 of 9 authored manifests recognised, 0 misclassified).  A silent
    removal here would let a dev seeder writing into a shipped theme directory
    lose its fixture and never find out.
    """
    theme = tmp_path / "Hybrid"
    theme.mkdir()
    (theme / "trcc.json").write_text(json.dumps({
        "name": "Hybrid", "width": 854, "height": 480, "elements": [],
    }), encoding="utf-8")
    (theme / "config1.dc").write_bytes(_build_dc())

    with caplog.at_level(logging.WARNING):
        FileContentStore().load(theme)

    assert any("carried 'width'" in r.message for r in caplog.records), (
        "an authored manifest was removed without a warning"
    )


def test_list_finds_both_formats(tmp_path: Path) -> None:
    json_t = tmp_path / "A"
    json_t.mkdir()
    (json_t / "trcc.json").write_text('{"elements": []}')
    dc_t = tmp_path / "B"
    dc_t.mkdir()
    (dc_t / "config1.dc").write_bytes(_build_dc())
    broken = tmp_path / "C"
    broken.mkdir()    # no config — skipped silently

    svc = FileContentStore()
    themes = svc.list(tmp_path)

    names = {t.name for t in themes}
    assert "A" in names
    assert "B" in names
    assert "C" not in names


def test_background_path_finds_00_png(tmp_path: Path) -> None:
    """Legacy / cloud convention: 00.png IS the rendered background.
    Theme.png is the panel thumbnail and must NEVER be a render target."""
    theme = tmp_path / "Legacy"
    theme.mkdir()
    (theme / "trcc.json").write_text('{"elements": []}')
    (theme / "00.png").write_bytes(b"\x89PNG\r\n\x1a\n")       # render target
    (theme / "Theme.png").write_bytes(b"\x89PNG\r\n\x1a\n")    # thumbnail only

    svc = FileContentStore()
    t = svc.load(theme)

    assert svc.background_path(t) == theme / "00.png"
    assert svc.preview_path(t) == theme / "Theme.png"


def test_background_path_returns_none_when_only_thumbnail(
    tmp_path: Path,
) -> None:
    """Theme.png alone is not enough — rendering the thumbnail would
    ship preview-only artwork to the device."""
    theme = tmp_path / "ThumbOnly"
    theme.mkdir()
    (theme / "trcc.json").write_text('{"elements": []}')
    (theme / "Theme.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    svc = FileContentStore()
    t = svc.load(theme)

    assert svc.background_path(t) is None


def test_loads_pre_cutover_filename(tmp_path: Path) -> None:
    """Themes written by pre-cutover next/ (``trcc.json``) still
    load — the rename to ``trcc.json`` doesn't strand existing themes."""
    theme = tmp_path / "OldName"
    theme.mkdir()
    (theme / "trcc.json").write_text(json.dumps({
        "name": "Pre-cutover", "elements": [], "overlay_enabled": True,
    }), encoding="utf-8")

    svc = FileContentStore()
    t = svc.load(theme)

    assert t.name == "Pre-cutover"
    # Old file is left in place — rollback safety, no silent deletion.
    assert (theme / "trcc.json").exists()


def test_loads_legacy_config_json(tmp_path: Path) -> None:
    """Themes saved by legacy Windows/Linux TRCC use ``config.json``
    with a dict-of-elements shape under ``dc``.  Loading translates
    each entry into next/'s element list shape."""
    theme = tmp_path / "Custom_Legacy"
    theme.mkdir()
    (theme / "config.json").write_text(json.dumps({
        "background": str(theme / "Theme.png"),
        "mask": "",
        "dc": {
            "time": {
                "x": 50, "y": 100, "color": "#80ffff",
                "font": {"size": 32, "name": "DejaVu Sans", "style": "bold"},
                "enabled": True, "metric": "time", "time_format": 0,
            },
            "cpu:temp": {
                "x": 120, "y": 200, "color": "#ffffff",
                "font": {"size": 24, "name": "DejaVu Sans", "style": "regular"},
                "enabled": True, "metric": "cpu:temp", "mode_sub": 0,
            },
            "off_field": {
                "x": 0, "y": 0, "color": "#000", "font": {"size": 12},
                "enabled": False, "metric": "weekday",
            },
        },
    }), encoding="utf-8")

    svc = FileContentStore()
    t = svc.load(theme)

    assert t.name == "Custom_Legacy"
    elements = t.config["elements"]
    # 2 enabled, 1 disabled → 2 elements
    assert len(elements) == 2
    by_marker = {
        (e.get("type"), e.get("source") or e.get("metric")): e for e in elements
    }
    time_el = by_marker[("clock", "time")]
    assert time_el["x"] == 50
    assert time_el["y"] == 100
    assert time_el["color"] == "#80ffff"
    assert time_el["size"] == 32
    assert time_el["bold"] is True
    assert time_el["name"] == "DejaVu Sans"
    cpu_el = by_marker[("metric", "cpu:temp")]
    assert cpu_el["bold"] is False


def test_list_finds_legacy_config_json_themes(tmp_path: Path) -> None:
    """``list()`` discovers themes with legacy ``config.json``."""
    theme = tmp_path / "LegacyOne"
    theme.mkdir()
    (theme / "config.json").write_text('{"dc": {}}', encoding="utf-8")

    themes = FileContentStore().list(tmp_path)

    assert {t.name for t in themes} == {"LegacyOne"}


def test_list_finds_pre_cutover_themes(tmp_path: Path) -> None:
    """``list()`` recognises pre-cutover ``trcc.json`` as a marker."""
    theme = tmp_path / "OldStill"
    theme.mkdir()
    (theme / "trcc.json").write_text('{"elements": []}', encoding="utf-8")

    themes = FileContentStore().list(tmp_path)

    assert {t.name for t in themes} == {"OldStill"}


def test_background_path_prefers_video_over_static(tmp_path: Path) -> None:
    """When both Theme.mp4 (video) and 00.png (static) exist, the video
    is the background — matches legacy ``td.video or td.bg`` preference."""
    theme = tmp_path / "Both"
    theme.mkdir()
    (theme / "trcc.json").write_text('{"elements": []}')
    (theme / "00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (theme / "Theme.mp4").write_bytes(b"\x00" * 16)

    svc = FileContentStore()
    t = svc.load(theme)

    assert svc.background_path(t) == theme / "Theme.mp4"


# ── list_web_previews — cloud-theme preview enumeration ──────────────


def test_list_web_previews_enumerates_pngs(tmp_path: Path) -> None:
    """One entry per <id>.png; category = first letter; has_video from .mp4."""
    web = tmp_path / "web" / "320320"
    web.mkdir(parents=True)
    (web / "a001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (web / "a001.mp4").write_bytes(b"v")          # → has_video
    (web / "b002.png").write_bytes(b"\x89PNG\r\n\x1a\n")   # no video

    previews = {p.id: p for p in FileContentStore().list_web_previews(web)}

    assert set(previews) == {"a001", "b002"}
    assert previews["a001"].category == "a"
    assert previews["a001"].has_video is True
    assert previews["b002"].has_video is False


def test_list_web_previews_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert FileContentStore().list_web_previews(tmp_path / "nope") == []


# Keep the unused `struct` import alive even if tests don't use it directly —
# it's there for future parametrization of binary DC buffers.
_ = struct


# ── Per-frame logging — the report tail is the diagnostic instrument ──


def _reference_theme(tmp_path: Path) -> tuple[FileContentStore, Path]:
    """A theme whose config names a library mask, as cloud themes do."""
    from .conftest import FakePaths

    paths = FakePaths(tmp_path)
    mask_dir = paths.data_dir() / "web" / "zt1600720" / "000a"
    mask_dir.mkdir(parents=True)
    (mask_dir / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    theme_dir = tmp_path / "Theme1"
    theme_dir.mkdir()
    (theme_dir / "config.json").write_text(json.dumps({
        "mask": "web/zt1600720/000a", "elements": [],
    }))
    return FileContentStore(paths), theme_dir


def test_resolving_a_referenced_mask_does_not_log_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """``mask_path`` sits on the per-frame render path — ``_resolve_mask_source``
    calls it for every frame composed — so an INFO line here floods the log we
    ask reporters to send us.  One report (#264) carried ten identical
    ``mask_path`` INFO lines inside a single second, which is how a capped tail
    ends up all frames and no user actions.

    MUTATION CHECK: put the log.info back and this fails with 1 != 0.
    """
    svc, theme_dir = _reference_theme(tmp_path)
    theme = svc.load(theme_dir)

    with caplog.at_level(logging.DEBUG, logger="trcc.adapters.theme.filesystem"):
        resolved = svc.mask_path(theme)

    assert resolved is not None and resolved.name == "01.png"   # still resolves
    info_lines = [r for r in caplog.records
                  if r.levelno >= logging.INFO and "mask_path" in r.message]
    assert info_lines == []


def test_no_producer_writes_both_a_dc_and_a_manifest(tmp_path: Path) -> None:
    """THE INVARIANT the discard rule rests on.

    ``_load_config`` treats "both files present" as proof the JSON is derived,
    and that is only sound while nothing else can produce the pair.  Two
    producers could: ``export`` (whose archive ``import_`` unpacks verbatim)
    and ``SaveTheme``\'s staging dir.  Neither may include a ``config1.dc``.

    Asserted on the EXPORT MEMBERS rather than on a saved directory so the
    check does not need a device: the archive is what ``import_`` writes, so
    a DC appearing here is a DC appearing on disk.
    """
    theme_dir = tmp_path / "Src"
    theme_dir.mkdir()
    (theme_dir / "config1.dc").write_bytes(_build_dc())
    (theme_dir / "00.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    store = FileContentStore()
    theme = store.load(theme_dir)
    members = store._export_members(theme, theme_dir)

    assert "trcc.json" in members, "an export must carry its manifest"
    assert "config1.dc" not in members, (
        "an exported theme carries a DC — import_ would then unpack a "
        "directory holding BOTH, and _load_config would delete the manifest "
        "it was supposed to honour"
    )
