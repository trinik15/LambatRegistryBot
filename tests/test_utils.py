"""Tests for utils.py — date validation/parsing/formatting helpers.

These cover the Phase 0 join-date sanity rules (no future dates, no pre-CivMC
dates) plus the multi-format parse/format helpers that asyncpg DATE objects,
strings, and datetimes all flow through.
"""

from datetime import date, datetime

from utils import (
    MIN_JOIN_DATE,
    format_date,
    is_valid_date,
    parse_join_date,
)

# ---------------------------------------------------------------------------
# is_valid_date — the Phase 0 join-date sanity gate.
# ---------------------------------------------------------------------------


def test_is_valid_date_accepts_valid_past_date():
    assert is_valid_date("15/06/2024") is True


def test_is_valid_date_accepts_civmc_launch_date():
    # The boundary itself (2022-01-01) is allowed.
    assert is_valid_date("01/01/2022") is True


def test_is_valid_date_rejects_future_date():
    # A typo like 25/12/2099 would otherwise corrupt the "recent joins" stat.
    assert is_valid_date("25/12/2099") is False


def test_is_valid_date_rejects_pre_civmc_date():
    assert is_valid_date("01/01/2020") is False


def test_is_valid_date_rejects_day_before_launch_in_2021():
    assert is_valid_date("31/12/2021") is False


def test_is_valid_date_rejects_bad_format():
    assert is_valid_date("2024-06-15") is False  # ISO, not DD/MM/YYYY
    assert is_valid_date("15-06-2024") is False
    assert is_valid_date("abc") is False
    assert is_valid_date("") is False
    assert is_valid_date("31/02/2024") is False  # impossible day


def test_min_join_date_constant():
    assert date(2022, 1, 1) == MIN_JOIN_DATE


# ---------------------------------------------------------------------------
# parse_join_date
# ---------------------------------------------------------------------------


def test_parse_join_date_ddmmyyyy():
    assert parse_join_date("15/06/2024") == date(2024, 6, 15)


def test_parse_join_date_iso():
    # asyncpg can stringify DATE as YYYY-MM-DD; the parser must accept it.
    assert parse_join_date("2024-06-15") == date(2024, 6, 15)


def test_parse_join_date_none_returns_none():
    assert parse_join_date(None) is None


def test_parse_join_date_garbage_returns_none():
    assert parse_join_date("not-a-date") is None
    assert parse_join_date("") is None


def test_parse_join_date_passes_through_date_object():
    d = date(2024, 6, 15)
    assert parse_join_date(d) is d


def test_parse_join_date_passes_through_datetime_object():
    dt = datetime(2024, 6, 15, 12, 0)
    assert parse_join_date(dt) == date(2024, 6, 15)


# ---------------------------------------------------------------------------
# format_date
# ---------------------------------------------------------------------------


def test_format_date_none_returns_na():
    assert format_date(None) == "N/A"


def test_format_date_date_object():
    assert format_date(date(2024, 6, 15)) == "15/06/2024"


def test_format_date_datetime_object():
    assert format_date(datetime(2024, 6, 15, 12, 0)) == "15/06/2024"


def test_format_date_ddmmyyyy_string_roundtrips():
    assert format_date("15/06/2024") == "15/06/2024"


def test_format_date_iso_string_normalises_to_ddmmyyyy():
    assert format_date("2024-06-15") == "15/06/2024"


def test_format_date_custom_format():
    assert format_date(date(2024, 6, 15), "%Y-%m-%d") == "2024-06-15"
