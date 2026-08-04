"""CSV bulk import service — Phase 3.1.

Parses and validates a CSV file of citizens before any DB writes. The dry-run
preview shows conflicts (duplicate IGN, unknown settlement, bad IGN format);
the confirm button triggers the actual import via the cog.

The CSV format matches /report export:
    IGN, Discord ID, Settlement, Join Date, Address, Mailbox, Recruiter IDs, Notes

This module is import-safe (no Discord / DB calls in the parser); the cog
handles the DB writes and role assignment.
"""

import csv
import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Minecraft username rules: 3-16 chars, alphanumeric + underscore.
_IGN_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


@dataclass
class ParsedRow:
    """A single parsed CSV row with validation results."""

    line: int
    ign: str
    discord_id: str
    settlement: str
    join_date: str
    address: str = ""
    mailbox: str = ""
    recruiter_ids: str = ""
    notes: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


@dataclass
class ParseResult:
    """Result of parsing a CSV file: rows + summary stats."""

    rows: list[ParsedRow] = field(default_factory=list)
    valid_count: int = 0
    invalid_count: int = 0
    duplicate_igns_in_csv: list[str] = field(default_factory=list)
    unknown_settlements: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)


def parse_csv(
    csv_content: str | bytes,
    known_settlements: list[str] | None = None,
    existing_igns: list[str] | None = None,
) -> ParseResult:
    """Parse a CSV file of citizens and validate each row.

    Args:
        csv_content: The CSV file content (str or bytes).
        known_settlements: Settlement names that exist in the DB. If provided,
            rows referencing unknown settlements are flagged.
        existing_igns: IGNs already in the registry. If provided, rows with
            duplicate IGNs are flagged.

    Returns:
        ParseResult with all rows + validation summary.
    """
    if isinstance(csv_content, bytes):
        csv_content = csv_content.decode("utf-8-sig")  # handle BOM

    result = ParseResult()
    settlements_lower = {s.lower() for s in (known_settlements or [])}
    existing_igns_lower = {i.lower() for i in (existing_igns or [])}
    seen_igns_in_csv: dict[str, int] = {}  # ign_lower → first line number

    reader = csv.DictReader(io.StringIO(csv_content))
    if reader.fieldnames is None:
        return result  # empty file

    # Normalize headers (case-insensitive match).
    fieldmap = _normalize_headers(list(reader.fieldnames))

    for line_num, raw_row in enumerate(reader, start=2):  # line 1 = header
        row = _extract_row(raw_row, fieldmap, line_num)
        _validate_row(
            row,
            settlements_lower,
            existing_igns_lower,
            seen_igns_in_csv,
            result,
        )
        result.rows.append(row)

    result.valid_count = sum(1 for r in result.rows if r.is_valid)
    result.invalid_count = result.total - result.valid_count
    return result


def _normalize_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Map normalized header names to the canonical field names."""
    canonical = {
        "ign": "ign",
        "discord id": "discord_id",
        "discord_id": "discord_id",
        "settlement": "settlement",
        "join date": "join_date",
        "join_date": "join_date",
        "address": "address",
        "mailbox": "mailbox",
        "recruiter ids": "recruiter_ids",
        "recruiter_ids": "recruiter_ids",
        "notes": "notes",
    }
    fieldmap: dict[str, str] = {}
    if fieldnames:
        for fn in fieldnames:
            key = fn.strip().lower()
            if key in canonical:
                fieldmap[canonical[key]] = fn
    return fieldmap


def _extract_row(raw_row: dict, fieldmap: dict[str, str], line: int) -> ParsedRow:
    """Extract a ParsedRow from a CSV dict row."""

    def get(field: str) -> str:
        col = fieldmap.get(field)
        if col is None:
            return ""
        val = raw_row.get(col, "")
        return val.strip() if isinstance(val, str) else ""

    return ParsedRow(
        line=line,
        ign=get("ign"),
        discord_id=get("discord_id"),
        settlement=get("settlement"),
        join_date=get("join_date"),
        address=get("address"),
        mailbox=get("mailbox"),
        recruiter_ids=get("recruiter_ids"),
        notes=get("notes"),
    )


def _validate_row(
    row: ParsedRow,
    settlements_lower: set[str],
    existing_igns_lower: set[str],
    seen_igns_in_csv: dict[str, int],
    result: ParseResult,
) -> None:
    """Validate a single row, appending errors/warnings in place."""
    # IGN format.
    if not row.ign:
        row.errors.append("Missing IGN")
    elif not _IGN_PATTERN.match(row.ign):
        row.errors.append(f"Invalid IGN format: '{row.ign}' (must be 3-16 alphanumeric/underscore)")

    # Discord ID.
    if not row.discord_id:
        row.errors.append("Missing Discord ID")
    elif not row.discord_id.isdigit():
        row.errors.append(f"Invalid Discord ID: '{row.discord_id}' (must be numeric)")

    # Settlement.
    if not row.settlement:
        row.errors.append("Missing settlement")
    elif settlements_lower and row.settlement.lower() not in settlements_lower:
        row.errors.append(f"Unknown settlement: '{row.settlement}'")
        if row.settlement not in result.unknown_settlements:
            result.unknown_settlements.append(row.settlement)

    # Join date (optional — if missing, defaults to today on import).
    if row.join_date and len(row.join_date) < 8:
        # Basic format check (DD/MM/YYYY); the full validation happens on import.
        row.warnings.append(f"Suspicious join_date format: '{row.join_date}'")

    # Duplicate IGN within the CSV.
    if row.ign:
        ign_lower = row.ign.lower()
        if ign_lower in seen_igns_in_csv:
            row.errors.append(
                f"Duplicate IGN in CSV (first seen at line {seen_igns_in_csv[ign_lower]})"
            )
        else:
            seen_igns_in_csv[ign_lower] = row.line

        # Duplicate IGN against existing registry.
        if existing_igns_lower and ign_lower in existing_igns_lower:
            row.warnings.append("IGN already exists in the registry (will be skipped on import)")
            if row.ign not in result.duplicate_igns_in_csv:
                result.duplicate_igns_in_csv.append(row.ign)
