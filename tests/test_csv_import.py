"""Tests for CSV bulk import — Phase 3.1 parser."""

from services.csv_import import parse_csv

VALID_CSV = """IGN,Discord ID,Settlement,Join Date,Address,Mailbox,Recruiter IDs,Notes
SteveB,111111111111111111,Lambat City,15/01/2025,,,222222222222222222,
Alex123,333333333333333333,Florraine,20/01/2025,,,"444444444444444444,555555555555555555",New member
"""

BAD_IGN_CSV = """IGN,Discord ID,Settlement,Join Date
ab,111,Lambat City,15/01/2025
this_ign_is_way_too_long,222,Florraine,20/01/2025
"""

MISSING_FIELDS_CSV = """IGN,Discord ID,Settlement,Join Date
,111,Lambat City,15/01/2025
SteveB,,Lambat City,15/01/2025
SteveB,111,,15/01/2025
"""


class TestParseCsv:
    def test_valid_csv(self):
        result = parse_csv(VALID_CSV, known_settlements=["Lambat City", "Florraine"])
        assert result.total == 2
        assert result.valid_count == 2
        assert result.invalid_count == 0
        assert result.rows[0].ign == "SteveB"
        assert result.rows[1].ign == "Alex123"

    def test_empty_csv(self):
        result = parse_csv("", known_settlements=[])
        assert result.total == 0

    def test_header_only(self):
        result = parse_csv("IGN,Discord ID,Settlement\n", known_settlements=[])
        assert result.total == 0

    def test_bad_ign_format(self):
        result = parse_csv(BAD_IGN_CSV, known_settlements=["Lambat City", "Florraine"])
        assert result.invalid_count == 2
        assert any("Invalid IGN" in e for r in result.rows for e in r.errors)

    def test_missing_fields(self):
        result = parse_csv(MISSING_FIELDS_CSV, known_settlements=["Lambat City"])
        assert result.invalid_count == 3
        assert any("Missing IGN" in e for r in result.rows for e in r.errors)
        assert any("Missing Discord ID" in e for r in result.rows for e in r.errors)
        assert any("Missing settlement" in e for r in result.rows for e in r.errors)

    def test_unknown_settlement(self):
        csv_content = "IGN,Discord ID,Settlement,Join Date\nSteveB,111,Nowhere,15/01/2025\n"
        result = parse_csv(csv_content, known_settlements=["Lambat City"])
        assert result.invalid_count == 1
        assert "Nowhere" in result.unknown_settlements

    def test_duplicate_ign_in_csv(self):
        csv_content = (
            "IGN,Discord ID,Settlement,Join Date\n"
            "SteveB,111,Lambat City,15/01/2025\n"
            "SteveB,222,Florraine,20/01/2025\n"
        )
        result = parse_csv(csv_content, known_settlements=["Lambat City", "Florraine"])
        assert result.invalid_count == 1  # second SteveB is invalid
        dup_row = [r for r in result.rows if r.ign == "SteveB" and r.line == 3][0]
        assert any("Duplicate IGN" in e for e in dup_row.errors)

    def test_existing_ign_warning(self):
        csv_content = "IGN,Discord ID,Settlement,Join Date\nSteveB,111,Lambat City,15/01/2025\n"
        result = parse_csv(
            csv_content,
            known_settlements=["Lambat City"],
            existing_igns=["SteveB"],
        )
        assert result.valid_count == 1  # still valid, just warned
        assert "SteveB" in result.duplicate_igns_in_csv
        assert any("already exists" in w for r in result.rows for w in r.warnings)

    def test_bad_discord_id(self):
        csv_content = (
            "IGN,Discord ID,Settlement,Join Date\nSteveB,not_a_number,Lambat City,15/01/2025\n"
        )
        result = parse_csv(csv_content, known_settlements=["Lambat City"])
        assert result.invalid_count == 1
        assert any("Invalid Discord ID" in e for r in result.rows for e in r.errors)

    def test_bytes_input_with_bom(self):
        """UTF-8 BOM should be handled gracefully."""
        bom = b"\xef\xbb\xbf"
        csv_bytes = bom + VALID_CSV.encode("utf-8")
        result = parse_csv(csv_bytes, known_settlements=["Lambat City", "Florraine"])
        assert result.total == 2
        assert result.rows[0].ign == "SteveB"

    def test_case_insensitive_headers(self):
        csv_content = "ign,discord id,settlement,join date\nSteveB,111,Lambat City,15/01/2025\n"
        result = parse_csv(csv_content, known_settlements=["Lambat City"])
        assert result.total == 1
        assert result.rows[0].ign == "SteveB"

    def test_case_insensitive_settlement_match(self):
        """Settlement names should match case-insensitively (CITEXT)."""
        csv_content = "IGN,Discord ID,Settlement,Join Date\nSteveB,111,lambat city,15/01/2025\n"
        result = parse_csv(csv_content, known_settlements=["Lambat City"])
        assert result.valid_count == 1
