"""Truth table for :func:`trcc.core.geometry.plan_orientation`.

Phase A of the folder-switch geometry restore (#136): pins the compose
canvas + portrait flag + whole-composite rotation for every panel class ×
angle × content-orientation.  This is a faithful extraction of
``DisplayService._compose_geometry`` — the table here is the contract Phase B
must preserve byte-for-byte when it routes the render path through this module.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trcc.core.geometry import (
    OrientationPlan,
    content_is_portrait,
    lock_region_to_panel,
    plan_orientation,
)
from trcc.core.models import Theme
from trcc.core.protocol import FBL_PROFILES, DeviceProfile

ANGLES = (0, 90, 180, 270)

# Representative panels, one per structural class.
_SQUARE = DeviceProfile(360, 360, jpeg=True)                       # non-rotate square
_NON_ROTATE = DeviceProfile(320, 320, big_endian=True)            # square, rotate=False
_SMALL_RGB565 = DeviceProfile(320, 240, rotate=True)             # small rotate panel
_SMALL_JPEG = DeviceProfile(320, 240, jpeg=True, rotate=True)    # Mjolnir — still small
_WIDE_JPEG = DeviceProfile(854, 480, jpeg=True, rotate=True, widescreen=True)


# ── Explicit, hand-derived expectations ─────────────────────────────────────
# (label, profile, orientation, content_is_portrait, expected OrientationPlan)
CASES = [
    # Square / non-rotate: else-branch always — canvas = oriented(native),
    # portrait=False, post_rotate=0.  A square swaps to itself.
    ("square@0",   _SQUARE,     0,   False, OrientationPlan((360, 360), False, 0)),
    ("square@90",  _SQUARE,     90,  False, OrientationPlan((360, 360), False, 0)),
    ("square@90p", _SQUARE,     90,  True,  OrientationPlan((360, 360), False, 0)),
    ("nonrot@90",  _NON_ROTATE, 90,  False, OrientationPlan((320, 320), False, 0)),

    # Small rotate RGB565 — landscape content.
    ("s565@0",     _SMALL_RGB565, 0,   False, OrientationPlan((320, 240), False, 0)),
    ("s565@180",   _SMALL_RGB565, 180, False, OrientationPlan((320, 240), False, 0)),
    # 90/270 landscape-only → LANDSCAPE canvas + whole-composite spin (fallback).
    ("s565@90L",   _SMALL_RGB565, 90,  False, OrientationPlan((320, 240), False, 90)),
    ("s565@270L",  _SMALL_RGB565, 270, False, OrientationPlan((320, 240), False, 270)),
    # Portrait content → PORTRAIT canvas, composed UPRIGHT (post_rotate=0 at
    # every angle) — the same model as the widescreen panels below.  The WIRE
    # owns all rotation via wire_angle (= base − orientation): a base-90 RGB565
    # panel gets 0 @90 / 180 @270 (net-identical to the old post_rotate=180),
    # while a base-0 panel gets 270 @90 / 90 @270 (transposes the portrait canvas
    # to the device's landscape buffer — the #234 640×480 squeeze fix).  Keeping
    # a post_rotate here would double-rotate on top of the wire angle.
    ("s565@90P",   _SMALL_RGB565, 90,  True,  OrientationPlan((240, 320), True, 0)),
    ("s565@270P",  _SMALL_RGB565, 270, True,  OrientationPlan((240, 320), True, 0)),

    # Small rotate JPEG (Mjolnir) behaves identically — rotate, not widescreen.
    ("sjpg@90L",   _SMALL_JPEG, 90,  False, OrientationPlan((320, 240), False, 90)),
    ("sjpg@90P",   _SMALL_JPEG, 90,  True,  OrientationPlan((240, 320), True, 0)),
    ("sjpg@270P",  _SMALL_JPEG, 270, True,  OrientationPlan((240, 320), True, 0)),

    # Widescreen JPEG (#169/#203) — always composes portrait at 90/270 (rides the
    # widescreen rotate_panel branch), regardless of content flag, with
    # post_rotate=0.  The WIRE rotation is owned entirely by resolve_encode_angle
    # (via wire_angle), which the C# ImageToJpg applies to the whole composite at
    # every angle — a post_rotate here would double-rotate on top of it (#169).
    # 0/180 = oriented landscape.
    ("wide@0",     _WIDE_JPEG, 0,   False, OrientationPlan((854, 480), False, 0)),
    ("wide@180",   _WIDE_JPEG, 180, False, OrientationPlan((854, 480), False, 0)),
    ("wide@90L",   _WIDE_JPEG, 90,  False, OrientationPlan((480, 854), True, 0)),
    ("wide@90P",   _WIDE_JPEG, 90,  True,  OrientationPlan((480, 854), True, 0)),
    ("wide@270L",  _WIDE_JPEG, 270, False, OrientationPlan((480, 854), True, 0)),
    ("wide@270P",  _WIDE_JPEG, 270, True,  OrientationPlan((480, 854), True, 0)),
]


@pytest.mark.parametrize(
    "profile,orientation,content_portrait,expected",
    [(p, o, c, e) for _, p, o, c, e in CASES],
    ids=[label for label, *_ in CASES],
)
def test_plan_orientation_truth_table(
    profile: DeviceProfile, orientation: int,
    content_portrait: bool, expected: OrientationPlan,
) -> None:
    assert plan_orientation(profile, orientation, content_portrait) == expected


@pytest.mark.parametrize("fbl", sorted(FBL_PROFILES))
@pytest.mark.parametrize("orientation", ANGLES)
@pytest.mark.parametrize("content_portrait", [True, False])
def test_plan_invariants_over_every_profile(
    fbl: int, orientation: int, content_portrait: bool,
) -> None:
    """Structural invariants that must hold for every real FBL profile."""
    profile = FBL_PROFILES[fbl]
    plan = plan_orientation(profile, orientation, content_portrait)

    # Canvas is always a permutation of the native resolution (area preserved).
    w, h = profile.resolution
    assert plan.canvas in {(w, h), (h, w)}
    assert plan.canvas[0] * plan.canvas[1] == w * h

    # post_rotate is 0, the raw orientation (landscape-fallback spin), or 180
    # (non-widescreen portrait content at 270 — the dimension-preserving flip).
    # Widescreen panels take post_rotate=0 at every angle: their whole-composite
    # rotation is owned by resolve_encode_angle at wire time (#169), never here.
    assert plan.post_rotate in {0, orientation, 180}

    # Any non-zero spin is a rotate panel at 90/270.
    if plan.post_rotate:
        assert profile.rotate and w != h
        assert orientation in (90, 270)
        if plan.post_rotate == 180:
            # The 270 flip on non-widescreen portrait-composed content (widescreen
            # never reaches here — it composes portrait with post_rotate=0).
            assert orientation == 270
            assert not profile.widescreen
            assert plan.canvas == (h, w)
            assert plan.is_portrait_content is True
        else:
            # Landscape-only fallback: composed on the landscape canvas, spun
            # whole.  Non-widescreen only — widescreen always composes portrait.
            assert plan.post_rotate == orientation
            assert not content_portrait
            assert not profile.widescreen
            assert plan.canvas == (w, h)
            assert plan.is_portrait_content is False


def test_landscape_angles_never_spin() -> None:
    """0/180 never produce a whole-composite spin on any panel."""
    for fbl, profile in FBL_PROFILES.items():
        for orientation in (0, 180):
            for content_portrait in (True, False):
                plan = plan_orientation(profile, orientation, content_portrait)
                assert plan.post_rotate == 0, f"fbl={fbl} @{orientation}"


# ── content_is_portrait — the shared parent-folder predicate ────────────────
# A non-square rotate panel with native (320, 240): its portrait catalogs are
# theme240320 / zt240320.  Three OR-signals + the square/non-rotate guard.

_ROTATE = DeviceProfile(320, 240, rotate=True)
_SQUARE = DeviceProfile(320, 320, big_endian=True)


def _theme(path: str, rotation: int = 0) -> Theme:
    return Theme(path=Path(path), name="t", resolution=(320, 240),
                 config={"rotation": rotation})


def test_content_portrait_active_mask_wins_over_landscape_theme() -> None:
    # Portrait mask (web/zt240320) over a landscape base theme → portrait.
    t = _theme("/x/theme320240/T", rotation=0)
    assert content_is_portrait(t, _ROTATE, "/x/web/zt240320/000d/01.png", True)


def test_content_portrait_mask_ignored_when_hidden() -> None:
    t = _theme("/x/theme320240/T", rotation=0)
    assert not content_is_portrait(t, _ROTATE, "/x/web/zt240320/000d/01.png", False)


def test_content_portrait_landscape_mask_is_not_portrait() -> None:
    t = _theme("/x/theme320240/T", rotation=0)
    assert not content_is_portrait(t, _ROTATE, "/x/web/zt320240/000d/01.png", True)


def test_content_portrait_theme_folder_signal_beats_lying_dc() -> None:
    # Shipped-bug case: portrait folder, landscape DC (rotation=0) → still portrait.
    t = _theme("/x/theme240320/T", rotation=0)
    assert content_is_portrait(t, _ROTATE, None, False)


def test_content_portrait_dc_rotation_signal_kept() -> None:
    t = _theme("/x/anywhere/T", rotation=90)
    assert content_is_portrait(t, _ROTATE, None, False)


def test_content_portrait_all_signals_false_is_landscape() -> None:
    t = _theme("/x/theme320240/T", rotation=0)
    assert not content_is_portrait(t, _ROTATE, None, False)


def test_content_portrait_square_never_portrait() -> None:
    # Every signal points portrait, but a square panel never composes portrait.
    t = _theme("/x/theme240320/T", rotation=90)
    assert not content_is_portrait(t, _SQUARE, "/x/web/zt320320/m/01.png", True)


# =========================================================================
# lock_region_to_panel — TASK 4: the fit constraint, made universal
# =========================================================================


def test_a_dragged_region_takes_the_panel_shape() -> None:
    """854x480 is 0.562 high per wide, so 200 across is 112 down.

    Anchored to the number the gui panel's own comment records: on an 854x480
    panel a width of 201 gives 113, and 200 gives 112.  The ratio is
    height/width and height follows WIDTH — the width the user dragged is the
    intent, so the gesture's horizontal extent is what survives.
    """
    assert lock_region_to_panel((854, 480), 10, 20, 200, 999) == (10, 20, 200, 112)


def test_a_square_panel_locks_too() -> None:
    """The old hardcoded table skipped squares behind a ``ratio != 1.0`` guard,
    so a 320x320 device never locked at all."""
    assert lock_region_to_panel((320, 320), 0, 0, 200, 999) == (0, 0, 200, 200)


def test_the_panel_the_old_table_forgot() -> None:
    """640x172 was missing from the tabulated ratios entirely and fell back to
    0.75 — 2.8x wrong.  Derived, it is 0.269."""
    assert lock_region_to_panel((640, 172), 0, 0, 200, 999) == (0, 0, 200, 54)


def test_an_unknown_panel_leaves_the_region_alone() -> None:
    """``None`` means "nobody has told us which panel yet".

    Constraining to a panel we have not met would silently shrink the user's
    region to a guess.
    """
    assert lock_region_to_panel(None, 10, 20, 200, 999) == (10, 20, 200, 999)


@pytest.mark.parametrize("bad", [(0, 480), (854, 0), (-1, -1)])
def test_a_degenerate_panel_leaves_the_region_alone(bad) -> None:
    """A zero or negative dimension must not divide by zero or invert."""
    assert lock_region_to_panel(bad, 5, 6, 100, 200) == (5, 6, 100, 200)


def test_every_panel_in_the_catalog_gives_a_usable_height() -> None:
    """Parametrized over the REAL catalog, not invented sizes.

    ``FBL_PROFILES`` is the single source of truth for panel geometry, so a
    panel added there cannot silently produce a zero-height region.
    """
    from trcc.core.protocol import FBL_PROFILES

    seen = 0
    for profile in FBL_PROFILES.values():
        res = getattr(profile, "resolution", None)
        if not res or res[0] <= 0 or res[1] <= 0:
            continue
        seen += 1
        _, _, w, h = lock_region_to_panel(res, 0, 0, 200, 999)
        assert w == 200, "the dragged width is the intent and must survive"
        assert h >= 1, f"{res} produced a zero-height region"
    assert seen > 5, f"only {seen} panels checked — is FBL_PROFILES wired?"


def test_a_narrow_drag_on_a_wide_panel_never_collapses_to_zero() -> None:
    """A 640x172 panel is 0.269 high per wide, so a 1px drag rounds to 0.

    A zero-height region is not a small capture, it is a capture of nothing —
    and it reaches the wire as a degenerate rectangle.  The ``max(1, ...)``
    floor exists for this, and the catalog sweep at width 200 never reaches
    it, so nothing tested it until a mutation removed the floor and every
    test stayed green.
    """
    for width in (1, 2, 3):
        _, _, w, h = lock_region_to_panel((640, 172), 0, 0, width, 999)
        assert w == width
        assert h >= 1, f"a {width}px drag collapsed to {h}px high"
