#!/usr/bin/env python3
"""
daily_latin.py — Config-driven daily Latin reading email.

Usage:
    python daily_latin.py                  # send today's email
    python daily_latin.py --day 47         # send day 47's email
    python daily_latin.py --dry-run        # save preview.html, do not send
    python daily_latin.py --day 1 --dry-run
    python daily_latin.py --no-ai          # layout test without API call

Active text: set LATIN_TEXT env var (default: "caesar_bg").
"""

import argparse
import calendar
import json
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Text configs ───────────────────────────────────────────────────────────────
# Add a new entry here to support a different Latin text.
# genre="prose" → constructions + historical_note sections
# genre="verse" → scansion sections

TEXT_CONFIGS = {
    "caesar_bg": {
        "title": "Caesar's Gallic Wars",
        "short_title": "BG",
        "author": "Caesar",
        "genre": "prose",
        "data_file": "gallic_wars.json",
        "units_per_day": 2,
        "unit_type": "chapter",
        "rest_weekday": 2,  # Wednesday
        "start_date": date(2026, 5, 21),
        "book_names": ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"],
        "book_unit_counts": [54, 35, 29, 38, 58, 44, 90, 55],
        "book_notes": {8: "by A. Hirtius"},
        "sender_name": "Daily Latin",
        "footer_motto": "GALLIA EST OMNIS DIVISA IN PARTES TRES",
        "fact_categories": [
            "historical context",
            "source tradition",
            "linguistic register",
            "intertextual echo",
            "biographical context",
            "military & political context",
        ],
        "fact_colors": {
            "historical context":           "#2d5a27",
            "source tradition":             "#1a3a5c",
            "linguistic register":          "#5c4011",
            "intertextual echo":            "#1a455c",
            "biographical context":         "#3d2d5c",
            "military & political context": "#5c1a3a",
        },
        # prose-only
        "historical_note_categories": [
            "Gallic tribes",
            "Roman army",
            "geography",
            "Roman politics",
            "Caesar's rhetoric",
            "chronology",
        ],
        "historical_note_colors": {
            "Gallic tribes":    "#2d5a27",
            "Roman army":       "#1a3a5c",
            "geography":        "#5c4011",
            "Roman politics":   "#5c1a3a",
            "Caesar's rhetoric":"#1a455c",
            "chronology":       "#3d2d5c",
        },
    },
    "aeneid": {
        "title": "Virgil's Aeneid",
        "short_title": "Aeneid",
        "author": "Virgil",
        "genre": "verse",
        "data_file": "aeneid.json",
        "units_per_day": 27,
        "unit_type": "line",
        "rest_weekday": 2,
        "start_date": date(2026, 5, 16),
        "book_names": ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"],
        "book_unit_counts": [756, 803, 718, 705, 871, 901, 817, 731, 815, 907, 915, 950],
        "book_notes": {},
        "sender_name": "Daily Aeneid",
        "footer_motto": "ARMA VIRUMQUE CANO",
        "fact_categories": [
            "compositional technique",
            "manuscript tradition",
            "reception history",
            "metrical anomaly",
            "intertextual echo",
            "biographical context",
        ],
        "fact_colors": {
            "compositional technique": "#2d5a27",
            "manuscript tradition":    "#1a3a5c",
            "reception history":       "#5c1a3a",
            "metrical anomaly":        "#5c3d11",
            "intertextual echo":       "#1a455c",
            "biographical context":    "#3d2d5c",
        },
    },
}

# ── Reading day / unit arithmetic ──────────────────────────────────────────────

def reading_day_for(target: date, cfg: dict) -> int | None:
    """1-indexed reading day for `target`, or None if rest day / before start."""
    start = cfg["start_date"]
    rest = cfg["rest_weekday"]
    if target < start or target.weekday() == rest:
        return None
    count = 0
    d = start
    while d <= target:
        if d.weekday() != rest:
            count += 1
        d += timedelta(days=1)
    return count


def total_units(cfg: dict) -> int:
    return sum(cfg["book_unit_counts"])


def global_unit_range(day: int, cfg: dict) -> tuple[int, int] | None:
    """Inclusive global (1-indexed) unit range for reading day N, or None if finished."""
    upd = cfg["units_per_day"]
    tot = total_units(cfg)
    start = (day - 1) * upd + 1
    if start > tot:
        return None
    return start, min(day * upd, tot)


def extract_units(
    data: list[dict], start_global: int, end_global: int
) -> list[tuple[int, int, str]]:
    """Return [(book_num, unit_num_within_book, text), …] for the global range."""
    result = []
    cumulative = 0
    for book_data in data:
        book_num = book_data["book"]
        units = book_data.get("units") or book_data.get("lines", [])
        book_end = cumulative + len(units)

        if end_global <= cumulative or start_global > book_end:
            cumulative = book_end
            continue

        local_start = max(start_global, cumulative + 1) - cumulative - 1
        local_end   = min(end_global, book_end) - cumulative - 1
        for i in range(local_start, local_end + 1):
            if i < len(units):
                result.append((book_num, i + 1, units[i]))

        cumulative = book_end
    return result


def describe_range(units_data: list[tuple[int, int, str]], cfg: dict) -> str:
    """E.g. 'BG I.3–4' or 'Aeneid III.240–266'."""
    if not units_data:
        return ""
    fb, fu, _ = units_data[0]
    lb, lu, _ = units_data[-1]
    bnames = cfg["book_names"]
    prefix = cfg["short_title"]
    if fb == lb:
        return f"{prefix} {bnames[fb-1]}.{fu}–{lu}"
    return f"{prefix} {bnames[fb-1]}.{fu}–{bnames[lb-1]}.{lu}"


# ── Claude prompts ─────────────────────────────────────────────────────────────

_CLAUDE_MODEL = "claude-sonnet-4-6"


def _prose_text_block(units_data: list[tuple[int, int, str]], cfg: dict) -> str:
    bnames = cfg["book_names"]
    book_notes = cfg.get("book_notes", {})
    lines = []
    prev_book = None
    for book, unit_num, text in units_data:
        if book != prev_book:
            note = book_notes.get(book, "")
            label = f"[Book {bnames[book-1]}" + (f", {note}]" if note else "]")
            lines.append(label)
            prev_book = book
        lines.append(f"  Chapter {unit_num}: {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _verse_text_block(units_data: list[tuple[int, int, str]], cfg: dict) -> str:
    bnames = cfg["book_names"]
    lines = []
    prev_book = None
    for book, unit_num, text in units_data:
        if book != prev_book:
            lines.append(f"[Book {bnames[book-1]}]")
            prev_book = book
        lines.append(f"  {unit_num:>4}  {text}")
    return "\n".join(lines)


def _build_prose_prompt(units_data, day, range_str, cfg):
    latin_block = _prose_text_block(units_data, cfg)
    fact_cats = cfg["fact_categories"]
    hist_cats = cfg["historical_note_categories"]
    fact_cat = fact_cats[day % len(fact_cats)]
    hist_cat = hist_cats[day % len(hist_cats)]

    return f"""You are a classicist writing a daily reading guide for a Latin learner studying {cfg['title']}. Today is reading day {day}; the passage is {range_str}.

Here is the Latin text:

{latin_block}

Return ONLY a valid JSON object with exactly these keys — no markdown, no prose outside the JSON:

{{
  "narrative_anchor": "<2–3 sentences: where are we in the campaign, what just happened, what is coming. Assume reader knows the broad outline but may have lost the daily thread.>",
  "vocabulary": [
    {{
      "lemma": "<dictionary headword>",
      "pos": "<e.g. 'noun, 2nd decl., m.' or 'verb, 3rd conj.' — be precise>",
      "definition": "<definition specifically in context of this passage>",
      "parallel": "<one other notable occurrence in BG, Cicero, Livy, or Latin canon with author+location — or null>"
    }}
  ],
  "constructions": [
    {{
      "name": "<construction name, e.g. 'ablative absolute', 'indirect statement (ACI)', 'gerundive of obligation', 'cum + subjunctive'>",
      "latin": "<exact Latin phrase from the passage>",
      "analysis": "<2–3 sentences: identify the morphological forms and explain the syntactic function in this sentence>",
      "pattern": "<one-line structural template, e.g. 'NP_abl + pple_abl → attendant circumstance'>"
    }}
  ],
  "close_reading": "<A single genuinely interesting question — about Caesar's self-presentation, his third-person narration, military strategy, propaganda vs. reality, political subtext, relationship with Roman readers. NOT a comprehension check. 2–4 sentences.>",
  "historical_note": {{
    "category": "<exactly one of: {' | '.join(hist_cats)}>",
    "content": "<3–5 sentences of genuine historical/geographical/ethnographic context that illuminates this passage>"
  }},
  "fact_of_the_day": {{
    "category": "<exactly one of: {' | '.join(fact_cats)}>",
    "content": "<3–5 sentences — erudite, surprising, the kind of thing you wouldn't find in a basic textbook>"
  }}
}}

Constraints:
- vocabulary: 5–8 words; prefer semantically rich, rare, or learner-tripping words
- constructions: 2–3 items; pick paradigmatic examples the learner should internalize; vary construction types
- rotate fact_of_the_day: day {day} % {len(fact_cats)} = {day % len(fact_cats)} → use "{fact_cat}"
- rotate historical_note: day {day} % {len(hist_cats)} = {day % len(hist_cats)} → use "{hist_cat}"
- tone: knowledgeable friend, scholarly but not dry
- return raw JSON only, starting with {{ and ending with }}"""


def _build_verse_prompt(units_data, day, range_str, cfg):
    latin_block = _verse_text_block(units_data, cfg)
    fact_cats = cfg["fact_categories"]
    fact_cat = fact_cats[day % len(fact_cats)]

    return f"""You are a classicist writing a daily reading guide for a serious Latin learner studying {cfg['title']}. Today is reading day {day}; the passage is {range_str}.

Here is the Latin text:

{latin_block}

Return ONLY a valid JSON object with exactly these keys — no markdown, no prose outside the JSON:

{{
  "scansion_example": {{
    "line_num": <int — line number within its book>,
    "line_text": "<the full Latin line, verbatim>",
    "scansion": "<the line rewritten with macrons (ā ē ī ō ū) over long vowels, breves (ă ĕ ĭ ŏ ŭ) over short, vertical bar | for foot divisions, double bar || for the main caesura>",
    "note": "<2–3 sentences on what makes this line metrically interesting>"
  }},
  "scansion_challenge": {{
    "line_num": <int — a DIFFERENT line from the passage, one worth scanning>,
    "line_text": "<that line verbatim>"
  }},
  "vocabulary": [
    {{
      "lemma": "<dictionary headword>",
      "pos": "<e.g. 'noun, 2nd decl., m.' or 'verb, 3rd conj.' — be precise>",
      "definition": "<definition specifically in context of this passage>",
      "parallel": "<one other notable occurrence in the text or Latin canon — book.line reference if same work — or null>"
    }}
  ],
  "close_reading": "<A single genuinely interesting question — about rhetoric, imagery, intertextuality, characterisation, Augustan ideology. NOT a comprehension check. 2–4 sentences.>",
  "narrative_anchor": "<2–3 sentences: where are we in the story, what just happened, what is coming.>",
  "fact_of_the_day": {{
    "category": "<exactly one of: {' | '.join(fact_cats)}>",
    "content": "<3–5 sentences — erudite, surprising>"
  }}
}}

Constraints:
- vocabulary: 5–8 words; prefer rare, semantically rich, or learner-tripping words
- scansion: dactylic hexameter; mark every syllable accurately
- rotate fact_of_the_day: day {day} % {len(fact_cats)} = {day % len(fact_cats)} → use "{fact_cat}"
- tone: knowledgeable friend, scholarly but not dry
- return raw JSON only, starting with {{ and ending with }}"""


def generate_content(
    units_data: list[tuple[int, int, str]],
    day: int,
    range_str: str,
    cfg: dict,
) -> dict:
    if cfg["genre"] == "prose":
        prompt = _build_prose_prompt(units_data, day, range_str, cfg)
    else:
        prompt = _build_verse_prompt(units_data, day, range_str, cfg)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_H2 = ('font-size:10px;letter-spacing:2.5px;text-transform:uppercase;'
       'color:#8B6914;margin:0 0 12px;font-weight:normal;')


def _section_heading(label: str) -> str:
    return f'<h2 style="{_H2}">{label}</h2>'


def _cat_heading(label: str, category: str, color: str) -> str:
    return (
        f'<h2 style="{_H2}">'
        f'{label} &nbsp;·&nbsp; '
        f'<span style="color:{color};font-weight:normal;text-transform:none;">'
        f'{_esc(category.title())}'
        f'</span></h2>'
    )


def _vocab_html(vocabulary: list[dict]) -> str:
    items = []
    for word in vocabulary:
        parallel = word.get("parallel")
        par_html = (
            f'<br><span style="color:#777;font-size:12px;font-style:italic;">'
            f'&#8627; {_esc(parallel)}</span>'
            if parallel else ""
        )
        items.append(
            f'<div style="margin-bottom:13px;padding-bottom:13px;border-bottom:1px solid #f0e8d8;">'
            f'<span style="font-family:\'Courier New\',monospace;font-size:14px;'
            f'color:#5c3d11;font-weight:bold;">{_esc(word.get("lemma",""))}</span> '
            f'<span style="color:#999;font-size:11px;font-style:italic;">'
            f'{_esc(word.get("pos",""))}</span><br>'
            f'<span style="color:#333;font-size:14px;">{_esc(word.get("definition",""))}</span>'
            f'{par_html}'
            f'</div>'
        )
    return "\n".join(items)


def _constructions_html(constructions: list[dict]) -> str:
    cards = []
    for c in constructions:
        cards.append(
            f'<div style="background:#f0f5f0;border:1px solid #c0d5c0;border-radius:3px;'
            f'padding:16px 18px;margin-bottom:10px;">'
            f'<div style="font-size:10px;color:#2a5a2a;letter-spacing:1.5px;font-weight:bold;'
            f'text-transform:uppercase;margin-bottom:8px;">{_esc(c.get("name",""))}</div>'
            f'<div style="font-family:\'Courier New\',monospace;font-size:14px;color:#1a1a1a;'
            f'margin-bottom:8px;border-left:2px solid #4a8a4a;padding-left:10px;">'
            f'{_esc(c.get("latin",""))}</div>'
            f'<div style="font-size:13px;color:#333;line-height:1.75;margin-bottom:8px;">'
            f'{_esc(c.get("analysis",""))}</div>'
            f'<div style="font-size:11px;color:#777;font-style:italic;background:#e8f0e8;'
            f'padding:4px 8px;border-radius:2px;display:inline-block;">'
            f'{_esc(c.get("pattern",""))}</div>'
            f'</div>'
        )
    return "\n".join(cards)


def _scansion_html(sc: dict, ch: dict) -> str:
    return (
        f'<div style="background:#f0f5f0;border:1px solid #c0d5c0;border-radius:3px;'
        f'padding:16px 18px;margin-bottom:10px;">'
        f'<div style="font-size:10px;color:#666;margin-bottom:6px;letter-spacing:1px;">'
        f'LINE {sc.get("line_num","")}</div>'
        f'<div style="font-family:\'Courier New\',monospace;font-size:13px;color:#1a1a1a;'
        f'margin-bottom:8px;">{_esc(sc.get("line_text",""))}</div>'
        f'<div style="font-family:\'Courier New\',monospace;font-size:13px;color:#2a5a2a;'
        f'letter-spacing:1px;">{_esc(sc.get("scansion",""))}</div>'
        f'<div style="margin-top:10px;font-size:13px;color:#444;line-height:1.7;">'
        f'{sc.get("note","")}</div>'
        f'</div>'
        f'<div style="background:#fff8ee;border:1px solid #e8d0a0;border-radius:3px;'
        f'padding:16px 18px;">'
        f'<div style="font-size:10px;color:#8B6914;margin-bottom:6px;letter-spacing:1px;'
        f'font-weight:bold;">YOUR TURN — scan this before looking:</div>'
        f'<div style="font-family:\'Courier New\',monospace;font-size:13px;color:#1a1a1a;">'
        f'{_esc(ch.get("line_text",""))} '
        f'<span style="color:#bbb;font-size:11px;">(line {ch.get("line_num","")})</span>'
        f'</div>'
        f'</div>'
    )


def _prose_latin_html(units_data: list[tuple[int, int, str]], cfg: dict) -> str:
    bnames = cfg["book_names"]
    book_notes = cfg.get("book_notes", {})
    rows = []
    prev_book = None
    for book, unit_num, text in units_data:
        if book != prev_book:
            if prev_book is not None:
                rows.append('<div style="height:10px;"></div>')
            note = book_notes.get(book, "")
            book_label = f"Book {bnames[book-1]}"
            note_html = (
                f' <span style="font-weight:normal;font-style:italic;">({_esc(note)})</span>'
                if note else ""
            )
            rows.append(
                f'<div style="font-family:Georgia,serif;font-size:11px;color:#8B6914;'
                f'letter-spacing:1px;margin-bottom:8px;font-weight:bold;">'
                f'{_esc(book_label)}{note_html}</div>'
            )
            prev_book = book
        rows.append(
            f'<div style="margin-bottom:16px;">'
            f'<div style="font-size:10px;color:#bbb;letter-spacing:1px;margin-bottom:4px;">'
            f'CHAPTER {unit_num}</div>'
            f'<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:14px;'
            f'color:#1a1a1a;line-height:1.9;">{_esc(text)}</div>'
            f'</div>'
        )
    return "\n".join(rows)


def _verse_latin_html(units_data: list[tuple[int, int, str]], cfg: dict) -> str:
    bnames = cfg["book_names"]
    rows = []
    prev_book = None
    for book, unit_num, text in units_data:
        if book != prev_book:
            if prev_book is not None:
                rows.append('<tr><td colspan="2" style="padding:6px 0 2px;"></td></tr>')
            rows.append(
                f'<tr><td colspan="2" style="font-family:Georgia,serif;font-size:11px;'
                f'color:#8B6914;font-style:italic;padding:2px 0 5px;">'
                f'Book {bnames[book-1]}</td></tr>'
            )
            prev_book = book
        rows.append(
            f'<tr>'
            f'<td style="font-family:monospace;font-size:12px;color:#aaa;'
            f'padding:1px 14px 1px 0;white-space:nowrap;vertical-align:top;">{unit_num}</td>'
            f'<td style="font-family:\'Courier New\',Courier,monospace;font-size:14px;'
            f'color:#1a1a1a;padding:1px 0;line-height:1.75;">{_esc(text)}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


# ── HTML assembler ─────────────────────────────────────────────────────────────

def build_html(
    units_data: list[tuple[int, int, str]],
    content: dict,
    day: int,
    range_str: str,
    start_global: int,
    end_global: int,
    cfg: dict,
) -> str:
    tot = total_units(cfg)
    progress_pct = round(end_global / tot * 100, 1)
    genre = cfg["genre"]
    unit_type_pl = cfg["unit_type"].capitalize() + "s"

    # ── Latin text ────────────────────────────────────────────────────────────
    if genre == "prose":
        latin_inner = _prose_latin_html(units_data, cfg)
        latin_section = (
            f'<div style="background:#f9f6f0;border:1px solid #e5d9c5;border-radius:3px;'
            f'padding:18px 20px;margin-bottom:28px;">{latin_inner}</div>'
        )
    else:
        latin_rows = _verse_latin_html(units_data, cfg)
        latin_section = (
            f'<div style="background:#f9f6f0;border:1px solid #e5d9c5;border-radius:3px;'
            f'padding:18px 20px;margin-bottom:28px;overflow-x:auto;">'
            f'<table style="border-collapse:collapse;width:100%;">{latin_rows}</table></div>'
        )

    # ── Genre-specific analysis ───────────────────────────────────────────────
    if genre == "prose":
        analysis_block = (
            f'{_section_heading("Grammar &amp; Constructions")}'
            f'<div style="margin-bottom:28px;">'
            f'{_constructions_html(content.get("constructions", []))}'
            f'</div>'
        )
    else:
        sc = content.get("scansion_example", {})
        ch = content.get("scansion_challenge", {})
        analysis_block = (
            f'{_section_heading("Scansion")}'
            f'<div style="margin-bottom:28px;">{_scansion_html(sc, ch)}</div>'
        )

    # ── Historical note (prose only) ──────────────────────────────────────────
    hist_block = ""
    if genre == "prose":
        hist = content.get("historical_note", {})
        hist_cat = hist.get("category", "")
        hist_color = cfg["historical_note_colors"].get(hist_cat, "#555")
        hist_block = (
            f'{_cat_heading("Historical Note", hist_cat, hist_color)}'
            f'<div style="border-left:3px solid {hist_color};padding-left:16px;'
            f'color:#333;line-height:1.8;font-size:14px;margin-bottom:28px;">'
            f'{_esc(hist.get("content",""))}</div>'
        )

    # ── Fact of the day ───────────────────────────────────────────────────────
    fact = content.get("fact_of_the_day", {})
    fact_cat = fact.get("category", "")
    fact_color = cfg["fact_colors"].get(fact_cat, "#555")

    return f"""<!DOCTYPE html>
<html lang="la">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(range_str)}</title>
</head>
<body style="margin:0;padding:0;background:#f0ebe0;font-family:Georgia,'Times New Roman',serif;">
<div style="max-width:640px;margin:0 auto;padding:20px 12px 40px;">

  <!-- HEADER -->
  <div style="background:#2c1810;border-radius:4px 4px 0 0;padding:28px 28px 22px;">
    <div style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#b89a6a;margin-bottom:8px;">
      Day {day:,} &nbsp;·&nbsp; {unit_type_pl} {start_global:,}–{end_global:,} of {tot:,} &nbsp;·&nbsp; {progress_pct}% complete
    </div>
    <h1 style="margin:0;font-size:24px;font-weight:normal;color:#f5f0e8;letter-spacing:0.3px;">
      {_esc(range_str)}
    </h1>
    <div style="margin-top:14px;background:rgba(255,255,255,0.12);border-radius:2px;height:3px;">
      <div style="background:#b89a6a;height:3px;border-radius:2px;width:{progress_pct}%;max-width:100%;"></div>
    </div>
  </div>

  <!-- BODY -->
  <div style="background:#fffef9;border:1px solid #ddd3bf;border-top:none;border-radius:0 0 4px 4px;padding:28px 28px 32px;">

    <!-- Narrative anchor -->
    <div style="border-left:3px solid #c4a060;padding:2px 0 2px 16px;color:#555;font-style:italic;line-height:1.75;margin-bottom:28px;font-size:14px;">
      {content.get("narrative_anchor","")}
    </div>

    <!-- LATIN TEXT -->
    {_section_heading("The Text")}
    {latin_section}

    <!-- ANALYSIS: constructions (prose) or scansion (verse) -->
    {analysis_block}

    <!-- VOCABULARY -->
    {_section_heading("Vocabulary")}
    <div style="margin-bottom:28px;">
      {_vocab_html(content.get("vocabulary", []))}
    </div>

    <!-- CLOSE READING -->
    {_section_heading("Close Reading")}
    <div style="background:#f5f0ff;border:1px solid #cbbfdf;border-radius:3px;padding:18px 20px;margin-bottom:28px;font-size:15px;line-height:1.8;color:#2a1a44;">
      {content.get("close_reading","")}
    </div>

    <!-- HISTORICAL NOTE (prose only) -->
    {hist_block}

    <!-- FACT OF THE DAY -->
    {_cat_heading("Fact of the Day", fact_cat, fact_color)}
    <div style="border-left:3px solid {fact_color};padding-left:16px;color:#333;line-height:1.8;font-size:14px;margin-bottom:24px;">
      {fact.get("content","")}
    </div>

    <!-- FOOTER -->
    <div style="border-top:1px solid #e5d9c5;padding-top:16px;font-size:11px;color:#bbb;text-align:center;letter-spacing:1px;">
      {_esc(cfg["footer_motto"])} &nbsp;·&nbsp; DAY {day}
    </div>

  </div>
</div>
</body>
</html>"""


# ── Placeholder content (--no-ai) ──────────────────────────────────────────────

def _placeholder_content(cfg: dict, units_data: list) -> dict:
    genre = cfg["genre"]
    base = {
        "narrative_anchor": "Narrative context generated by Claude API (remove --no-ai).",
        "vocabulary": [{"lemma": "exemplum", "pos": "noun, 2nd decl., n.",
                        "definition": "Vocabulary generated by Claude API.", "parallel": None}],
        "close_reading": "Close reading question generated by Claude API (remove --no-ai).",
        "fact_of_the_day": {
            "category": cfg["fact_categories"][0],
            "content": "Fact generated by Claude API (remove --no-ai).",
        },
    }
    if genre == "prose":
        base["constructions"] = [{
            "name": "ablative absolute",
            "latin": "(run without --no-ai)",
            "analysis": "Constructions generated by Claude API.",
            "pattern": "NP_abl + pple_abl → attendant circumstance",
        }]
        base["historical_note"] = {
            "category": cfg["historical_note_categories"][0],
            "content": "Historical note generated by Claude API (remove --no-ai).",
        }
    else:
        base["scansion_example"] = {
            "line_num": units_data[0][1], "line_text": units_data[0][2],
            "scansion": "(run without --no-ai)", "note": "",
        }
        base["scansion_challenge"] = {
            "line_num": units_data[-1][1], "line_text": units_data[-1][2],
        }
    return base


# ── Gmail SMTP ─────────────────────────────────────────────────────────────────

def send_email(html: str, subject: str, cfg: dict) -> None:
    user      = os.environ["GMAIL_USER"]
    password  = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{cfg['sender_name']} <{user}>"
    msg["To"]      = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, recipient, msg.as_string())

    print(f"  Sent to {recipient}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    text_key = os.environ.get("LATIN_TEXT", "caesar_bg")
    if text_key not in TEXT_CONFIGS:
        print(
            f"ERROR: Unknown LATIN_TEXT='{text_key}'. "
            f"Options: {list(TEXT_CONFIGS)}",
            file=sys.stderr,
        )
        sys.exit(1)
    cfg = TEXT_CONFIGS[text_key]

    parser = argparse.ArgumentParser(description=f"Send daily {cfg['title']} email")
    parser.add_argument("--day", type=int, metavar="N",
                        help="Override reading day number (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Save preview.html instead of sending")
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip Claude API call (layout test only)")
    args = parser.parse_args()

    data_path = Path(__file__).parent / cfg["data_file"]
    if not data_path.exists():
        print(
            f"ERROR: {cfg['data_file']} not found. "
            f"Run: python fetch_text.py --text {text_key}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)

    if args.day:
        day = args.day
    else:
        today = date.today()
        day = reading_day_for(today, cfg)
        if day is None:
            rest_name = calendar.day_name[cfg["rest_weekday"]]
            if today < cfg["start_date"]:
                print(f"Reading plan starts {cfg['start_date']}.")
            elif today.weekday() == cfg["rest_weekday"]:
                print(f"{rest_name} — rest day. No email sent.")
            else:
                print("No reading day calculated.")
            return

    unit_range = global_unit_range(day, cfg)
    if unit_range is None:
        print(f"{cfg['title']} is complete. Congratulations.")
        return

    start_global, end_global = unit_range
    units_data = extract_units(data, start_global, end_global)
    range_str  = describe_range(units_data, cfg)

    unit_pl = cfg["unit_type"] + "s"
    print(f"Day {day}: {range_str} ({len(units_data)} {unit_pl}, global {start_global}–{end_global})")

    if args.no_ai:
        content = _placeholder_content(cfg, units_data)
    else:
        print("Generating scholarly content via Claude API...")
        content = generate_content(units_data, day, range_str, cfg)

    html    = build_html(units_data, content, day, range_str, start_global, end_global, cfg)
    subject = f"{cfg['short_title']} · {range_str} · Day {day}"

    if args.dry_run:
        out = Path("preview.html")
        out.write_text(html, encoding="utf-8")
        print(f"Dry run — saved to {out}")
    else:
        print("Sending email...")
        send_email(html, subject, cfg)


if __name__ == "__main__":
    main()
