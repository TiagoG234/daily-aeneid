#!/usr/bin/env python3
"""
fetch_text.py — Fetch Latin text from The Latin Library and save as JSON.

Usage:
    python fetch_text.py                          # fetch Caesar's Gallic Wars (default)
    python fetch_text.py --text caesar_bg         # same
    python fetch_text.py --text aeneid            # fetch Virgil's Aeneid
    python fetch_text.py --text caesar_bg --verify
    python fetch_text.py --text caesar_bg --resume
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LatinReader/1.0)"}

# ── Text definitions ───────────────────────────────────────────────────────────

TEXT_DEFS = {
    "caesar_bg": {
        "name": "Caesar's Gallic Wars",
        "output_file": "gallic_wars.json",
        "book_count": 8,
        "url_pattern": "https://www.thelatinlibrary.com/caesar/gall{n}.shtml",
        "book_unit_counts": [54, 35, 29, 38, 58, 44, 90, 55],
        "unit_type": "chapter",
        "parser": "caesar",
        "total_expected": 403,
    },
    "aeneid": {
        "name": "Virgil's Aeneid",
        "output_file": "aeneid.json",
        "book_count": 12,
        "url_pattern": "https://www.thelatinlibrary.com/vergil/aen{n}.shtml",
        "book_unit_counts": [756, 803, 718, 705, 871, 901, 817, 731, 815, 907, 915, 950],
        "unit_type": "line",
        "parser": "aeneid",
        "total_expected": 9896,
    },
}

# ── HTTP helper ────────────────────────────────────────────────────────────────

def _fetch_html(url: str) -> str:
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.encoding = "utf-8"
            return resp.text
        except Exception as e:
            if attempt == 3:
                raise
            wait = 5 * (2 ** attempt)
            print(f"\n  (retry {attempt+1}: {e}, waiting {wait}s)", end="", flush=True)
            time.sleep(wait)


# ── Caesar parser ──────────────────────────────────────────────────────────────

_CHAPTER_RE = re.compile(r"^\s*\[\s*(\d+)\s*\]\s*")


def _parse_caesar_book(html: str, book_num: int, expected: int) -> list[str]:
    """
    Parse one book of Caesar's BG into a list of chapter strings.

    The Latin Library encodes each chapter as one <p> paragraph whose text
    starts with [N] (an <a name="N"> anchor rendered as "[N]").
    Section numbers within each chapter appear as <font size="2">N</font>.
    We strip the chapter marker and section numbers, returning clean prose.
    """
    soup = BeautifulSoup(html, "html.parser")
    chapters: dict[int, str] = {}

    for p in soup.find_all("p"):
        if "internal_navigation" in p.get("class", []):
            continue

        # Remove <font> section-number tags but keep no content (they are just
        # inline section markers like "1", "2" — we drop them for clean prose).
        for font in p.find_all("font"):
            font.decompose()

        text = p.get_text(separator=" ").replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()

        m = _CHAPTER_RE.match(text)
        if not m:
            continue  # not a chapter paragraph

        chap_num = int(m.group(1))
        text = text[m.end():].strip()
        if text and len(text) >= 10:
            chapters[chap_num] = text

    result = [chapters[i] for i in sorted(chapters) if i in chapters]

    if abs(len(result) - expected) > 2:
        print(f"  WARNING Book {book_num}: got {len(result)} chapters, expected {expected}")

    return result


# ── Aeneid parser ──────────────────────────────────────────────────────────────

_AENEID_NAV = {"Vergil", "The Latin Library", "The Classics Page"}


def _parse_aeneid_book(html: str, book_num: int, expected: int) -> list[str]:
    """Parse one book of the Aeneid into individual verse lines."""
    soup = BeautifulSoup(html, "html.parser")
    lines = []
    for p in soup.find_all("p"):
        classes = p.get("class", [])
        if "internal_navigation" in classes:
            continue
        if classes and "poem" not in classes:
            continue
        for span in p.find_all("span"):
            span.decompose()
        for br in p.find_all("br"):
            br.replace_with("\n")
        for raw_line in p.get_text().split("\n"):
            line = raw_line.replace("\xa0", " ").strip()
            if line in _AENEID_NAV:
                continue
            if len(line) >= 4 and re.search(r"[a-zA-Z]{2}", line):
                lines.append(line)

    if len(lines) == expected:
        return lines
    if len(lines) > expected:
        return lines[-expected:]
    if abs(len(lines) - expected) > 2:
        print(f"  WARNING Book {book_num}: got {len(lines)} lines, expected {expected}")
    return lines


# ── Fetch all books ────────────────────────────────────────────────────────────

def fetch_all(text_key: str, resume: bool = False) -> list[dict]:
    defn = TEXT_DEFS[text_key]
    output_file = defn["output_file"]

    existing: dict[int, list[str]] = {}
    if resume and Path(output_file).exists():
        with open(output_file, encoding="utf-8") as f:
            for b in json.load(f):
                existing[b["book"]] = b["units"]
        print(f"  Resuming — already have books: {sorted(existing)}\n")

    result = []
    for i in range(defn["book_count"]):
        book_num = i + 1
        expected = defn["book_unit_counts"][i]

        if book_num in existing:
            cached = existing[book_num]
            print(f"  Book {book_num:>3} ({expected:>3} expected)... {len(cached)}/{expected} (cached)")
            result.append({"book": book_num, "units": cached})
            continue

        url = defn["url_pattern"].format(n=book_num)
        print(f"  Book {book_num:>3} ({expected:>3} expected)... ", end="", flush=True)
        try:
            html = _fetch_html(url)
        except Exception as e:
            print(f"FAILED: {e}")
            if result:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"  Partial save: {len(result)} books. Re-run with --resume.")
            sys.exit(1)

        if defn["parser"] == "caesar":
            units = _parse_caesar_book(html, book_num, expected)
        else:
            units = _parse_aeneid_book(html, book_num, expected)

        print(f"{len(units)}/{expected}")
        result.append({"book": book_num, "units": units})
        time.sleep(2)

    return result


def verify(data: list[dict], text_key: str) -> None:
    defn = TEXT_DEFS[text_key]
    unit_type = defn["unit_type"]
    for book in data:
        n = book["book"]
        units = book["units"]
        print(f"\nBook {n} ({len(units)} {unit_type}s)")
        for u in units[:2]:
            preview = u if len(u) <= 100 else u[:97] + "..."
            print(f"  {preview}")
        print("  …")
        for u in units[-2:]:
            preview = u if len(u) <= 100 else u[:97] + "..."
            print(f"  {preview}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Latin text from The Latin Library and save as JSON."
    )
    parser.add_argument(
        "--text", default="caesar_bg", choices=list(TEXT_DEFS),
        help="Which text to fetch (default: caesar_bg)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Show sample units from an existing JSON file",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip books already present in the JSON, fetch the rest",
    )
    args = parser.parse_args()

    defn = TEXT_DEFS[args.text]
    output_file = defn["output_file"]

    if args.verify:
        try:
            with open(output_file, encoding="utf-8") as f:
                data = json.load(f)
            verify(data, args.text)
        except FileNotFoundError:
            print(f"{output_file} not found. Run without --verify first.")
        return

    print(f"Fetching {defn['name']} from The Latin Library...\n")
    data = fetch_all(args.text, resume=args.resume)

    total = sum(len(b["units"]) for b in data)
    expected = defn["total_expected"]
    print(f"\nTotal: {total} {defn['unit_type']}s (expected ~{expected})")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved {output_file} — commit this file to the repo.")


if __name__ == "__main__":
    main()
