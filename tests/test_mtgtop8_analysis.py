from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from mtgtop8_analysis import (
    CardStat,
    DeckCard,
    DeckRecord,
    build_card_stats,
    card_type_group,
    is_top_eight,
    parse_cached_deck,
    parse_event_page,
    parse_listing_page,
    parse_rank,
    scryfall_image,
    suggest_swaps,
    tournament_weight,
    write_cached_deck,
)


LISTING_HTML = """
<table>
  <tr class=hover_tr>
    <td><input type=hidden name=deck_ref[1] value=884927></td>
    <td><a href=/event?e=90236&d=884927&f=EDH>Slimefoot And Squee</a></td>
    <td><a class=player>Bastien Béron</a></td>
    <td><a href=/event?e=90236&f=EDH>Win a Scrubland</a></td>
    <td><img src=/graph/star.png><img src=/graph/star.png></td>
    <td>3-4</td><td>30/08/26</td>
  </tr>
</table>
"""


EVENT_HTML = """
<div class=meta_arch>Duel Commander <img src=/graph/online/paper.png title="Paper"></div>
<div style="margin-bottom:5px;">106 players - 03/05/25</div>
<div class=O14>COMMANDER</div>
<div id=sbmoc447 class="deck_line hover_tr">1 <span class=L14>Slimefoot and Squee</span></div>
<div class=O14 style="margin-top:5px;">2 LANDS (3)</div>
<div id=mdabu092 class="deck_line hover_tr">2 <span class=L14>Forest</span></div>
<div class=O14 style="margin-top:5px;">1 CREATURES</div>
<div id=mdabu016 class="deck_line hover_tr">1 <span class=L14>Birds of Paradise</span></div>
"""


def record(*, players: int = 32, rank: str = "3-4", level: int = 2) -> DeckRecord:
    return DeckRecord(
        deck_id="1",
        event_id="2",
        deck_name="Slimefoot",
        player="Player",
        event_name="Event",
        level=level,
        rank=rank,
        event_date="2026-08-01",
        players=players,
        platform="Paper",
        url="https://example.test",
        cache_file="1.txt",
        card_count=100,
        downloaded_at="2026-08-02T10:00:00+02:00",
    )


class ParserTests(unittest.TestCase):
    def test_listing_parser_extracts_metadata(self) -> None:
        decks = parse_listing_page(LISTING_HTML)
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0].deck_id, "884927")
        self.assertEqual(decks[0].player, "Bastien Béron")
        self.assertEqual(decks[0].level, 2)
        self.assertEqual(decks[0].rank, "3-4")
        self.assertEqual(decks[0].event_date, date(2026, 8, 30))

    def test_event_parser_extracts_cards_size_and_platform(self) -> None:
        event = parse_event_page(EVENT_HTML)
        self.assertEqual(event.players, 106)
        self.assertEqual(event.platform, "Paper")
        self.assertEqual(sum(card.quantity for card in event.cards), 4)
        self.assertEqual([card.section for card in event.cards], ["commander", "lands", "creatures"])

    def test_cache_is_human_readable_and_round_trips(self) -> None:
        cards = [DeckCard(1, "Slimefoot and Squee", "commander"), DeckCard(2, "Forest", "lands")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deck.txt"
            write_cached_deck(path, record(), cards, "Slimefoot and Squee")
            self.assertIn("[COMMANDER]", path.read_text(encoding="utf-8"))
            self.assertEqual(parse_cached_deck(path), cards)


class AnalysisTests(unittest.TestCase):
    def test_rank_parser_and_top_eight_filter(self) -> None:
        self.assertEqual(parse_rank("5-8"), (5, 8))
        self.assertTrue(is_top_eight("5-8"))
        self.assertFalse(is_top_eight("9-16"))

    def test_larger_better_finish_has_more_weight(self) -> None:
        self.assertGreater(
            tournament_weight(record(players=100, rank="1", level=3)),
            tournament_weight(record(players=10, rank="5-8", level=1)),
        )

    def test_commander_stays_commander_after_scryfall_enrichment(self) -> None:
        samples = [(record(), [DeckCard(1, "Slimefoot and Squee", "commander")])]
        current = [DeckCard(1, "Slimefoot and Squee", "commander")]
        metadata = {
            "slimefoot and squee": {
                "name": "Slimefoot and Squee",
                "type_line": "Legendary Creature — Fungus Goblin",
            }
        }
        stats = build_card_stats(samples, current, metadata)
        self.assertEqual(stats[0].section, "commander")

    def test_swaps_only_pair_same_section(self) -> None:
        stats = [
            CardStat("old", "Old Creature", "creatures", 1, 5, 5, 1),
            CardStat("new", "New Creature", "creatures", 10, 80, 80, 0),
            CardStat("land", "Popular Land", "lands", 10, 90, 90, 0),
        ]
        swaps = suggest_swaps(stats, 10)
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].remove.name, "Old Creature")
        self.assertEqual(swaps[0].add.name, "New Creature")

    def test_precise_card_type_groups(self) -> None:
        instant = CardStat("bolt", "Bolt", "spells", 1, 10, 10, 1, type_line="Instant")
        artifact_creature = CardStat(
            "robot", "Robot", "creatures", 1, 10, 10, 1, type_line="Artifact Creature — Robot"
        )
        enchantment = CardStat("aura", "Aura", "other", 1, 10, 10, 1, type_line="Enchantment — Aura")
        self.assertEqual(card_type_group(instant), "instants")
        self.assertEqual(card_type_group(artifact_creature), "creatures")
        self.assertEqual(card_type_group(enchantment), "enchantments")

    def test_double_faced_card_image_falls_back_to_first_face(self) -> None:
        card = {
            "card_faces": [
                {"name": "Front", "image_uris": {"normal": "https://example.test/front.jpg"}},
                {"name": "Back", "image_uris": {"normal": "https://example.test/back.jpg"}},
            ]
        }
        self.assertEqual(scryfall_image(card), "https://example.test/front.jpg")


if __name__ == "__main__":
    unittest.main()
