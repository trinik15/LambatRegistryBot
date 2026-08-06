"""Tests for core/i18n.tr (Phase 4.5).

Covers the lazy-load, fallback chain, interpolation, and the test-only reload
helper. Locale files live in ``locales/{en,fil}.json`` at the repo root.
"""

import json

import pytest

from core import i18n
from core.config import Config


@pytest.fixture(autouse=True)
def _reload_locales_each_test():
    """Force i18n to re-read locale files before every test.

    Without this, the in-process cache means edits to _supported (or a
    Config.LOCALE change) wouldn't be seen by subsequent tests.
    """
    i18n.reload_for_tests()
    yield
    i18n.reload_for_tests()


# ---------------------------------------------------------------------------
# available_langs — both en + fil ship with the bot.
# ---------------------------------------------------------------------------


def test_available_langs_includes_en_and_fil():
    langs = i18n.available_langs()
    assert "en" in langs
    assert "fil" in langs


def test_available_langs_puts_en_first():
    # 'en' is the fallback, so it should always be first for deterministic
    # /help and config dumps.
    langs = i18n.available_langs()
    assert langs[0] == "en"


# ---------------------------------------------------------------------------
# tr — basic lookup + interpolation.
# ---------------------------------------------------------------------------


def test_tr_returns_english_value_by_default():
    assert i18n.tr("help.title") == "Lambat National Registry"


def test_tr_interpolates_named_fields():
    result = i18n.tr("monthly.gain", new_citizens=7)
    assert "+7 new citizens" in result


def test_tr_handles_emoji_placeholder():
    # The province section header takes an {emoji} field.
    result = i18n.tr("monthly.section.province_total", emoji="🌾")
    assert result.startswith("**🌾 POPULATION PER PROVINCE")


# ---------------------------------------------------------------------------
# Fallback chain — missing key in requested lang falls back to en.
# ---------------------------------------------------------------------------


def test_tr_falls_back_to_en_when_key_missing_in_fil():
    # 'monthly.title' is only in en.json, not fil.json. Asking for fil should
    # still return the English template (with the month_name interpolated).
    result = i18n.tr("monthly.title", lang="fil", month_name="Feb 2026")
    assert "Feb 2026" in result
    assert "Lambat's Census of Population" in result


def test_tr_returns_key_when_missing_in_all_locales():
    # A genuinely-unknown key returns the key itself so the typo is visible.
    assert i18n.tr("totally.made.up.key") == "totally.made.up.key"


def test_tr_returns_key_for_unknown_lang():
    # Asking for a lang that isn't loaded should fall back to en.
    result = i18n.tr("help.title", lang="xx")
    assert result == "Lambat National Registry"


# ---------------------------------------------------------------------------
# Filipino translation — the stretch-goal locale.
# ---------------------------------------------------------------------------


def test_tr_returns_filipino_value_when_lang_is_fil():
    result = i18n.tr("help.title", lang="fil")
    assert result == "Pambansang Talaan ng Lambat"


# ---------------------------------------------------------------------------
# Config.LOCALE — the default lang is read from Config.
# ---------------------------------------------------------------------------


def test_tr_uses_config_locale_when_no_lang_passed(monkeypatch):
    monkeypatch.setattr(Config, "LOCALE", "fil")
    i18n.reload_for_tests()
    result = i18n.tr("help.title")
    assert result == "Pambansang Talaan ng Lambat"


# ---------------------------------------------------------------------------
# Format-error resilience — a template with a missing field shouldn't raise.
# ---------------------------------------------------------------------------


def test_tr_returns_template_on_format_error(monkeypatch):
    # Inject a template with a field that won't be supplied.
    i18n._load()
    i18n._supported["en"]["test.bad_template"] = "Hello {missing_field}"
    try:
        # Calling without the missing_field kwarg should not raise; it
        # returns the unformatted template.
        result = i18n.tr("test.bad_template")
        assert "missing_field" in result
    finally:
        i18n._supported["en"].pop("test.bad_template", None)


# ---------------------------------------------------------------------------
# No-kwargs fast path — tr(key) without kwargs returns the raw template.
# ---------------------------------------------------------------------------


def test_tr_no_kwargs_returns_raw_template():
    # monthly.title has a {month_name} field, but without kwargs we skip
    # .format() and return the template literally.
    result = i18n.tr("monthly.title")
    assert "{month_name}" in result


# ---------------------------------------------------------------------------
# Locale file validity — both JSON files parse + are flat str→str mappings.
# ---------------------------------------------------------------------------


def test_locale_files_are_valid_flat_dicts():
    from pathlib import Path

    locales_dir = Path(__file__).resolve().parent.parent / "locales"
    for path in locales_dir.glob("*.json"):
        table = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(table, dict), f"{path.name} is not a JSON object"
        for k, v in table.items():
            assert isinstance(k, str), f"{path.name} has a non-string key"
            assert isinstance(v, str), f"{path.name}[{k!r}] is not a string"
