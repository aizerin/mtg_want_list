#!/usr/bin/env python3
"""Price an MTG want list from a local Scryfall all-cards JSON export."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DATASET = "all-cards-20260424092225.json"
DEFAULT_WANTLIST = "want-list-plain"
DEFAULT_OUTPUT = "index.html"
ECB_DAILY_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

PAPER_FORMATS = {
    "standard",
    "future",
    "pioneer",
    "modern",
    "legacy",
    "vintage",
    "pauper",
    "commander",
    "oathbreaker",
    "standardbrawl",
    "paupercommander",
    "duel",
    "oldschool",
    "premodern",
    "predh",
}

LEGAL_VALUES = {"legal", "restricted"}
EXCLUDED_BORDER_COLORS = {"gold", "silver"}
EXCLUDED_SET_TYPES = {"alchemy", "funny", "memorabilia", "minigame", "token"}

CATEGORIES = [
    ("0-5 eur", Decimal("0"), Decimal("5")),
    ("5-10 eur", Decimal("5"), Decimal("10")),
    ("10-50 eur", Decimal("10"), Decimal("50")),
    ("50-100 eur", Decimal("50"), Decimal("100")),
    ("100-200 eur", Decimal("100"), Decimal("200")),
    ("200+ eur", Decimal("200"), None),
]


@dataclass(frozen=True)
class Want:
    display_name: str
    lookup_key: str
    quantity: int
    line_no: int
    owned: bool


@dataclass(frozen=True)
class WantList:
    path: Path
    wants: list[Want]


@dataclass(frozen=True)
class ExchangeRate:
    eur_to_czk: Decimal
    source: str


@dataclass(frozen=True)
class Match:
    want: Want
    price: Decimal
    finish: str
    card_name: str
    set_code: str
    set_name: str
    collector_number: str
    released_at: str
    lang: str
    scryfall_uri: str
    image_url: str


def normalize_name(value: str, *, fold_accents: bool = False) -> str:
    value = value.replace("’", "'").replace("`", "'").strip().casefold()
    value = re.sub(r"\s+", " ", value)
    if fold_accents:
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def parse_wantlist(path: Path) -> list[Want]:
    wants: list[Want] = []
    seen: set[str] = set()

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        owned = False
        if line.startswith("+"):
            owned = True
            line = line[1:].strip()

        quantity = 1
        match = re.match(r"^(?:(\d+)\s*x?\s+)(.+)$", line, flags=re.IGNORECASE)
        if match:
            quantity = int(match.group(1))
            line = match.group(2).strip()
            if line.startswith("+"):
                owned = True
                line = line[1:].strip()

        if not line:
            continue

        key = normalize_name(line)
        if key in seen:
            continue
        seen.add(key)
        wants.append(Want(display_name=line, lookup_key=key, quantity=quantity, line_no=line_no, owned=owned))

    return wants


def iter_scryfall_cards(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[dict[str, Any]]:
    """Yield objects from Scryfall's large JSON array without loading it all."""
    decoder = json.JSONDecoder()
    buffer = ""
    pos = 0
    started = False
    eof = False

    with path.open("r", encoding="utf-8") as handle:
        while True:
            if not eof:
                chunk = handle.read(chunk_size)
                if chunk:
                    buffer += chunk
                else:
                    eof = True

            if not started:
                pos = skip_ws(buffer, pos)
                if pos >= len(buffer):
                    if eof:
                        raise ValueError("Dataset is empty.")
                    continue
                if buffer[pos] != "[":
                    raise ValueError("Expected Scryfall dataset to be a JSON array.")
                pos += 1
                started = True

            while True:
                pos = skip_ws(buffer, pos)
                if pos < len(buffer) and buffer[pos] == ",":
                    pos += 1
                    pos = skip_ws(buffer, pos)

                if pos < len(buffer) and buffer[pos] == "]":
                    return

                try:
                    obj, pos = decoder.raw_decode(buffer, pos)
                except json.JSONDecodeError:
                    if eof:
                        raise
                    break

                if isinstance(obj, dict):
                    yield obj

            if pos:
                buffer = buffer[pos:]
                pos = 0

            if eof:
                return


def skip_ws(value: str, pos: int) -> int:
    while pos < len(value) and value[pos] in " \t\r\n":
        pos += 1
    return pos


def add_name_keys(keys: set[str], value: str) -> None:
    keys.add(normalize_name(value))
    keys.add(normalize_name(value, fold_accents=True))


def card_lookup_keys(card: dict[str, Any], *, include_all_faces: bool) -> set[str]:
    keys: set[str] = set()
    for field in ("name", "printed_name"):
        value = card.get(field)
        if isinstance(value, str):
            add_name_keys(keys, value)

    faces = card.get("card_faces") or []
    if include_all_faces:
        faces_to_match = faces
    else:
        faces_to_match = faces[:1]

    for face in faces_to_match:
        if isinstance(face, dict):
            for field in ("name", "printed_name"):
                value = face.get(field)
                if isinstance(value, str):
                    add_name_keys(keys, value)

    return keys


def is_tournament_legal_paper_print(card: dict[str, Any], *, allow_unreleased: bool) -> bool:
    if card.get("digital"):
        return False
    if "paper" not in (card.get("games") or []):
        return False
    if card.get("oversized"):
        return False
    if card.get("border_color") in EXCLUDED_BORDER_COLORS:
        return False
    if card.get("set_type") in EXCLUDED_SET_TYPES:
        return False

    released_at = card.get("released_at")
    if not allow_unreleased and isinstance(released_at, str):
        try:
            if date.fromisoformat(released_at) > date.today():
                return False
        except ValueError:
            return False

    legalities = card.get("legalities") or {}
    return any(legalities.get(fmt) in LEGAL_VALUES for fmt in PAPER_FORMATS)


def cheapest_eur_price(card: dict[str, Any]) -> tuple[Decimal, str] | None:
    prices = card.get("prices") or {}
    candidates: list[tuple[Decimal, str]] = []

    for field, finish in (
        ("eur", "nonfoil"),
        ("eur_foil", "foil"),
        ("eur_etched", "etched"),
    ):
        value = prices.get(field)
        if not value:
            continue
        try:
            candidates.append((Decimal(str(value)), finish))
        except InvalidOperation:
            continue

    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])


def card_image_url(card: dict[str, Any]) -> str:
    image_uris = card.get("image_uris")
    if isinstance(image_uris, dict):
        for size in ("normal", "large", "small"):
            value = image_uris.get(size)
            if isinstance(value, str):
                return value

    for face in card.get("card_faces") or []:
        if not isinstance(face, dict):
            continue
        face_image_uris = face.get("image_uris")
        if not isinstance(face_image_uris, dict):
            continue
        for size in ("normal", "large", "small"):
            value = face_image_uris.get(size)
            if isinstance(value, str):
                return value

    return ""


def find_matches(
    dataset_path: Path,
    wantlists: list[WantList],
    *,
    allow_unreleased: bool,
    match_card_faces: bool,
) -> tuple[dict[str, Match], int]:
    wanted_by_key: dict[str, str] = {}
    want_by_lookup_key: dict[str, Want] = {}
    for wantlist in wantlists:
        for want in wantlist.wants:
            wanted_by_key[want.lookup_key] = want.lookup_key
            wanted_by_key[normalize_name(want.display_name, fold_accents=True)] = want.lookup_key
            want_by_lookup_key.setdefault(want.lookup_key, want)

    best: dict[str, Match] = {}
    scanned = 0

    for card in iter_scryfall_cards(dataset_path):
        scanned += 1
        matching_lookup_keys = {
            wanted_by_key[key]
            for key in card_lookup_keys(card, include_all_faces=match_card_faces)
            if key in wanted_by_key
        }
        if not matching_lookup_keys:
            continue
        if not is_tournament_legal_paper_print(card, allow_unreleased=allow_unreleased):
            continue

        price = cheapest_eur_price(card)
        if price is None:
            continue

        value, finish = price
        for lookup_key in matching_lookup_keys:
            previous = best.get(lookup_key)
            if previous is None or value < previous.price:
                best[lookup_key] = Match(
                    want=want_by_lookup_key[lookup_key],
                    price=value,
                    finish=finish,
                    card_name=str(card.get("name") or want_by_lookup_key[lookup_key].display_name),
                    set_code=str(card.get("set") or "").upper(),
                    set_name=str(card.get("set_name") or ""),
                    collector_number=str(card.get("collector_number") or ""),
                    released_at=str(card.get("released_at") or ""),
                    lang=str(card.get("lang") or ""),
                    scryfall_uri=str(card.get("scryfall_uri") or ""),
                    image_url=card_image_url(card),
                )

    return best, scanned


def category_for(price: Decimal) -> str:
    for label, lower, upper in CATEGORIES:
        if price >= lower and (upper is None or price < upper):
            return label
    raise ValueError(f"Price does not fit a category: {price}")


def format_price(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))} eur"


def format_czk(value: Decimal, exchange_rate: ExchangeRate) -> str:
    return f"{(value * exchange_rate.eur_to_czk).quantize(Decimal('0.01'))} CZK"


def format_prices(value: Decimal, exchange_rate: ExchangeRate) -> str:
    return f"{format_price(value)} / {format_czk(value, exchange_rate)}"


def fetch_eur_to_czk_rate() -> ExchangeRate:
    request = urllib.request.Request(
        ECB_DAILY_RATES_URL,
        headers={"User-Agent": "wantlist-scryfall-price-script/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    namespace = {"gesmes": "http://www.gesmes.org/xml/2002-08-01", "eurofxref": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    time_node = root.find(".//eurofxref:Cube[@time]", namespace)
    rate_node = root.find(".//eurofxref:Cube[@currency='CZK']", namespace)
    if rate_node is None or "rate" not in rate_node.attrib:
        raise ValueError("CZK exchange rate not found in ECB feed.")

    rate_date = time_node.attrib.get("time", "latest") if time_node is not None else "latest"
    return ExchangeRate(
        eur_to_czk=Decimal(rate_node.attrib["rate"]),
        source=f"ECB {rate_date}",
    )


def build_exchange_rate(value: str | None) -> ExchangeRate:
    if value is not None:
        try:
            rate = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid --eur-czk value: {value}") from exc
        if rate <= 0:
            raise ValueError("--eur-czk must be greater than zero.")
        return ExchangeRate(eur_to_czk=rate, source="manual")

    return fetch_eur_to_czk_rate()


def group_matches(wants: list[Want], matches: dict[str, Match]) -> tuple[dict[str, list[tuple[Want, Match]]], list[Want]]:
    grouped: dict[str, list[tuple[Want, Match]]] = {label: [] for label, _, _ in CATEGORIES}
    for want in wants:
        match = matches.get(want.lookup_key)
        if match:
            grouped[category_for(match.price)].append((want, match))

    missing = [want for want in wants if want.lookup_key not in matches]
    return grouped, missing


def line_total(items: list[tuple[Want, Match]]) -> Decimal:
    return sum((match.price * want.quantity for want, match in items if not want.owned), Decimal("0"))


def wantlist_total(wants: list[Want], matches: dict[str, Match]) -> Decimal:
    return sum(
        (matches[want.lookup_key].price * want.quantity for want in wants if not want.owned and want.lookup_key in matches),
        Decimal("0"),
    )


def total_label(label: str, total: Decimal, exchange_rate: ExchangeRate) -> str:
    return f"{label} | total {format_prices(total, exchange_rate)}"


def write_text_output(path: Path, wantlists: list[WantList], matches: dict[str, Match], exchange_rate: ExchangeRate) -> None:
    lines: list[str] = []
    lines.append(f"EUR/CZK: {exchange_rate.eur_to_czk} ({exchange_rate.source})")
    lines.append("")

    for wantlist in wantlists:
        grouped, missing = group_matches(wantlist.wants, matches)
        lines.append(f"{wantlist.path.name} | total {format_prices(wantlist_total(wantlist.wants, matches), exchange_rate)}")
        lines.append("")

        for label, _, _ in CATEGORIES:
            items = sorted(grouped[label], key=lambda item: (item[1].price, item[0].display_name.casefold()))
            if not items:
                continue
            lines.append(total_label(label, line_total(items), exchange_rate))
            visible_items = [item for item in items if not item[0].owned]
            owned_items = [item for item in items if item[0].owned]
            for want, match in visible_items + owned_items:
                qty = f"{want.quantity}x " if want.quantity != 1 else ""
                owned = "+ " if want.owned else ""
                lines.append(f"{owned}{qty}{want.display_name} > {format_prices(match.price, exchange_rate)}")
            lines.append("")

        if missing:
            lines.append("not found / no EUR price")
            for want in missing:
                qty = f"{want.quantity}x " if want.quantity != 1 else ""
                owned = "+ " if want.owned else ""
                lines.append(f"{owned}{qty}{want.display_name}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_html_output(path: Path, wantlists: list[WantList], matches: dict[str, Match], exchange_rate: ExchangeRate) -> None:
    wantlist_sections: list[str] = []

    for wantlist in wantlists:
        grouped, missing = group_matches(wantlist.wants, matches)
        category_sections: list[str] = []

        for label, _, _ in CATEGORIES:
            items = sorted(grouped[label], key=lambda item: (item[1].price, item[0].display_name.casefold()))
            if not items:
                continue
            rows: list[str] = []
            visible_items = [item for item in items if not item[0].owned]
            owned_items = [item for item in items if item[0].owned]
            for want, match in visible_items + owned_items:
                qty = f"{want.quantity}x " if want.quantity != 1 else ""
                owned = "+ " if want.owned else ""
                name = html.escape(f"{owned}{qty}{want.display_name}")
                price = html.escape(format_prices(match.price, exchange_rate))
                image = html.escape(match.image_url, quote=True)
                owned_class = " owned" if want.owned else ""
                rows.append(
                    f'<div class="card-row{owned_class}" tabindex="0" data-image="{image}">'
                    f'<span>{name}</span><span>{price}</span></div>'
                )
            category_sections.append(
                f'<section class="price-band"><h3>{html.escape(total_label(label, line_total(items), exchange_rate))}</h3>'
                f'<div class="card-list">{"".join(rows)}</div></section>'
            )

        if missing:
            missing_rows = []
            for want in missing:
                qty = f"{want.quantity}x " if want.quantity != 1 else ""
                owned = "+ " if want.owned else ""
                owned_class = " owned" if want.owned else ""
                missing_rows.append(f'<div class="missing-row{owned_class}">{html.escape(owned + qty + want.display_name)}</div>')
            category_sections.append(
                '<section class="price-band"><h3>not found / no EUR price</h3>'
                f'<div class="card-list">{"".join(missing_rows)}</div></section>'
            )
        wantlist_sections.append(
            f'<section class="wantlist-section"><h2>{html.escape(wantlist.path.name)}'
            f'<span>{html.escape(format_prices(wantlist_total(wantlist.wants, matches), exchange_rate))}</span></h2>'
            f'{"".join(category_sections)}</section>'
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MTG Want List Prices</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f3ed;
      --panel: #ffffff;
      --ink: #1f2933;
      --muted: #667085;
      --line: #d9d3c8;
      --accent: #1c6f5b;
      --accent-soft: #e5f2ee;
      --shadow: 0 18px 45px rgba(25, 32, 38, 0.18);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.35;
    }}

    main {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 48px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 24px;
      align-items: start;
    }}

    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      font-weight: 750;
      letter-spacing: 0;
    }}

    .rate-note {{
      margin: 0 0 22px;
      color: var(--muted);
      font-size: 13px;
    }}

    h2 {{
      margin: 28px 0 14px;
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: baseline;
      font-size: 22px;
      font-weight: 750;
      letter-spacing: 0;
      color: #18232d;
    }}

    h2 span {{
      color: var(--accent);
      font-size: 15px;
      font-weight: 750;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}

    h3 {{
      margin: 0;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
      font-weight: 750;
      letter-spacing: 0;
      color: #26323d;
    }}

    .bands {{
      min-width: 0;
    }}

    .wantlist-section {{
      margin-bottom: 30px;
    }}

    .wantlist-section:first-of-type h2 {{
      margin-top: 0;
    }}

    .price-band {{
      margin-bottom: 16px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }}

    .card-list {{
      display: grid;
    }}

    .card-row,
    .missing-row,
    .empty {{
      min-height: 38px;
      padding: 9px 14px;
      border-top: 1px solid #eee9df;
    }}

    .card-row:first-child,
    .missing-row:first-child,
    .empty:first-child {{
      border-top: 0;
    }}

    .card-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(150px, auto);
      gap: 16px;
      align-items: center;
      cursor: default;
      outline: 0;
    }}

    .card-row span:first-child {{
      min-width: 0;
      overflow-wrap: anywhere;
      font-weight: 560;
    }}

    .card-row span:last-child {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      text-align: right;
    }}

    .card-row:hover,
    .card-row:focus {{
      background: var(--accent-soft);
    }}

    .card-row.owned,
    .missing-row.owned {{
      background: #e7f6ec;
      color: #166534;
    }}

    .card-row.owned span:last-child {{
      color: #2f7d46;
    }}

    .card-row.owned:hover,
    .card-row.owned:focus {{
      background: #d8f0df;
    }}

    .preview {{
      position: sticky;
      top: 24px;
      min-height: 480px;
    }}

    .preview-frame {{
      min-height: 474px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ede8dd;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}

    .preview img {{
      width: min(100%, 336px);
      aspect-ratio: 488 / 680;
      object-fit: contain;
      display: none;
    }}

    .preview.has-image img {{
      display: block;
    }}

    .preview-label {{
      color: var(--muted);
      padding: 20px;
      text-align: center;
    }}

    .preview.has-image .preview-label {{
      display: none;
    }}

    @media (max-width: 860px) {{
      main {{
        grid-template-columns: 1fr;
      }}

      .preview {{
        display: none;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="bands">
      <h1>MTG Want List Prices</h1>
      <p class="rate-note">EUR/CZK: {html.escape(str(exchange_rate.eur_to_czk))} ({html.escape(exchange_rate.source)})</p>
      {"".join(wantlist_sections)}
    </div>
    <aside class="preview" aria-live="polite">
      <div class="preview-frame">
        <img id="card-preview" alt="">
        <div class="preview-label">Hover a card</div>
      </div>
    </aside>
  </main>
  <script>
    const preview = document.querySelector(".preview");
    const image = document.querySelector("#card-preview");

    function showCard(row) {{
      const source = row.dataset.image;
      if (!source) return;
      image.src = source;
      image.alt = row.querySelector("span")?.textContent || "Card preview";
      preview.classList.add("has-image");
    }}

    document.querySelectorAll(".card-row").forEach((row) => {{
      row.addEventListener("pointerenter", () => showCard(row));
      row.addEventListener("focus", () => showCard(row));
      row.addEventListener("click", () => showCard(row));
    }});
  </script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def output_paths(path: Path) -> tuple[Path, Path]:
    suffix = path.suffix.casefold()
    if suffix in {".html", ".htm", ".txt"}:
        base = path.with_suffix("")
    else:
        base = path
    return base.with_suffix(".html"), base.with_suffix(".txt")


def write_output(path: Path, wantlists: list[WantList], matches: dict[str, Match], exchange_rate: ExchangeRate) -> tuple[Path, Path]:
    html_path, text_path = output_paths(path)
    write_html_output(html_path, wantlists, matches, exchange_rate)
    write_text_output(text_path, wantlists, matches, exchange_rate)
    return html_path, text_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filenames", nargs="*", type=Path, help="Plain-text want list file(s)")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, type=Path, help="Scryfall all-cards JSON file")
    parser.add_argument("--wantlist", default=DEFAULT_WANTLIST, type=Path, help="Plain-text want list")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, type=Path, help="Output basename or file. Writes both .html and .txt.")
    parser.add_argument("--eur-czk", help="Manual EUR to CZK rate. If omitted, the script fetches the ECB daily rate.")
    parser.add_argument("--include-unreleased", action="store_true", help="Allow cards with release dates after today")
    parser.add_argument("--match-card-faces", action="store_true", help="Also match back-side and other non-primary card_faces names")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    wantlist_paths = args.filenames or [args.wantlist]

    if not args.dataset.exists():
        print(f"Dataset not found: {args.dataset}", file=sys.stderr)
        return 2
    for wantlist_path in wantlist_paths:
        if not wantlist_path.exists():
            print(f"Want list not found: {wantlist_path}", file=sys.stderr)
            return 2

    try:
        exchange_rate = build_exchange_rate(args.eur_czk)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"Could not get EUR/CZK exchange rate: {exc}", file=sys.stderr)
        print("Use --eur-czk RATE to provide it manually.", file=sys.stderr)
        return 2

    wantlists = [WantList(path=wantlist_path, wants=parse_wantlist(wantlist_path)) for wantlist_path in wantlist_paths]
    matches, scanned = find_matches(
        args.dataset,
        wantlists,
        allow_unreleased=args.include_unreleased,
        match_card_faces=args.match_card_faces,
    )
    html_path, text_path = write_output(args.output, wantlists, matches, exchange_rate)

    total_wants = sum(len(wantlist.wants) for wantlist in wantlists)
    total_matches = sum(1 for wantlist in wantlists for want in wantlist.wants if want.lookup_key in matches)
    print(f"EUR/CZK: {exchange_rate.eur_to_czk} ({exchange_rate.source}).")
    print(f"Scanned {scanned} cards.")
    print(f"Matched {total_matches} of {total_wants} want-list entries.")
    print(f"Wrote {html_path}.")
    print(f"Wrote {text_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
