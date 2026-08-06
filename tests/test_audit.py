"""Tests for services/audit.py — the pure helpers (Phase 2.1).

The DB-touching functions (emit, search, post_to_channel) require a live
asyncpg pool and are exercised by the integration in CI against a real
Postgres. Here we cover the pure logic: the action vocabulary, the JSON
serialiser fallback, the embed-summary renderer, and the colour/label map —
the pieces most likely to silently regress.
"""

from datetime import UTC, date, datetime

import pytest

from services import audit

# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------


def test_all_actions_complete_and_stable():
    """The vocabulary is the single source of truth for /audit search filters."""
    assert audit.CITIZEN_ADD == "citizen.add"
    assert audit.CITIZEN_UPDATE == "citizen.update"
    assert audit.CITIZEN_REMOVE == "citizen.remove"
    assert audit.SETTLEMENT_ADD == "settlement.add"
    assert audit.SETTLEMENT_REMOVE == "settlement.remove"
    assert audit.ROLE_SYNC_DISCREPANCY == "role_sync.discrepancy"
    assert audit.ROLE_SYNC_FIXED == "role_sync.fixed"
    assert audit.EMOJI_SET == "emoji.set"
    assert audit.SNAPSHOT_ANNOTATE == "snapshot.annotate"
    assert audit.AUDIT_PRUNE == "audit.prune"
    assert len(audit.ALL_ACTIONS) == 10
    # No duplicates.
    assert len(set(audit.ALL_ACTIONS)) == 10


def test_snapshot_annotate_has_label_and_color():
    """Phase 4.6: the new action renders in the audit embed."""
    assert audit._action_label(audit.SNAPSHOT_ANNOTATE) == "Snapshot annotated"
    # Blurple (same as citizen.update — it's an update-class op).
    assert audit._action_color(audit.SNAPSHOT_ANNOTATE) == 0x5865F2


def test_audit_prune_has_label():
    """ROADMAP §6.2: the retention-prune action renders in the audit embed.

    Color is left to the default grey-blue (it's a maintenance op, not a
    create/update/remove) so we only assert the label here.
    """
    assert audit._action_label(audit.AUDIT_PRUNE) == "Audit log pruned"
    assert audit.AUDIT_PRUNE in audit.ALL_ACTIONS


# ---------------------------------------------------------------------------
# _json_default — the fallback that lets json.dumps handle date/set values
# in the details dict (which is common: join_date is a date, changes keys may
# come from a set).
# ---------------------------------------------------------------------------


def test_json_default_datetime():
    dt = datetime(2024, 1, 15, 12, 30, tzinfo=UTC)
    assert audit._json_default(dt) == "2024-01-15T12:30:00+00:00"


def test_json_default_date():
    d = date(2024, 1, 15)
    assert audit._json_default(d) == "2024-01-15"


def test_json_default_set():
    assert audit._json_default({"b", "a"}) == ["a", "b"]


def test_json_default_unknown_falls_back_to_str():
    assert audit._json_default(42) == "42"


def test_emit_uses_json_default_for_dates():
    """json.dumps with default=_json_default must not raise on a date value."""
    import json

    details = {"join_date": date(2024, 1, 15)}
    # This is the exact call emit() makes internally.
    result = json.dumps(details, default=audit._json_default)
    assert "2024-01-15" in result


# ---------------------------------------------------------------------------
# _action_label — the human-readable title for the audit embed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        (audit.CITIZEN_ADD, "Citizen added"),
        (audit.CITIZEN_UPDATE, "Citizen updated"),
        (audit.CITIZEN_REMOVE, "Citizen removed"),
        (audit.SETTLEMENT_ADD, "Settlement added"),
        (audit.SETTLEMENT_REMOVE, "Settlement removed"),
        (audit.ROLE_SYNC_DISCREPANCY, "Role discrepancy"),
        (audit.ROLE_SYNC_FIXED, "Role auto-fixed"),
        (audit.EMOJI_SET, "Emoji updated"),
    ],
)
def test_action_label_known(action, expected):
    assert audit._action_label(action) == expected


def test_action_label_unknown_falls_back_to_action():
    assert audit._action_label("something.new") == "something.new"


# ---------------------------------------------------------------------------
# _action_color — colour-codes the embed so council can scan the channel
# ---------------------------------------------------------------------------


def test_action_color_creates_are_green():
    assert audit._action_color(audit.CITIZEN_ADD) == 0x43B581
    assert audit._action_color(audit.SETTLEMENT_ADD) == 0x43B581
    assert audit._action_color(audit.EMOJI_SET) == 0x43B581


def test_action_color_update_is_blurple():
    assert audit._action_color(audit.CITIZEN_UPDATE) == 0x5865F2


def test_action_color_removes_and_discrepancies_are_orange():
    assert audit._action_color(audit.CITIZEN_REMOVE) == 0xFF9900
    assert audit._action_color(audit.SETTLEMENT_REMOVE) == 0xFF9900
    assert audit._action_color(audit.ROLE_SYNC_DISCREPANCY) == 0xFF9900


def test_action_color_fixed_is_light_green():
    assert audit._action_color(audit.ROLE_SYNC_FIXED) == 0x57F287


def test_action_color_unknown_is_default_grey():
    assert audit._action_color("unknown.action") == 0x7289DA


# ---------------------------------------------------------------------------
# _summarise_details — renders the JSONB details as a compact embed field
# ---------------------------------------------------------------------------


def test_summarise_details_changes_shape():
    """The citizen.update details use a {field: [old, new]} dict."""
    details = {
        "changes": {
            "Settlement": ["OldPlace", "NewPlace"],
            "Address": ["123 Old St", "456 New Ave"],
        }
    }
    result = audit._summarise_details(details)
    assert "Settlement" in result
    assert "OldPlace" in result
    assert "NewPlace" in result
    assert "→" in result
    assert "Address" in result


def test_summarise_details_settlement_shape():
    """The citizen.add details carry settlement + recruiters."""
    details = {
        "discord_id": "123",
        "settlement": "Lambat City",
        "recruiters": ["111", "222"],
    }
    result = audit._summarise_details(details)
    assert "Lambat City" in result
    assert "<@111>" in result
    assert "<@222>" in result


def test_summarise_details_name_shape():
    """The settlement.add details carry name + duchy."""
    details = {"name": "New September", "duchy": "Lambat City"}
    result = audit._summarise_details(details)
    assert "New September" in result
    assert "Lambat City" in result


def test_summarise_details_role_sync_shape():
    """The role_sync discrepancy details carry member + issues."""
    details = {
        "member": "999",
        "issues": ["missing_settler_role", "has_guest_role"],
    }
    result = audit._summarise_details(details)
    assert "999" in result
    assert "missing_settler_role" in result


def test_summarise_details_generic_fallback():
    """Unknown shapes fall back to a key=value listing."""
    details = {"foo": "bar", "baz": 42}
    result = audit._summarise_details(details)
    assert "foo" in result
    assert "bar" in result
    assert "baz" in result


def test_summarise_details_empty_dict():
    assert audit._summarise_details({}) == ""


def test_summarise_details_changes_with_non_list_value():
    """A changes entry that isn't [old, new] is rendered as field: value."""
    details = {"changes": {"note": "just a value"}}
    result = audit._summarise_details(details)
    assert "note" in result
    assert "just a value" in result
