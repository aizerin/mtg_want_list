#!/usr/bin/env python3
"""Cache MTGTop8 Duel Commander decks and compare them with a local deck."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


MTGTOP8_BASE_URL = "https://www.mtgtop8.com/"
SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
DEFAULT_COMMANDER = "Slimefoot and Squee"
DEFAULT_ARCHETYPE_ID = 1650
DEFAULT_FORMAT = "EDH"
DEFAULT_DECK = Path("decks/duel-slimfoot")
DEFAULT_CACHE_DIR = Path("mtgtop8-cache/slimefoot-and-squee")
DEFAULT_OUTPUT = Path("duel-slimfoot-analysis.html")
USER_AGENT = "wantlist-scryfall-mtgtop8-analysis/1.0 (personal deck analysis)"
STATE_VERSION = 1
SECTION_ORDER = ["commander", "lands", "creatures", "spells", "other", "unknown"]
SECTION_LABELS = {
    "commander": "Commander",
    "lands": "Země",
    "creatures": "Bytosti",
    "spells": "Instanty a sorcery",
    "other": "Ostatní permanenty",
    "unknown": "Neurčeno",
}
TYPE_GROUP_ORDER = [
    "commander",
    "lands",
    "creatures",
    "instants",
    "sorceries",
    "artifacts",
    "enchantments",
    "planeswalkers",
    "battles",
    "other",
]
TYPE_GROUP_LABELS = {
    "commander": "Commander",
    "lands": "Země",
    "creatures": "Bytosti",
    "instants": "Instanty",
    "sorceries": "Sorcery",
    "artifacts": "Artefakty",
    "enchantments": "Enchantmenty",
    "planeswalkers": "Planeswalkeři",
    "battles": "Battles",
    "other": "Ostatní",
}


@dataclass
class ListingDeck:
    deck_id: str
    event_id: str
    deck_name: str
    player: str
    event_name: str
    level: int
    rank: str
    event_date: date

    @property
    def url(self) -> str:
        query = urllib.parse.urlencode({"e": self.event_id, "d": self.deck_id, "f": DEFAULT_FORMAT})
        return urllib.parse.urljoin(MTGTOP8_BASE_URL, f"event?{query}")


@dataclass
class DeckRecord:
    deck_id: str
    event_id: str
    deck_name: str
    player: str
    event_name: str
    level: int
    rank: str
    event_date: str
    players: int | None
    platform: str
    url: str
    cache_file: str
    card_count: int
    downloaded_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeckRecord":
        return cls(
            deck_id=str(value["deck_id"]),
            event_id=str(value["event_id"]),
            deck_name=str(value.get("deck_name") or ""),
            player=str(value.get("player") or ""),
            event_name=str(value.get("event_name") or ""),
            level=int(value.get("level") or 0),
            rank=str(value.get("rank") or ""),
            event_date=str(value["event_date"]),
            players=int(value["players"]) if value.get("players") is not None else None,
            platform=str(value.get("platform") or "unknown"),
            url=str(value.get("url") or ""),
            cache_file=str(value["cache_file"]),
            card_count=int(value.get("card_count") or 0),
            downloaded_at=str(value.get("downloaded_at") or ""),
        )


@dataclass(frozen=True)
class DeckCard:
    quantity: int
    name: str
    section: str


@dataclass
class EventDeck:
    players: int | None = None
    platform: str = "unknown"
    cards: list[DeckCard] = field(default_factory=list)


@dataclass
class CardStat:
    key: str
    name: str
    section: str
    decks: int
    raw_pct: float
    weighted_pct: float
    current_quantity: int
    type_line: str = ""
    image_url: str = ""
    scryfall_url: str = ""


@dataclass(frozen=True)
class SwapSuggestion:
    remove: CardStat
    add: CardStat

    @property
    def gain(self) -> float:
        return self.add.weighted_pct - self.remove.weighted_pct


class Fetcher:
    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self.last_request = 0.0

    def get_text(self, url: str) -> str:
        elapsed = time.monotonic() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                charset = response.headers.get_content_charset() or "iso-8859-1"
                # MTGTop8 declares ISO-8859-1 but some names contain Windows-1252
                # bytes (for example š as 0x9A). Windows-1252 is a compatible
                # superset for the printable characters used by the site.
                if charset.casefold() in {"iso-8859-1", "latin-1", "latin1"}:
                    charset = "windows-1252"
        finally:
            self.last_request = time.monotonic()
        return payload.decode(charset, errors="replace")


class ListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "tr" and "hover_tr" in attributes.get("class", "").split():
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "hrefs": [], "stars": 0}
            self._row.append(self._cell)
        elif tag == "a" and self._cell is not None:
            self._cell["hrefs"].append(attributes.get("href", ""))
        elif tag == "img" and self._cell is not None and attributes.get("src", "").endswith("/star.png"):
            self._cell["stars"] += 1

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None


class EventParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_section = "unknown"
        self.cards: list[DeckCard] = []
        self._capture_kind: str | None = None
        self._capture_depth = 0
        self._capture_text: list[str] = []
        self._card_section = "unknown"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if self._capture_kind is not None:
            if tag == "div":
                self._capture_depth += 1
            return

        if tag != "div":
            return

        classes = attributes.get("class", "").split()
        element_id = attributes.get("id", "")
        if "O14" in classes:
            self._capture_kind = "heading"
            self._capture_depth = 1
            self._capture_text = []
        elif (element_id.startswith("md") or element_id.startswith("sb")) and "deck_line" in classes:
            self._capture_kind = "card"
            self._capture_depth = 1
            self._capture_text = []
            self._card_section = self.current_section

    def handle_data(self, data: str) -> None:
        if self._capture_kind is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or self._capture_kind is None:
            return
        self._capture_depth -= 1
        if self._capture_depth:
            return

        value = clean_text(" ".join(self._capture_text))
        if self._capture_kind == "heading":
            self.current_section = section_from_heading(value)
        elif self._capture_kind == "card":
            match = re.match(r"^(\d+)\s+(.+)$", value)
            if match:
                self.cards.append(DeckCard(int(match.group(1)), match.group(2).strip(), self._card_section))
        self._capture_kind = None
        self._capture_text = []


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def normalize_name(value: str) -> str:
    value = value.replace("’", "'").replace("`", "'").strip().casefold()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", value)


def section_from_heading(value: str) -> str:
    heading = value.upper()
    if "COMMANDER" in heading:
        return "commander"
    if "LAND" in heading:
        return "lands"
    if "CREATURE" in heading:
        return "creatures"
    if "INSTANT" in heading or "SORC" in heading:
        return "spells"
    if "OTHER" in heading:
        return "other"
    return "unknown"


def parse_rank(value: str) -> tuple[int, int] | None:
    numbers = [int(number) for number in re.findall(r"\d+", value)]
    if not numbers:
        return None
    return min(numbers), max(numbers)


def is_top_eight(value: str) -> bool:
    rank = parse_rank(value)
    return bool(rank and rank[1] <= 8)


def parse_listing_page(document: str) -> list[ListingDeck]:
    parser = ListingParser()
    parser.feed(document)
    decks: list[ListingDeck] = []

    for cells in parser.rows:
        if len(cells) < 7:
            continue
        deck_href = next(
            (href for href in cells[1]["hrefs"] if re.search(r"(?:^|[?&])d=\d+", href)),
            "",
        )
        if not deck_href:
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlparse(deck_href).query)
        if not query.get("d") or not query.get("e"):
            continue
        try:
            event_date = datetime.strptime(clean_text("".join(cells[6]["text"])), "%d/%m/%y").date()
        except ValueError:
            continue
        decks.append(
            ListingDeck(
                deck_id=query["d"][0],
                event_id=query["e"][0],
                deck_name=clean_text("".join(cells[1]["text"])),
                player=clean_text("".join(cells[2]["text"])),
                event_name=clean_text("".join(cells[3]["text"])),
                level=int(cells[4]["stars"]),
                rank=clean_text("".join(cells[5]["text"])),
                event_date=event_date,
            )
        )
    return decks


def parse_event_page(document: str) -> EventDeck:
    parser = EventParser()
    parser.feed(document)
    players_match = re.search(r"(\d+)\s+players?\s*-\s*\d{2}/\d{2}/\d{2}", document, flags=re.IGNORECASE)
    if re.search(r"/graph/online/paper\.png[^>]+title=[\"']?Paper", document, flags=re.IGNORECASE):
        platform = "Paper"
    elif re.search(r"/graph/online/mtgo\.png[^>]+title=[\"']?MTG Online", document, flags=re.IGNORECASE):
        platform = "MTGO"
    else:
        platform = "unknown"
    return EventDeck(
        players=int(players_match.group(1)) if players_match else None,
        platform=platform,
        cards=parser.cards,
    )


def parse_local_deck(path: Path, commander: str) -> list[DeckCard]:
    cards: list[DeckCard] = []
    commander_key = normalize_name(commander)
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.casefold() in {"sideboard", "maybeboard"}:
            continue
        match = re.match(r"^(\d+)\s*x?\s+(.+)$", line, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"Neplatný řádek {line_no} v {path}: {raw_line}")
        quantity = int(match.group(1))
        name = match.group(2).strip()
        section = "commander" if normalize_name(name) == commander_key else "unknown"
        cards.append(DeckCard(quantity, name, section))
    return cards


def parse_cached_deck(path: Path) -> list[DeckCard]:
    cards: list[DeckCard] = []
    section = "unknown"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = re.match(r"^\[([A-Z_]+)\]$", line)
        if section_match:
            section = section_match.group(1).casefold()
            continue
        card_match = re.match(r"^(\d+)\s+(.+)$", line)
        if card_match:
            cards.append(DeckCard(int(card_match.group(1)), card_match.group(2).strip(), section))
    return cards


def safe_metadata(value: str) -> str:
    return clean_text(value).replace("\n", " ")


def write_cached_deck(path: Path, record: DeckRecord, cards: list[DeckCard], commander: str) -> None:
    grouped: dict[str, list[DeckCard]] = defaultdict(list)
    for card in cards:
        grouped[card.section].append(card)

    lines = [
        f"# MTGTop8 deck {record.deck_id}",
        f"# Commander: {safe_metadata(commander)}",
        f"# Player: {safe_metadata(record.player)}",
        f"# Event: {safe_metadata(record.event_name)}",
        f"# Event date: {record.event_date}",
        f"# Players: {record.players if record.players is not None else 'unknown'}",
        f"# Rank: {safe_metadata(record.rank)}",
        f"# MTGTop8 level: {record.level}",
        f"# Platform: {safe_metadata(record.platform)}",
        f"# Source: {record.url}",
        f"# Downloaded at: {record.downloaded_at}",
        "",
    ]
    for section in SECTION_ORDER:
        section_cards = grouped.get(section, [])
        if not section_cards:
            continue
        lines.append(f"[{section.upper()}]")
        lines.extend(f"{card.quantity} {card.name}" for card in section_cards)
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def default_state(commander: str, archetype_id: int, since: date) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "commander": commander,
        "archetype_id": archetype_id,
        "analysis_since": since.isoformat(),
        "last_successful_analysis_date": None,
        "last_analysis_at": None,
        "decks": {},
    }


def load_state(path: Path, commander: str, archetype_id: int, since: date) -> dict[str, Any]:
    if not path.exists():
        return default_state(commander, archetype_id, since)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != STATE_VERSION:
        raise ValueError(f"Nepodporovaná verze cache v {path}")
    if normalize_name(str(value.get("commander") or "")) != normalize_name(commander):
        raise ValueError(f"Cache {path.parent} patří jinému commanderovi")
    if int(value.get("archetype_id") or 0) != archetype_id:
        raise ValueError(f"Cache {path.parent} patří jinému MTGTop8 archetypu")
    value.setdefault("decks", {})
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def listing_url(archetype_id: int, page: int) -> str:
    query = urllib.parse.urlencode(
        {"a": archetype_id, "f": DEFAULT_FORMAT, "meta": 56, "current_page": page}
    )
    return urllib.parse.urljoin(MTGTOP8_BASE_URL, f"archetype.php?{query}")


def discover_decks(fetcher: Fetcher, archetype_id: int, since: date, max_pages: int) -> tuple[list[ListingDeck], int]:
    discovered: list[ListingDeck] = []
    seen_ids: set[str] = set()
    pages_read = 0

    for page in range(1, max_pages + 1):
        rows = parse_listing_page(fetcher.get_text(listing_url(archetype_id, page)))
        pages_read += 1
        if not rows:
            break
        page_ids = {row.deck_id for row in rows}
        if page_ids and page_ids.issubset(seen_ids):
            break
        for row in rows:
            seen_ids.add(row.deck_id)
            if row.event_date >= since and is_top_eight(row.rank):
                discovered.append(row)
        dated_rows = [row.event_date for row in rows]
        if dated_rows and max(dated_rows) < since:
            break
    else:
        raise RuntimeError(f"Dosažen limit {max_pages} stránek MTGTop8; zvyšte --max-pages")

    return discovered, pages_read


def validate_event_deck(event_deck: EventDeck, commander: str) -> None:
    commander_key = normalize_name(commander)
    commanders = [card for card in event_deck.cards if card.section == "commander"]
    if not any(normalize_name(card.name) == commander_key for card in commanders):
        raise ValueError(f"deck neobsahuje očekávaného commandera {commander!r}")
    card_count = sum(card.quantity for card in event_deck.cards)
    if card_count < 90:
        raise ValueError(f"decklist vypadá neúplně ({card_count} karet)")


def record_for_listing(listing: ListingDeck, event_deck: EventDeck, cache_file: Path) -> DeckRecord:
    return DeckRecord(
        deck_id=listing.deck_id,
        event_id=listing.event_id,
        deck_name=listing.deck_name,
        player=listing.player,
        event_name=listing.event_name,
        level=listing.level,
        rank=listing.rank,
        event_date=listing.event_date.isoformat(),
        players=event_deck.players,
        platform=event_deck.platform,
        url=listing.url,
        cache_file=str(cache_file),
        card_count=sum(card.quantity for card in event_deck.cards),
        downloaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def update_cache(
    state: dict[str, Any],
    state_path: Path,
    cache_dir: Path,
    fetcher: Fetcher,
    commander: str,
    archetype_id: int,
    fetch_since: date,
    max_pages: int,
) -> tuple[int, int, list[str]]:
    discovered, pages_read = discover_decks(fetcher, archetype_id, fetch_since, max_pages)
    existing = state["decks"]
    downloaded = 0
    failures: list[str] = []

    for index, listing in enumerate(discovered, start=1):
        cached = existing.get(listing.deck_id)
        cached_path = Path(cached["cache_file"]) if cached else cache_dir / "decks" / f"{listing.deck_id}.txt"
        if cached and cached_path.exists():
            cached.update(
                {
                    "deck_name": listing.deck_name,
                    "player": listing.player,
                    "event_name": listing.event_name,
                    "level": listing.level,
                    "rank": listing.rank,
                    "event_date": listing.event_date.isoformat(),
                    "url": listing.url,
                }
            )
            write_cached_deck(cached_path, DeckRecord.from_dict(cached), parse_cached_deck(cached_path), commander)
            continue

        print(
            f"[{index}/{len(discovered)}] Stahuji {listing.event_date.isoformat()} – "
            f"{listing.player} ({listing.rank})",
            flush=True,
        )
        try:
            event_deck = parse_event_page(fetcher.get_text(listing.url))
            validate_event_deck(event_deck, commander)
            record = record_for_listing(listing, event_deck, cached_path)
            write_cached_deck(cached_path, record, event_deck.cards, commander)
            existing[listing.deck_id] = asdict(record)
            write_json(state_path, state)
            downloaded += 1
        except (OSError, ValueError, urllib.error.URLError) as exc:
            failures.append(f"{listing.deck_id} ({listing.player}): {exc}")

    return downloaded, pages_read, failures


def tournament_weight(record: DeckRecord) -> float:
    players = record.players if record.players and record.players > 0 else 8
    size_factor = math.sqrt(max(players, 8) / 8)
    rank = parse_rank(record.rank)
    high = rank[1] if rank else 8
    if high <= 1:
        placement_factor = 1.6
    elif high <= 2:
        placement_factor = 1.4
    elif high <= 4:
        placement_factor = 1.2
    else:
        placement_factor = 1.0
    level_factor = 1.0 + 0.1 * max(0, record.level - 1)
    return size_factor * placement_factor * level_factor


def type_section(type_line: str) -> str:
    types = type_line.partition("—")[0].partition("-")[0]
    if "Land" in types:
        return "lands"
    if "Creature" in types:
        return "creatures"
    if "Instant" in types or "Sorcery" in types:
        return "spells"
    if type_line:
        return "other"
    return "unknown"


def card_type_group(stat: CardStat) -> str:
    if stat.section == "commander":
        return "commander"
    card_types = stat.type_line.partition("—")[0].partition("-")[0]
    if "Land" in card_types:
        return "lands"
    if "Creature" in card_types:
        return "creatures"
    if "Instant" in card_types:
        return "instants"
    if "Sorcery" in card_types:
        return "sorceries"
    if "Planeswalker" in card_types:
        return "planeswalkers"
    if "Battle" in card_types:
        return "battles"
    if "Artifact" in card_types:
        return "artifacts"
    if "Enchantment" in card_types:
        return "enchantments"
    if stat.section == "lands":
        return "lands"
    if stat.section == "creatures":
        return "creatures"
    return "other"


def scryfall_image(card: dict[str, Any]) -> str:
    image_uris = card.get("image_uris") or {}
    if isinstance(image_uris, dict):
        image = image_uris.get("normal") or image_uris.get("small")
        if image:
            return str(image)
    for face in card.get("card_faces") or []:
        face_images = face.get("image_uris") or {}
        if isinstance(face_images, dict) and (face_images.get("normal") or face_images.get("small")):
            return str(face_images.get("normal") or face_images.get("small"))
    return ""


def load_scryfall_cache(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def enrich_from_scryfall(names: Iterable[str], cache_path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    metadata = load_scryfall_cache(cache_path)
    display_by_key = {normalize_name(name): name for name in names}
    missing = sorted(
        key
        for key in display_by_key
        if key not in metadata
        or not metadata[key].get("type_line")
        or not metadata[key].get("image_url")
    )
    failures: list[str] = []

    for batch in chunks(missing, 75):
        payload = json.dumps({"identifiers": [{"name": display_by_key[key]} for key in batch]}).encode("utf-8")
        request = urllib.request.Request(
            SCRYFALL_COLLECTION_URL,
            data=payload,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json;q=0.9,*/*;q=0.8",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            for card in result.get("data") or []:
                candidates: dict[str, str] = {
                    normalize_name(str(card.get("name") or "")): str(card.get("type_line") or "")
                }
                for face in card.get("card_faces") or []:
                    candidates[normalize_name(str(face.get("name") or ""))] = str(face.get("type_line") or "")
                for key in batch:
                    if key not in candidates:
                        continue
                    metadata[key] = {
                        "name": display_by_key[key],
                        "type_line": candidates[key],
                        "image_url": scryfall_image(card),
                        "scryfall_url": str(card.get("scryfall_uri") or ""),
                    }
            for identifier in result.get("not_found") or []:
                missing_name = str(identifier.get("name") or "")
                if missing_name:
                    failures.append(missing_name)
                    metadata.setdefault(normalize_name(missing_name), {})
            write_json(cache_path, metadata)
            time.sleep(0.12)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            failures.extend(f"{display_by_key[key]} ({exc})" for key in batch)
            break
    return metadata, failures


def build_card_stats(
    samples: list[tuple[DeckRecord, list[DeckCard]]],
    current_cards: list[DeckCard],
    scryfall: dict[str, dict[str, str]],
) -> list[CardStat]:
    current_quantities: dict[str, int] = defaultdict(int)
    current_sections: dict[str, str] = {}
    display_names: dict[str, Counter[str]] = defaultdict(Counter)
    deck_counts: Counter[str] = Counter()
    weighted_counts: Counter[str] = Counter()
    section_weights: dict[str, Counter[str]] = defaultdict(Counter)
    total_weight = sum(tournament_weight(record) for record, _ in samples)

    for card in current_cards:
        key = normalize_name(card.name)
        current_quantities[key] += card.quantity
        current_sections[key] = card.section
        display_names[key][card.name] += card.quantity

    for record, cards in samples:
        weight = tournament_weight(record)
        present: dict[str, DeckCard] = {}
        for card in cards:
            key = normalize_name(card.name)
            display_names[key][card.name] += 1
            present.setdefault(key, card)
        for key, card in present.items():
            deck_counts[key] += 1
            weighted_counts[key] += weight
            section_weights[key][card.section] += weight

    keys = set(current_quantities) | set(deck_counts)
    stats: list[CardStat] = []
    for key in keys:
        card_meta = scryfall.get(key) or {}
        name = str(card_meta.get("name") or display_names[key].most_common(1)[0][0])
        section = type_section(str(card_meta.get("type_line") or ""))
        if current_sections.get(key) == "commander" or section_weights[key].get("commander"):
            section = "commander"
        elif section == "unknown" and section_weights[key]:
            section = section_weights[key].most_common(1)[0][0]
        stats.append(
            CardStat(
                key=key,
                name=name,
                section=section,
                decks=deck_counts[key],
                raw_pct=(100.0 * deck_counts[key] / len(samples)) if samples else 0.0,
                weighted_pct=(100.0 * weighted_counts[key] / total_weight) if total_weight else 0.0,
                current_quantity=current_quantities[key],
                type_line=str(card_meta.get("type_line") or ""),
                image_url=str(card_meta.get("image_url") or ""),
                scryfall_url=str(card_meta.get("scryfall_url") or ""),
            )
        )
    return stats


def suggest_swaps(stats: list[CardStat], sample_count: int, limit: int = 12) -> list[SwapSuggestion]:
    minimum_decks = max(3, math.ceil(sample_count * 0.10))
    additions: dict[str, list[CardStat]] = defaultdict(list)
    removals: dict[str, list[CardStat]] = defaultdict(list)
    for stat in stats:
        if stat.section in {"commander", "unknown"}:
            continue
        if not stat.current_quantity and stat.decks >= minimum_decks and stat.weighted_pct >= 45.0:
            additions[stat.section].append(stat)
        elif stat.current_quantity:
            removals[stat.section].append(stat)

    suggestions: list[SwapSuggestion] = []
    for section in SECTION_ORDER:
        ins = sorted(additions[section], key=lambda item: (-item.weighted_pct, item.name.casefold()))
        outs = sorted(removals[section], key=lambda item: (item.weighted_pct, item.name.casefold()))
        for remove, add in zip(outs, ins):
            suggestion = SwapSuggestion(remove, add)
            if suggestion.gain >= 15.0:
                suggestions.append(suggestion)
    return sorted(suggestions, key=lambda item: (-item.gain, item.add.name.casefold()))[:limit]


def fmt_pct(value: float) -> str:
    return f"{value:.1f} %"


def render_report(
    path: Path,
    commander: str,
    current_deck_path: Path,
    analysis_since: date,
    generated_at: datetime,
    samples: list[tuple[DeckRecord, list[DeckCard]]],
    current_cards: list[DeckCard],
    stats: list[CardStat],
    suggestions: list[SwapSuggestion],
    warnings: list[str],
) -> None:
    current_total = sum(card.quantity for card in current_cards)
    dates = [date.fromisoformat(record.event_date) for record, _ in samples]
    total_weight = sum(tournament_weight(record) for record, _ in samples)
    average_players_values = [record.players for record, _ in samples if record.players]
    average_players = (
        sum(average_players_values) / len(average_players_values) if average_players_values else 0.0
    )

    suggestion_rows: list[str] = []
    for suggestion in suggestions:
        suggestion_rows.append(
            "<tr>"
            f'<td><span class="minus">−</span> {card_link(suggestion.remove)}</td>'
            f'<td class="number">{fmt_pct(suggestion.remove.weighted_pct)}</td>'
            f'<td><span class="plus">+</span> {card_link(suggestion.add)}</td>'
            f'<td class="number">{fmt_pct(suggestion.add.weighted_pct)}</td>'
            f'<td class="number gain">+{fmt_pct(suggestion.gain)}</td>'
            "</tr>"
        )
    if not suggestion_rows:
        suggestion_rows.append('<tr><td colspan="5" class="empty">Pro nastavené prahy nevznikla žádná rozumná statistická výměna.</td></tr>')

    rows_by_type: dict[str, list[str]] = defaultdict(list)
    for stat in sorted(stats, key=lambda item: (-item.weighted_pct, item.name.casefold())):
        owned = stat.current_quantity > 0
        status = f"V balíku ({stat.current_quantity}×)" if owned else "Chybí"
        image = html.escape(stat.image_url, quote=True)
        type_group = card_type_group(stat)
        rows_by_type[type_group].append(
            f'<tr class="card-row {"owned" if owned else "missing"}" '
            f'data-key="{html.escape(stat.key, quote=True)}" '
            f'data-status="{"owned" if owned else "missing"}" data-category="{html.escape(stat.section)}" '
            f'data-type-group="{html.escape(type_group)}" data-name="{html.escape(normalize_name(stat.name), quote=True)}" '
            f'data-image="{image}" data-count="{stat.decks}" data-weighted="{stat.weighted_pct:.8f}">'
            f'<td>{card_link(stat)}<small>{html.escape(stat.type_line or SECTION_LABELS.get(stat.section, stat.section))}</small></td>'
            f'<td><span class="status {"yes" if owned else "no"}">{html.escape(status)}</span></td>'
            f'<td class="number strong stat-weighted">{fmt_pct(stat.weighted_pct)}</td>'
            f'<td class="number stat-raw">{fmt_pct(stat.raw_pct)}</td>'
            f'<td class="number stat-count">{stat.decks} / {len(samples)}</td>'
            "</tr>"
        )

    card_groups: list[str] = []
    for type_group in TYPE_GROUP_ORDER:
        rows = rows_by_type.get(type_group, [])
        if not rows:
            continue
        card_groups.append(
            f'<tbody class="card-group" data-type-group="{html.escape(type_group)}">'
            f'<tr class="group-heading"><th colspan="5" scope="rowgroup">'
            f'<span>{html.escape(TYPE_GROUP_LABELS[type_group])}</span>'
            f'<span class="group-count">{len(rows)}</span></th></tr>{"".join(rows)}</tbody>'
        )

    event_rows = []
    for record, _ in sorted(samples, key=lambda item: (-tournament_weight(item[0]), item[0].event_date)):
        players = str(record.players) if record.players else "?"
        event_rows.append(
            f'<tr class="event-row" data-date="{html.escape(record.event_date)}">'
            f'<td><a href="{html.escape(record.url, quote=True)}">{html.escape(record.event_name)}</a>'
            f'<small>{html.escape(record.player)}</small></td>'
            f'<td>{html.escape(record.event_date)}</td><td class="number">{html.escape(record.rank)}</td>'
            f'<td class="number">{players}</td><td class="number">{record.level}</td>'
            f'<td class="number">{tournament_weight(record):.2f}</td>'
            "</tr>"
        )

    warning_html = ""
    if warnings:
        warning_html = '<aside class="warnings"><strong>Upozornění</strong><ul>' + "".join(
            f"<li>{html.escape(warning)}</li>" for warning in warnings
        ) + "</ul></aside>"

    date_range = "bez dat"
    if dates:
        date_range = f"{min(dates).isoformat()} až {max(dates).isoformat()}"

    payload = {
        "analysisSince": analysis_since.isoformat(),
        "generatedDate": generated_at.date().isoformat(),
        "cards": {
            stat.key: {
                "name": stat.name,
                "section": stat.section,
                "typeGroup": card_type_group(stat),
                "currentQuantity": stat.current_quantity,
                "imageUrl": stat.image_url,
                "scryfallUrl": stat.scryfall_url,
            }
            for stat in stats
        },
        "samples": [
            {
                "date": record.event_date,
                "players": record.players,
                "weight": tournament_weight(record),
                "cards": sorted({normalize_name(card.name) for card in cards}),
            }
            for record, cards in samples
        ],
    }
    analysis_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    analysis_json = analysis_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    client_script = r"""
    const analysis = JSON.parse(document.querySelector('#analysis-data').textContent);
    const rows = [...document.querySelectorAll('.card-row')];
    const groups = [...document.querySelectorAll('.card-group')];
    const rowByKey = new Map(rows.map(row => [row.dataset.key, row]));
    const search = document.querySelector('#search');
    const statusButtons = [...document.querySelectorAll('.status-filter')];
    const dateButtons = [...document.querySelectorAll('.date-filter')];
    const sinceInput = document.querySelector('#since-date');
    const suggestionBody = document.querySelector('#suggestions');
    const preview = document.querySelector('#preview');
    let statusFilter = 'all';
    let selectedSince = analysis.analysisSince;

    function normalize(value) {
      return value.toLocaleLowerCase('cs').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[’`]/g, "'").trim();
    }

    function formatPercent(value) {
      return `${value.toFixed(1)} %`;
    }

    function shiftDays(isoDate, amount) {
      const value = new Date(`${isoDate}T00:00:00Z`);
      value.setUTCDate(value.getUTCDate() + amount);
      return value.toISOString().slice(0, 10);
    }

    function maxDate(first, second) {
      return first > second ? first : second;
    }

    function calculate(selectedSamples) {
      const counts = new Map();
      const weighted = new Map();
      const totalWeight = selectedSamples.reduce((sum, sample) => sum + sample.weight, 0);
      selectedSamples.forEach(sample => sample.cards.forEach(key => {
        counts.set(key, (counts.get(key) || 0) + 1);
        weighted.set(key, (weighted.get(key) || 0) + sample.weight);
      }));
      return {counts, weighted, totalWeight};
    }

    function currentStats(key, totals, sampleCount) {
      const count = totals.counts.get(key) || 0;
      return {
        count,
        raw: sampleCount ? 100 * count / sampleCount : 0,
        weighted: totals.totalWeight ? 100 * (totals.weighted.get(key) || 0) / totals.totalWeight : 0,
      };
    }

    function updateSummary(selectedSamples, totals) {
      const knownPlayers = selectedSamples.map(sample => sample.players).filter(value => Number.isFinite(value) && value > 0);
      const averagePlayers = knownPlayers.length ? knownPlayers.reduce((sum, value) => sum + value, 0) / knownPlayers.length : 0;
      const dates = selectedSamples.map(sample => sample.date).sort();
      document.querySelector('#sample-count').textContent = selectedSamples.length;
      document.querySelector('#average-players').textContent = averagePlayers.toFixed(1);
      document.querySelector('#weight-total').textContent = totals.totalWeight.toFixed(1);
      document.querySelector('#date-range').textContent = dates.length ? `${dates[0]} až ${dates[dates.length - 1]}` : 'bez nalezených turnajů';
      document.querySelector('#event-count').textContent = selectedSamples.length;
      document.querySelectorAll('.event-row').forEach(row => { row.hidden = row.dataset.date < selectedSince; });
    }

    function updateCards(selectedSamples, totals) {
      rows.forEach(row => {
        const stat = currentStats(row.dataset.key, totals, selectedSamples.length);
        row.dataset.count = stat.count;
        row.dataset.weighted = stat.weighted;
        row.querySelector('.stat-weighted').textContent = formatPercent(stat.weighted);
        row.querySelector('.stat-raw').textContent = formatPercent(stat.raw);
        row.querySelector('.stat-count').textContent = `${stat.count} / ${selectedSamples.length}`;
      });
      groups.forEach(group => {
        [...group.querySelectorAll('.card-row')]
          .sort((left, right) => Number(right.dataset.weighted) - Number(left.dataset.weighted) || left.dataset.name.localeCompare(right.dataset.name, 'cs'))
          .forEach(row => group.append(row));
      });
    }

    function applyVisibility() {
      const query = normalize(search.value);
      rows.forEach(row => {
        const statusOk = statusFilter === 'all' || row.dataset.status === statusFilter;
        const periodOk = row.dataset.status === 'owned' || Number(row.dataset.count) > 0;
        row.hidden = !(statusOk && periodOk && row.dataset.name.includes(query));
      });
      groups.forEach(group => {
        const visibleRows = [...group.querySelectorAll('.card-row')].filter(row => !row.hidden);
        group.hidden = visibleRows.length === 0;
        group.querySelector('.group-count').textContent = visibleRows.length;
      });
    }

    function appendCard(cell, card, sign, signClass) {
      const marker = document.createElement('span');
      marker.className = signClass;
      marker.textContent = sign;
      cell.append(marker, ' ');
      const link = document.createElement(card.scryfallUrl ? 'a' : 'span');
      if (card.scryfallUrl) link.href = card.scryfallUrl;
      link.textContent = card.name;
      link.className = 'previewable';
      link.dataset.image = card.imageUrl || '';
      link.dataset.cardName = card.name;
      cell.append(link);
    }

    function renderSuggestions(selectedSamples, totals) {
      const minimumDecks = Math.max(3, Math.ceil(selectedSamples.length * 0.10));
      const additions = new Map();
      const removals = new Map();
      const dynamicStats = new Map();
      Object.entries(analysis.cards).forEach(([key, card]) => {
        const stat = currentStats(key, totals, selectedSamples.length);
        dynamicStats.set(key, stat);
        if (card.section === 'commander' || card.section === 'unknown') return;
        if (!card.currentQuantity && stat.count >= minimumDecks && stat.weighted >= 45) {
          if (!additions.has(card.section)) additions.set(card.section, []);
          additions.get(card.section).push({key, card, stat});
        } else if (card.currentQuantity) {
          if (!removals.has(card.section)) removals.set(card.section, []);
          removals.get(card.section).push({key, card, stat});
        }
      });

      const suggestions = [];
      additions.forEach((ins, section) => {
        const outs = removals.get(section) || [];
        ins.sort((a, b) => b.stat.weighted - a.stat.weighted || a.card.name.localeCompare(b.card.name, 'cs'));
        outs.sort((a, b) => a.stat.weighted - b.stat.weighted || a.card.name.localeCompare(b.card.name, 'cs'));
        for (let index = 0; index < Math.min(ins.length, outs.length); index += 1) {
          const gain = ins[index].stat.weighted - outs[index].stat.weighted;
          if (gain >= 15) suggestions.push({remove: outs[index], add: ins[index], gain});
        }
      });
      suggestions.sort((a, b) => b.gain - a.gain || a.add.card.name.localeCompare(b.add.card.name, 'cs'));
      suggestionBody.replaceChildren();
      suggestions.slice(0, 12).forEach(suggestion => {
        const row = document.createElement('tr');
        const removeCard = document.createElement('td');
        const removePercent = document.createElement('td');
        const addCard = document.createElement('td');
        const addPercent = document.createElement('td');
        const gain = document.createElement('td');
        appendCard(removeCard, suggestion.remove.card, '−', 'minus');
        appendCard(addCard, suggestion.add.card, '+', 'plus');
        removePercent.className = 'number';
        addPercent.className = 'number';
        gain.className = 'number gain';
        removePercent.textContent = formatPercent(suggestion.remove.stat.weighted);
        addPercent.textContent = formatPercent(suggestion.add.stat.weighted);
        gain.textContent = `+${formatPercent(suggestion.gain)}`;
        row.append(removeCard, removePercent, addCard, addPercent, gain);
        suggestionBody.append(row);
      });
      if (!suggestionBody.children.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 5;
        cell.className = 'empty';
        cell.textContent = 'Pro zvolené období nevznikla žádná rozumná statistická výměna.';
        row.append(cell);
        suggestionBody.append(row);
      }
    }

    function selectPeriod(since) {
      selectedSince = maxDate(since, analysis.analysisSince);
      sinceInput.value = selectedSince;
      const selectedSamples = analysis.samples.filter(sample => sample.date >= selectedSince);
      const totals = calculate(selectedSamples);
      updateSummary(selectedSamples, totals);
      updateCards(selectedSamples, totals);
      renderSuggestions(selectedSamples, totals);
      applyVisibility();
    }

    function showPreview(target) {
      const image = target.dataset.image;
      if (!image) return;
      preview.src = image;
      preview.alt = target.dataset.cardName || target.dataset.name || 'Náhled karty';
      preview.classList.add('visible');
    }

    function hidePreview() {
      preview.classList.remove('visible');
    }

    search.addEventListener('input', applyVisibility);
    statusButtons.forEach(button => button.addEventListener('click', () => {
      statusFilter = button.dataset.filter;
      statusButtons.forEach(item => item.classList.toggle('active', item === button));
      applyVisibility();
    }));
    dateButtons.forEach(button => button.addEventListener('click', () => {
      const range = button.dataset.range;
      let since = analysis.analysisSince;
      if (range === '30') since = shiftDays(analysis.generatedDate, -29);
      if (range === '90') since = shiftDays(analysis.generatedDate, -89);
      if (range === 'year') since = `${analysis.generatedDate.slice(0, 4)}-01-01`;
      dateButtons.forEach(item => item.classList.toggle('active', item === button));
      selectPeriod(since);
    }));
    sinceInput.addEventListener('change', () => {
      dateButtons.forEach(item => item.classList.remove('active'));
      selectPeriod(sinceInput.value || analysis.analysisSince);
    });
    document.addEventListener('pointerover', event => {
      const target = event.target.closest('.previewable, .card-row');
      if (target) showPreview(target);
    });
    document.addEventListener('pointerout', event => {
      const target = event.target.closest('.previewable, .card-row');
      if (target && !target.contains(event.relatedTarget)) hidePreview();
    });
    document.addEventListener('focusin', event => {
      const target = event.target.closest('.previewable, .card-row');
      if (target) showPreview(target);
    });
    document.addEventListener('focusout', hidePreview);
    selectPeriod(analysis.analysisSince);
    """

    document = f"""<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(commander)} – MTGTop8 analýza</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f1ea; --panel:#fffdf8; --ink:#1f2925; --muted:#68726d; --line:#d9d4c8; --green:#246b4b; --green-soft:#e5f2e9; --red:#9f3f3f; --red-soft:#faeaea; --gold:#a8751d; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
    a {{ color:var(--green); text-decoration-thickness:1px; text-underline-offset:2px; }}
    header {{ padding:44px max(20px,calc((100vw - 1180px)/2)); background:#173d2d; color:white; }}
    header p {{ max-width:760px; margin:8px 0 0; color:#d9e9df; }}
    h1 {{ margin:0; font-size:clamp(28px,4vw,44px); line-height:1.05; }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:28px auto 64px; }}
    h2 {{ margin:34px 0 12px; font-size:24px; }}
    h3 {{ margin:0; }}
    .summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
    .metric,.panel,.warnings {{ border:1px solid var(--line); border-radius:12px; background:var(--panel); box-shadow:0 8px 25px rgba(27,38,33,.05); }}
    .metric {{ padding:18px; }}
    .metric strong {{ display:block; font-size:28px; line-height:1.1; color:var(--green); }}
    .metric span {{ display:block; margin-top:5px; color:var(--muted); font-size:13px; }}
    .panel {{ overflow:hidden; }}
    .panel-note {{ margin:0 0 14px; color:var(--muted); }}
    .warnings {{ margin:16px 0; padding:14px 18px; border-color:#e5c78e; background:#fff7e8; }}
    .warnings ul {{ margin:7px 0 0; padding-left:20px; }}
    .controls {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; align-items:center; }}
    .controls input {{ flex:1 1 260px; min-height:40px; padding:8px 11px; border:1px solid var(--line); border-radius:8px; font:inherit; background:white; }}
    .date-controls {{ margin:18px 0 8px; padding:14px; border:1px solid var(--line); border-radius:12px; background:rgba(255,253,248,.7); }}
    .date-controls label {{ display:flex; align-items:center; gap:8px; color:var(--muted); font-weight:700; }}
    .date-controls input {{ flex:0 0 auto; min-height:40px; }}
    button {{ min-height:40px; padding:8px 12px; border:1px solid var(--line); border-radius:8px; background:white; color:var(--ink); font:inherit; font-weight:700; cursor:pointer; }}
    button.active {{ border-color:var(--green); background:var(--green); color:white; }}
    .table-wrap {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; }}
    th {{ padding:10px 12px; background:#eeeae1; color:#4e5853; text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    td {{ padding:10px 12px; border-top:1px solid #ebe6dc; vertical-align:middle; }}
    td small {{ display:block; color:var(--muted); font-size:12px; }}
    .number {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
    .strong {{ font-weight:800; color:var(--green); }}
    .gain {{ color:var(--green); font-weight:800; }}
    .minus {{ color:var(--red); font-weight:900; }} .plus {{ color:var(--green); font-weight:900; }}
    .status {{ display:inline-block; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:750; white-space:nowrap; }}
    .status.yes {{ color:var(--green); background:var(--green-soft); }} .status.no {{ color:var(--muted); background:#eeece7; }}
    .card-row.owned {{ background:#fbfefc; }}
    .group-heading th {{ padding:11px 12px; border-top:2px solid var(--line); background:#e3e9e3; color:#294d3c; font-size:13px; }}
    .group-heading:first-child th {{ border-top:0; }}
    .group-heading th {{ display:flex; justify-content:space-between; gap:16px; }}
    .group-count {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
    .empty {{ padding:24px; color:var(--muted); text-align:center; }}
    details {{ margin-top:16px; }} details summary {{ cursor:pointer; font-weight:750; color:var(--green); }}
    .method {{ margin-top:36px; padding:20px; border-left:4px solid var(--gold); background:#fff9eb; }}
    .method p {{ margin:7px 0; }}
    .card-preview {{ position:fixed; z-index:5; right:18px; bottom:18px; width:244px; border-radius:12px; box-shadow:0 18px 48px rgba(0,0,0,.28); display:none; pointer-events:none; }}
    .card-preview.visible {{ display:block; }}
    @media (max-width:760px) {{ .summary {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} th,td {{ padding:9px 8px; }} .date-controls label {{ width:100%; justify-content:space-between; }} .card-preview {{ display:none!important; }} }}
    @media print {{ header {{ padding:20px; }} main {{ width:100%; margin:16px 0; }} .controls,.card-preview,details {{ display:none!important; }} .panel {{ box-shadow:none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(commander)}</h1>
    <p>Porovnání tvého Duel Commander balíku s top 8 decklisty z MTGTop8. Vygenerováno {generated_at.astimezone().strftime('%Y-%m-%d %H:%M %Z')}.</p>
  </header>
  <main>
    <section class="summary">
      <div class="metric"><strong id="sample-count">{len(samples)}</strong><span>top 8 decků ve výběru</span></div>
      <div class="metric"><strong>{current_total}</strong><span>karet v {html.escape(str(current_deck_path))}</span></div>
      <div class="metric"><strong id="average-players">{average_players:.1f}</strong><span>průměr hráčů na turnaji</span></div>
      <div class="metric"><strong id="weight-total">{total_weight:.1f}</strong><span>součet vah vzorku</span></div>
    </section>
    <div class="controls date-controls" aria-label="Období analýzy">
      <button class="date-filter active" data-range="all" type="button">Vše</button>
      <button class="date-filter" data-range="30" type="button">Posledních 30 dní</button>
      <button class="date-filter" data-range="90" type="button">Posledních 90 dní</button>
      <button class="date-filter" data-range="year" type="button">Aktuální rok</button>
      <label>Od data <input id="since-date" type="date" min="{analysis_since.isoformat()}" max="{generated_at.date().isoformat()}" value="{analysis_since.isoformat()}"></label>
    </div>
    <p class="panel-note">Analyzované období: <strong id="date-range">{date_range}</strong>; nejstarší data v reportu jsou od {analysis_since.isoformat()}.</p>
    {warning_html}

    <h2>Statistické návrhy výměn</h2>
    <p class="panel-note">Párují se jen karty stejného širokého typu. Jde o rozdíl v hranosti, ne o posouzení synergií, matchupů nebo lokální mety.</p>
    <section class="panel table-wrap">
      <table><thead><tr><th>Ven</th><th class="number">Hranost</th><th>Dovnitř</th><th class="number">Hranost</th><th class="number">Rozdíl</th></tr></thead>
      <tbody id="suggestions">{''.join(suggestion_rows)}</tbody></table>
    </section>

    <h2>Všechny karty ve vzorku</h2>
    <div class="controls">
      <input id="search" type="search" placeholder="Hledat kartu…" aria-label="Hledat kartu">
      <button class="status-filter active" data-filter="all" type="button">Vše</button>
      <button class="status-filter" data-filter="owned" type="button">Mám v balíku</button>
      <button class="status-filter" data-filter="missing" type="button">Chybí</button>
    </div>
    <section class="panel table-wrap">
      <table><thead><tr><th>Karta</th><th>Stav</th><th class="number">Vážená hranost</th><th class="number">Prostá hranost</th><th class="number">Decky</th></tr></thead>
      {''.join(card_groups)}</table>
    </section>

    <details>
      <summary>Turnaje zahrnuté do analýzy (<span id="event-count">{len(samples)}</span>)</summary>
      <section class="panel table-wrap"><table><thead><tr><th>Turnaj / hráč</th><th>Datum</th><th class="number">Pořadí</th><th class="number">Hráčů</th><th class="number">Level</th><th class="number">Váha</th></tr></thead><tbody>{''.join(event_rows)}</tbody></table></section>
    </details>

    <section class="method">
      <h3>Jak se počítá váha</h3>
      <p><code>√(max(počet hráčů, 8) / 8) × umístění × úroveň MTGTop8</code>.</p>
      <p>Koeficient umístění je 1,6 pro vítěze, 1,4 pro druhé místo, 1,2 pro 3.–4. místo a 1,0 pro 5.–8. místo. Úroveň MTGTop8 přidává 10 % za každou hvězdu nad první. Vážená hranost je podíl součtu vah decků s kartou vůči všem deckům.</p>
      <p>Zdroj decklistů: <a href="{html.escape(listing_url(DEFAULT_ARCHETYPE_ID, 1), quote=True)}">MTGTop8 – {html.escape(commander)}</a>. Data jsou lokálně cachována po jednotlivých decklistech.</p>
    </section>
  </main>
  <img class="card-preview" id="preview" alt="">
  <script id="analysis-data" type="application/json">{analysis_json}</script>
  <script>
{client_script}
  </script>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def card_link(stat: CardStat) -> str:
    label = html.escape(stat.name)
    attributes = (
        f'class="previewable" data-image="{html.escape(stat.image_url, quote=True)}" '
        f'data-card-name="{html.escape(stat.name, quote=True)}"'
    )
    if not stat.scryfall_url:
        return f"<span {attributes}>{label}</span>"
    return f'<a href="{html.escape(stat.scryfall_url, quote=True)}" {attributes}>{label}</a>'


def load_samples(state: dict[str, Any], since: date) -> tuple[list[tuple[DeckRecord, list[DeckCard]]], list[str]]:
    samples: list[tuple[DeckRecord, list[DeckCard]]] = []
    warnings: list[str] = []
    for value in state.get("decks", {}).values():
        record = DeckRecord.from_dict(value)
        if date.fromisoformat(record.event_date) < since or not is_top_eight(record.rank):
            continue
        cache_path = Path(record.cache_file)
        if not cache_path.exists():
            warnings.append(f"Chybí cache decku {record.deck_id}: {cache_path}")
            continue
        cards = parse_cached_deck(cache_path)
        if sum(card.quantity for card in cards) < 90:
            warnings.append(f"Cache decku {record.deck_id} je neúplná")
            continue
        samples.append((record, cards))
    samples.sort(key=lambda item: (item[0].event_date, item[0].deck_id), reverse=True)
    return samples, warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deck", type=Path, default=DEFAULT_DECK, help="Lokální Duel Commander decklist")
    parser.add_argument("--commander", default=DEFAULT_COMMANDER, help="Jméno commandera")
    parser.add_argument("--archetype-id", type=int, default=DEFAULT_ARCHETYPE_ID, help="MTGTop8 archetype ID")
    parser.add_argument("--since", type=date.fromisoformat, help="Začátek analyzovaného období (YYYY-MM-DD)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Lokální cache decklistů")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Výstupní HTML")
    parser.add_argument("--offline", action="store_true", help="Nestahovat nic a jen přegenerovat report z cache")
    parser.add_argument("--refresh-all", action="store_true", help="Znovu projít MTGTop8 od začátku období")
    parser.add_argument("--no-scryfall", action="store_true", help="Nevyžadovat typy, odkazy a obrázky ze Scryfall API")
    parser.add_argument("--request-delay", type=float, default=0.35, help="Prodleva mezi požadavky na MTGTop8")
    parser.add_argument("--max-pages", type=int, default=200, help="Bezpečnostní limit stránek výpisu")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if not args.deck.exists():
        print(f"Decklist neexistuje: {args.deck}", file=sys.stderr)
        return 2

    today = date.today()
    default_since = date(today.year, 1, 1)
    state_path = args.cache_dir / "state.json"
    try:
        state = load_state(state_path, args.commander, args.archetype_id, args.since or default_since)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Nelze načíst cache: {exc}", file=sys.stderr)
        return 2

    stored_since = date.fromisoformat(str(state.get("analysis_since") or default_since.isoformat()))
    analysis_since = args.since or stored_since
    state["analysis_since"] = analysis_since.isoformat()
    warnings: list[str] = []
    downloaded = 0
    pages_read = 0

    if not args.offline:
        previous_success = state.get("last_successful_analysis_date")
        fetch_since = analysis_since
        if previous_success and not args.refresh_all:
            fetch_since = date.fromisoformat(str(previous_success))
            if args.since:
                fetch_since = min(fetch_since, args.since)
        print(f"MTGTop8: hledám top 8 decky od {fetch_since.isoformat()}…", flush=True)
        try:
            downloaded, pages_read, fetch_failures = update_cache(
                state,
                state_path,
                args.cache_dir,
                Fetcher(args.request_delay),
                args.commander,
                args.archetype_id,
                fetch_since,
                args.max_pages,
            )
            warnings.extend(f"Stažení decku selhalo: {failure}" for failure in fetch_failures)
            if not fetch_failures:
                state["last_successful_analysis_date"] = today.isoformat()
                state["last_analysis_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            warnings.append(f"Aktualizace MTGTop8 nebyla dokončena: {exc}")
        write_json(state_path, state)

    try:
        current_cards = parse_local_deck(args.deck, args.commander)
        samples, cache_warnings = load_samples(state, analysis_since)
        warnings.extend(cache_warnings)
    except (OSError, ValueError) as exc:
        print(f"Nelze načíst decklisty: {exc}", file=sys.stderr)
        return 2

    if not samples:
        print("V cache nejsou žádné top 8 decky pro zvolené období.", file=sys.stderr)
        if args.offline:
            return 2

    all_names = [card.name for card in current_cards]
    all_names.extend(card.name for _, cards in samples for card in cards)
    scryfall: dict[str, dict[str, str]] = {}
    if not args.no_scryfall:
        scryfall, scryfall_failures = enrich_from_scryfall(all_names, args.cache_dir / "scryfall-cards.json")
        if scryfall_failures:
            warnings.append(f"Scryfall nedohledal nebo nenačetl {len(scryfall_failures)} názvů karet")

    stats = build_card_stats(samples, current_cards, scryfall)
    suggestions = suggest_swaps(stats, len(samples))
    generated_at = datetime.now().astimezone()
    render_report(
        args.output,
        args.commander,
        args.deck,
        analysis_since,
        generated_at,
        samples,
        current_cards,
        stats,
        suggestions,
        warnings,
    )

    print(f"Načteno {len(samples)} cachovaných top 8 decků; nově staženo {downloaded}; stránek výpisu {pages_read}.")
    print(f"Vygenerováno {args.output} ({len(stats)} unikátních karet, {len(suggestions)} návrhů výměn).")
    if warnings:
        print(f"Upozornění: {len(warnings)} (jsou uvedena i v HTML).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
