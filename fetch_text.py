#!/usr/bin/env python3
"""
fetch_text.py — One-time script: fetch the Aeneid from The Latin Library and
save it as aeneid.json. Run once locally, then commit aeneid.json to the repo.

Usage:
    python fetch_text.py            # fetch and save
    python fetch_text.py --verify   # show first/last 3 lines per book
"""

import argparse
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BOOK_LINE_COUNTS = [756, 804, 718, 705, 871, 901, 817, 731, 818, 908, 915, 952]
BOOK_NAMES = ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"]
BASE_URL = "https://www.thelatinlibrary.com/vergil/aen{}.shtml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AeneidReader/1.0)"}

def _fetch_html(url: str) -> str:
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.encoding = 'utf-8'
            return resp.text
        except Exception as e:
            if attempt == 3:
                raise
            wait = 5 * (2 ** attempt)
            print(f"\n  (retry {attempt+1}: {e}, waiting {wait}s)", end='', flush=True)
            time.sleep(wait)


_SITE_NAV = {'Vergil', 'The Latin Library', 'The Classics Page'}


def fetch_book(book_num: int) -> list[str]:
    """Extract verse lines from the Latin Library page for one book.

    Page structure varies by book:
      <p class='internal_navigation'> — duplicate of opening text for site nav; skip
      <p class='poem'>               — verse content (some books)
      <p class=[]>                   — verse content (other books, e.g. XII)
      <span style="font-size:80%">   — inline line-number labels; remove
    Footer strings ('Vergil', 'The Latin Library', 'The Classics Page') are
    filtered at the line level so we never need to detect the footer paragraph.
    """
    html = _fetch_html(BASE_URL.format(book_num))
    soup = BeautifulSoup(html, 'html.parser')

    lines = []
    for p in soup.find_all('p'):
        classes = p.get('class', [])
        if 'internal_navigation' in classes:
            continue
        if classes and 'poem' not in classes:
            continue  # skip pagehead and other named classes

        for span in p.find_all('span'):
            span.decompose()
        for br in p.find_all('br'):
            br.replace_with('\n')

        for raw_line in p.get_text().split('\n'):
            line = raw_line.replace('\xa0', ' ').strip()
            if line in _SITE_NAV:
                continue
            if len(line) >= 4 and re.search(r'[a-zA-Z]{2}', line):
                lines.append(line)

    return lines


def trim_to_expected(lines: list[str], expected: int, book_num: int) -> list[str]:
    if len(lines) == expected:
        return lines
    if len(lines) > expected:
        return lines[-expected:]
    # Hemistichoi: genuinely fewer lines is acceptable; warn only if > 2 off
    if abs(len(lines) - expected) > 2:
        print(f"  WARNING Book {BOOK_NAMES[book_num-1]}: got {len(lines)}, expected {expected}")
    return lines


def fetch_all(resume: bool = False) -> list[dict]:
    existing: dict[int, list[str]] = {}
    if resume and Path('aeneid.json').exists():
        with open('aeneid.json', encoding='utf-8') as f:
            for b in json.load(f):
                existing[b['book']] = b['lines']
        print(f"  Resuming — already have books: {sorted(existing)}\n")

    aeneid = []
    for i in range(12):
        book_num = i + 1
        expected = BOOK_LINE_COUNTS[i]

        if book_num in existing:
            print(f"  Book {BOOK_NAMES[i]:>4} ({expected:>3} lines)... {len(existing[book_num])}/{expected} (cached)")
            aeneid.append({"book": book_num, "lines": existing[book_num]})
            continue

        print(f"  Book {BOOK_NAMES[i]:>4} ({expected:>3} lines)... ", end='', flush=True)
        try:
            lines = fetch_book(book_num)
        except Exception as e:
            print(f"FAILED: {e}")
            # Save progress so far before exiting
            if aeneid:
                with open('aeneid.json', 'w', encoding='utf-8') as f:
                    json.dump(aeneid, f, ensure_ascii=False, indent=2)
                print(f"  Partial save: {len(aeneid)} books. Re-run with --resume.")
            sys.exit(1)
        lines = trim_to_expected(lines, expected, book_num)
        print(f"{len(lines)}/{expected}")
        aeneid.append({"book": book_num, "lines": lines})
        time.sleep(3)

    return aeneid


def verify(aeneid: list[dict]) -> None:
    for book in aeneid:
        n = book['book']
        lines = book['lines']
        print(f"\nBook {BOOK_NAMES[n-1]} ({len(lines)} lines)")
        for line in lines[:3]:
            print(f"  {line}")
        print("  …")
        for line in lines[-3:]:
            print(f"  {line}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true',
                        help='Show sample lines from existing aeneid.json')
    parser.add_argument('--resume', action='store_true',
                        help='Skip books already in aeneid.json, fetch the rest')
    args = parser.parse_args()

    if args.verify:
        try:
            with open('aeneid.json', encoding='utf-8') as f:
                aeneid = json.load(f)
            verify(aeneid)
        except FileNotFoundError:
            print("aeneid.json not found. Run without --verify first.")
        return

    print("Fetching Aeneid from The Latin Library...\n")
    aeneid = fetch_all(resume=args.resume)

    total = sum(len(b['lines']) for b in aeneid)
    print(f"\nTotal: {total} lines (expected 9896)")

    with open('aeneid.json', 'w', encoding='utf-8') as f:
        json.dump(aeneid, f, ensure_ascii=False, indent=2)
    print("Saved aeneid.json — commit this file to the repo.")


if __name__ == '__main__':
    main()
