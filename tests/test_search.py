"""Tests for /citizen search — Phase 3.2 search results embed builder."""

from datetime import date

from cogs.citizen import _build_search_results_embeds


class TestBuildSearchResultsEmbeds:
    """_build_search_results_embeds paginates search results into embeds."""

    def test_single_page(self):
        rows = [
            {
                "ign": "SteveB",
                "discord_id": "111",
                "settlement": "Lambat City",
                "join_date": date(2025, 1, 15),
            }
        ]
        embeds = _build_search_results_embeds("Steve", rows)
        assert len(embeds) == 1
        assert "Steve" in embeds[0].title
        assert "1" in embeds[0].description  # 1 result
        assert "SteveB" in embeds[0].fields[0].value

    def test_multiple_pages(self):
        """15 results → 2 pages (10 + 5)."""
        rows = [
            {
                "ign": f"Player{i}",
                "discord_id": str(100 + i),
                "settlement": "Lambat City",
                "join_date": date(2025, 1, 1),
            }
            for i in range(15)
        ]
        embeds = _build_search_results_embeds("Player", rows)
        assert len(embeds) == 2
        assert "1/2" in embeds[0].footer.text
        assert "2/2" in embeds[1].footer.text

    def test_empty_results(self):
        embeds = _build_search_results_embeds("Nobody", [])
        assert len(embeds) == 0

    def test_embed_contains_ign_and_settlement(self):
        rows = [
            {
                "ign": "TestUser",
                "discord_id": "999",
                "settlement": "Florraine",
                "join_date": date(2024, 6, 1),
            }
        ]
        embeds = _build_search_results_embeds("Test", rows)
        value = embeds[0].fields[0].value
        assert "TestUser" in value
        assert "Florraine" in value

    def test_total_count_in_description(self):
        rows = [
            {
                "ign": f"U{i}",
                "discord_id": str(i),
                "settlement": "X",
                "join_date": date(2025, 1, 1),
            }
            for i in range(25)
        ]
        embeds = _build_search_results_embeds("U", rows)
        assert "25" in embeds[0].description

    def test_discord_mention_in_results(self):
        rows = [
            {
                "ign": "SteveB",
                "discord_id": "123456789",
                "settlement": "Lambat City",
                "join_date": date(2025, 1, 1),
            }
        ]
        embeds = _build_search_results_embeds("Steve", rows)
        assert "<@123456789>" in embeds[0].fields[0].value
