"""Tests for services/charts font configuration (Phase 4.4).

Phase 4.4 pins ``font.family`` to ``DejaVu Sans`` so Filipino names ("Mabuhay",
"Kahiran ng Lambat") + accented European settlement names ("Tierra del Cabo",
"Bazariskes", "Florraine") render correctly on every host — not just hosts
whose default matplotlib font happens to cover those glyphs.

These tests verify:
  * The font family is set explicitly (no host-dependent default).
  * The font actually has the glyphs we care about (ñ, ü, á, é, í, ó, ú).
  * A chart rendered with those glyphs produces non-empty PNG bytes.
"""

import services.charts as charts

# The glyphs that matter for Lambat: Filipino names use the Latin alphabet
# + ñ + ü; European settlement names add á é í ó ú. DejaVu Sans covers all of
# these (it's a Latin-1 + Latin Extended-A font), but the test makes that
# guarantee explicit so a future font swap can't silently regress it.
LAMBAT_GLYPHS = "ñüáéíóúÑÜÁÉÍÓÚ"


def test_chart_font_family_is_explicitly_set():
    """_apply_dark_style must set font.family (not rely on matplotlib default)."""
    charts._apply_dark_style()
    import matplotlib.pyplot as plt

    family = plt.rcParams.get("font.family")
    # rcParams stores font.family as a list; the first element is the
    # resolved family name.
    if isinstance(family, list):
        assert charts.CHART_FONT_FAMILY in family or family[0] == charts.CHART_FONT_FAMILY
    else:
        assert family == charts.CHART_FONT_FAMILY


def test_chart_font_family_is_dejavu_sans():
    """The pinned font must be DejaVu Sans (ships with matplotlib, full coverage)."""
    assert charts.CHART_FONT_FAMILY == "DejaVu Sans"


def test_chart_font_has_lambat_glyphs():
    """DejaVu Sans must contain every glyph used by Lambat names.

    This is the core guarantee of Phase 4.4: no host-dependent tofu boxes for
    ñ / ü / accented vowels. If this fails, either matplotlib's bundled
    DejaVu Sans was removed (very unlikely) or someone changed the font to
    one that doesn't cover Latin Extended-A.
    """
    from matplotlib.font_manager import FontProperties, findfont

    charts._apply_dark_style()
    fp = FontProperties(family=charts.CHART_FONT_FAMILY)
    font_path = findfont(fp, fallback_to_default=False)
    # The font file must resolve to a real DejaVu Sans file.
    assert "DejaVuSans" in font_path, f"resolved font is not DejaVu Sans: {font_path}"

    # Verify the font has every needed glyph via the freetype backend.
    # FT2Font.get_char_index returns 0 (the .notdef glyph) when the char is
    # missing from the font.
    from matplotlib.ft2font import FT2Font

    font = FT2Font(font_path)
    for ch in LAMBAT_GLYPHS:
        idx = font.get_char_index(ord(ch))
        assert idx != 0, f"font {font_path!r} missing glyph {ch!r} (index=0 = .notdef)"


def test_render_activity_series_with_lambat_glyphs_produces_png():
    """A chart whose title contains ñ/ü must render to non-empty PNG bytes.

    Regression guard: if the font is ever swapped to one without these glyphs,
    matplotlib emits a 'Glyph missing from current font' warning but still
    returns bytes (with tofu boxes). We assert the bytes are non-empty AND
    that no missing-glyph warning is emitted.
    """
    import warnings
    from datetime import datetime

    charts._apply_dark_style()
    dates = [datetime(2025, 1, 1), datetime(2025, 2, 1), datetime(2025, 3, 1)]
    totals = [10, 12, 15]
    title = "Florraine — Mabuhay & Tierra del Cabo (ñüáé)"

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        png = charts.render_activity_series(title=title, dates=dates, totals=totals)

    assert png is not None
    assert len(png) > 1000  # a real PNG, not an empty buffer

    # Filter for the specific missing-glyph warning. matplotlib emits a
    # UserWarning with "missing from current font" in the message when a
    # glyph can't be rendered.
    missing = [
        str(w.message) for w in record if "missing from current font" in str(w.message).lower()
    ]
    assert not missing, f"chart has missing glyphs: {missing}"
