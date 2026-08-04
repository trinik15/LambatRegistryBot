"""Tests for tasks/role_sync.py — discrepancy detection (Phase 2.5).

``detect_role_issues`` is a pure function (no I/O) that takes a Discord member
and a settlement name and returns a list of human-readable discrepancies. We
test it with lightweight fakes instead of real discord.Member objects so the
role-reconciliation logic is covered without a live guild.
"""

from core.config import Config
from tasks.role_sync import detect_role_issues


class FakeRole:
    """A minimal stand-in for discord.Role — just an id and name."""

    def __init__(self, role_id: int, name: str = ""):
        self.id = role_id
        self.name = name


class FakeMember:
    """A minimal stand-in for discord.Member with just a .roles list."""

    def __init__(self, roles: list[FakeRole]):
        self.roles = roles


# Test role IDs (must match the dummy env in conftest.py for realism, but the
# detect_role_issues function reads Config directly so we just need consistency
# within these tests).
_CITIZEN_ROLE_ID = 111111111111111111
_SETTLER_ROLE_ID = 999999999999999999
_GUEST_ROLE_ID = 888888888888888888


def _member_with(*roles: FakeRole) -> FakeMember:
    return FakeMember(list(roles))


def _make_config():
    """Patch Config so detect_role_issues uses our test role IDs."""
    Config.CITIZEN_ROLE_IDS = [_CITIZEN_ROLE_ID]
    Config.SETTLER_ROLE_ID = _SETTLER_ROLE_ID
    Config.GUEST_ROLE_ID = _GUEST_ROLE_ID


def test_no_issues_when_all_roles_present():
    _make_config()
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(_SETTLER_ROLE_ID),
        FakeRole(123, "Pioneer"),
    )
    issues = detect_role_issues(member, "Pioneer")
    assert issues == []


def test_missing_citizen_role_detected():
    _make_config()
    member = _member_with(
        FakeRole(_SETTLER_ROLE_ID),
        FakeRole(123, "Pioneer"),
    )
    issues = detect_role_issues(member, "Pioneer")
    assert any("missing_citizen_role" in i for i in issues)


def test_missing_settler_role_detected():
    _make_config()
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(123, "Pioneer"),
    )
    issues = detect_role_issues(member, "Pioneer")
    assert "missing_settler_role" in issues


def test_missing_settlement_role_detected():
    _make_config()
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(_SETTLER_ROLE_ID),
        # No role named "Pioneer" → discrepancy
    )
    issues = detect_role_issues(member, "Pioneer")
    assert any("missing_settlement_role:Pioneer" in i for i in issues)


def test_settlement_role_case_insensitive():
    _make_config()
    # Discord role is "pioneer" (lowercase), settlement is "Pioneer".
    # CITEXT in the DB makes these equal; the role check must match.
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(_SETTLER_ROLE_ID),
        FakeRole(123, "pioneer"),
    )
    issues = detect_role_issues(member, "Pioneer")
    assert not any("missing_settlement_role" in i for i in issues)


def test_guest_role_present_detected():
    _make_config()
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(_SETTLER_ROLE_ID),
        FakeRole(123, "Pioneer"),
        FakeRole(_GUEST_ROLE_ID),  # should have been removed on registration
    )
    issues = detect_role_issues(member, "Pioneer")
    assert "has_guest_role" in issues


def test_multiple_issues_all_reported():
    _make_config()
    member = _member_with(
        # missing citizen role + settler role + settlement role, has guest role
        FakeRole(_GUEST_ROLE_ID),
    )
    issues = detect_role_issues(member, "Pioneer")
    assert any("missing_citizen_role" in i for i in issues)
    assert "missing_settler_role" in issues
    assert any("missing_settlement_role" in i for i in issues)
    assert "has_guest_role" in issues


def test_empty_settlement_skips_settlement_check():
    """An empty settlement string must not crash or flag a missing role."""
    _make_config()
    member = _member_with(
        FakeRole(_CITIZEN_ROLE_ID),
        FakeRole(_SETTLER_ROLE_ID),
    )
    issues = detect_role_issues(member, "")
    assert not any("settlement_role" in i for i in issues)
