"""Tests for /server online — Phase 3.8 online-now cross-reference."""

from cogs.server import _build_online_embed, _partition_citizens


class TestPartitionCitizens:
    """_partition_citizens is the pure helper that splits the online list."""

    def test_empty_online_list(self):
        citizens, non_citizens = _partition_citizens([], None)
        assert citizens == []
        assert non_citizens == []

    def test_no_citizens_online(self):
        """All online players are non-citizens."""
        online = ["Player1", "Player2", "Player3"]
        citizens, non_citizens = _partition_citizens(online, None)
        assert citizens == []
        assert sorted(non_citizens) == sorted(online)

    def test_all_citizens_online(self):
        """All online players are registered citizens."""
        online = ["SteveB", "Alex123"]
        citizen_rows = [
            {"ign": "SteveB", "settlement": "Lambat City"},
            {"ign": "Alex123", "settlement": "Florraine"},
        ]
        citizens, non_citizens = _partition_citizens(online, citizen_rows)
        assert len(citizens) == 2
        assert ("SteveB", "Lambat City") in citizens
        assert ("Alex123", "Florraine") in citizens
        assert non_citizens == []

    def test_mixed_online(self):
        """Some citizens, some non-citizens."""
        online = ["SteveB", "RandomDude", "Alex123", "Nobody"]
        citizen_rows = [
            {"ign": "SteveB", "settlement": "Lambat City"},
            {"ign": "Alex123", "settlement": "Florraine"},
        ]
        citizens, non_citizens = _partition_citizens(online, citizen_rows)
        assert len(citizens) == 2
        assert len(non_citizens) == 2
        assert "RandomDude" in non_citizens
        assert "Nobody" in non_citizens

    def test_case_insensitive_match(self):
        """CITEXT makes the registry case-insensitive; the helper must match too."""
        online = ["SteveB", "ALEX123"]
        citizen_rows = [
            {"ign": "steveb", "settlement": "Lambat City"},
            {"ign": "alex123", "settlement": "Florraine"},
        ]
        citizens, non_citizens = _partition_citizens(online, citizen_rows)
        assert len(citizens) == 2
        assert non_citizens == []
        # The original-case IGN from the online list is preserved in the output.
        igns = [c[0] for c in citizens]
        assert "SteveB" in igns
        assert "ALEX123" in igns

    def test_empty_strings_skipped(self):
        """Empty/falsy entries in the online list are skipped, not counted."""
        online = ["SteveB", "", "Alex123"]
        citizen_rows = [{"ign": "SteveB", "settlement": "Lambat City"}]
        citizens, non_citizens = _partition_citizens(online, citizen_rows)
        assert len(citizens) == 1
        assert len(non_citizens) == 1
        assert non_citizens[0] == "Alex123"

    def test_sorted_output(self):
        """Citizens and non-citizens are sorted alphabetically (case-insensitive)."""
        online = ["Zebra", "alpha", "Banana"]
        citizen_rows = [{"ign": "Zebra", "settlement": "X"}]
        citizens, non_citizens = _partition_citizens(online, citizen_rows)
        assert citizens[0][0] == "Zebra"
        assert non_citizens == ["alpha", "Banana"]


class TestBuildOnlineEmbed:
    """_build_online_embed constructs the Discord embed."""

    def test_no_players_online(self):
        embed = _build_online_embed(0, 20, [], [])
        assert "0/20" in embed.description
        assert "0" in embed.description  # 0 citizens

    def test_citizens_and_non_citizens(self):
        citizens = [("SteveB", "Lambat City"), ("Alex123", "Florraine")]
        non_citizens = ["RandomDude"]
        embed = _build_online_embed(3, 50, citizens, non_citizens)
        assert "3/50" in embed.description
        assert "2" in embed.description  # 2 citizens
        # Both citizen names should appear in the embed fields.
        field_values = [f.value for f in embed.fields]
        assert any("SteveB" in v and "Lambat City" in v for v in field_values)
        assert any("Alex123" in v and "Florraine" in v for v in field_values)
        assert any("RandomDude" in v for v in field_values)

    def test_more_than_25_truncated(self):
        """When >25 citizens, the list is truncated with a '...and N more' note."""
        citizens = [(f"Player{i}", "Settlement") for i in range(30)]
        embed = _build_online_embed(30, 100, citizens, [])
        citizen_field = embed.fields[0]
        assert "more" in citizen_field.value

    def test_only_citizens_no_non_citizens_field(self):
        """When there are no non-citizens, no 'Other Players' field is added."""
        citizens = [("SteveB", "Lambat City")]
        embed = _build_online_embed(1, 20, citizens, [])
        field_names = [f.name for f in embed.fields]
        assert any("Lambat" in n for n in field_names)
        assert not any("Other" in n for n in field_names)

    def test_only_non_citizens_no_citizens_field(self):
        """When no citizens are online, no 'Lambat Citizens' field is added."""
        non_citizens = ["RandomDude"]
        embed = _build_online_embed(1, 20, [], non_citizens)
        field_names = [f.name for f in embed.fields]
        assert not any("Lambat" in n for n in field_names)
        assert any("Other" in n for n in field_names)
