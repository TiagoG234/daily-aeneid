#!/usr/bin/env python3
"""
aeneid_email.py — Daily Aeneid reading email.

Usage:
    python aeneid_email.py                  # send today's email
    python aeneid_email.py --day 47         # send day 47's email
    python aeneid_email.py --dry-run        # save preview.html, do not send
    python aeneid_email.py --day 1 --dry-run
"""

import argparse
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

# ── Constants ──────────────────────────────────────────────────────────────────

START_DATE = date(2026, 5, 16)
LINES_PER_DAY = 27
REST_WEEKDAY = 2  # Wednesday (Monday=0, …, Sunday=6)

BOOK_NAMES = ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII"]
# Actual counts from the Latin Library text (minor variants vs. Mynors 9896)
BOOK_LINE_COUNTS = [756, 803, 718, 705, 871, 901, 817, 731, 815, 907, 915, 950]
TOTAL_LINES = sum(BOOK_LINE_COUNTS)  # 9889

# ── Reading day / line arithmetic ──────────────────────────────────────────────

def reading_day_for(target: date) -> int | None:
    """1-indexed reading day for `target`, or None if rest/before-start."""
    if target < START_DATE or target.weekday() == REST_WEEKDAY:
        return None
    count = 0
    d = START_DATE
    while d <= target:
        if d.weekday() != REST_WEEKDAY:
            count += 1
        d += timedelta(days=1)
    return count


def global_line_range(day: int) -> tuple[int, int] | None:
    """Inclusive global (1-indexed) line range for reading day N, or None if done."""
    start = (day - 1) * LINES_PER_DAY + 1
    if start > TOTAL_LINES:
        return None
    return start, min(day * LINES_PER_DAY, TOTAL_LINES)


def extract_lines(
    aeneid: list[dict],
    start_global: int,
    end_global: int,
) -> list[tuple[int, int, str]]:
    """Return [(book_num, line_within_book, text), …] for the global range."""
    result = []
    cumulative = 0
    for book_data in aeneid:
        book_num = book_data['book']
        book_lines = book_data['lines']
        book_end = cumulative + len(book_lines)

        if end_global <= cumulative or start_global > book_end:
            cumulative = book_end
            continue

        local_start = max(start_global, cumulative + 1) - cumulative - 1
        local_end   = min(end_global, book_end) - cumulative - 1
        for i in range(local_start, local_end + 1):
            if i < len(book_lines):
                result.append((book_num, i + 1, book_lines[i]))

        cumulative = book_end
    return result


def describe_range(lines_data: list[tuple[int, int, str]]) -> str:
    """E.g. 'Aeneid III.240–266' or 'Aeneid III.750–IV.6'."""
    if not lines_data:
        return ""
    fb, fl, _ = lines_data[0]
    lb, ll, _ = lines_data[-1]
    if fb == lb:
        return f"Aeneid {BOOK_NAMES[fb-1]}.{fl}–{ll}"
    return f"Aeneid {BOOK_NAMES[fb-1]}.{fl}–{BOOK_NAMES[lb-1]}.{ll}"


# ── Claude API ─────────────────────────────────────────────────────────────────

_CLAUDE_MODEL = "claude-sonnet-4-6"

def generate_content(
    lines_data: list[tuple[int, int, str]],
    day: int,
    range_str: str,
) -> dict:
    """Call Claude to produce all scholarly email sections as a JSON dict."""

    latin_block = "\n".join(
        f"  {line_num:>4}  {text}"
        for _, line_num, text in lines_data
    )

    # Indicate book transitions within the block
    if lines_data[0][0] != lines_data[-1][0]:
        annotated = []
        prev_book = None
        for book, line_num, text in lines_data:
            if book != prev_book:
                annotated.append(f"[Book {BOOK_NAMES[book-1]}]")
                prev_book = book
            annotated.append(f"  {line_num:>4}  {text}")
        latin_block = "\n".join(annotated)

    prompt = f"""You are a classicist writing a daily reading guide for a serious Latin learner studying Virgil's Aeneid. Today is reading day {day}; the passage is {range_str}.

Here is the Latin text:

{latin_block}

Return ONLY a valid JSON object with exactly these keys — no markdown, no prose outside the JSON:

{{
  "scansion_example": {{
    "line_num": <int — line number within its book>,
    "line_text": "<the full Latin line, verbatim>",
    "scansion": "<the line rewritten with macrons (ā ē ī ō ū) over long vowels, breves (ă ĕ ĭ ŏ ŭ) over short, vertical bar | for foot divisions, double bar || for the main caesura>",
    "note": "<2–3 sentences on what makes this line metrically interesting — spondaic weight, elision, rhythm matching sense, etc.>"
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
      "parallel": "<one other notable occurrence in the Aeneid or Latin canon — book.line reference if Aeneid — or null if none>"
    }}
  ],
  "close_reading": "<A single genuinely interesting question to sit with — about rhetoric, imagery, Homeric intertextuality, characterisation, Augustan ideology, Stoic resonance, etc. NOT a comprehension check. 2–4 sentences.>",
  "narrative_anchor": "<2–3 sentences: where are we in the story, what just happened, what is coming. Assume the reader knows the Aeneid broadly but may have lost the thread day-to-day.>",
  "fact_of_the_day": {{
    "category": "<exactly one of: compositional technique | manuscript tradition | reception history | metrical anomaly | intertextual echo | biographical context>",
    "content": "<3–5 sentences — erudite, surprising, the kind of thing you wouldn't find in a basic textbook introduction>"
  }}
}}

Constraints:
- vocabulary: 5–8 words; prefer rare, semantically rich, or learner-tripping words over common ones
- scansion: be accurate; dactylic hexameter; mark every syllable
- rotate fact_of_the_day category so day {day} % 6 = {day % 6} maps to: 0→compositional technique, 1→manuscript tradition, 2→reception history, 3→metrical anomaly, 4→intertextual echo, 5→biographical context
- tone: knowledgeable friend, scholarly but not dry
- return raw JSON only, starting with {{ and ending with }}"""

    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    message = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    # Strip any accidental markdown fences
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


# ── HTML builder ───────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def build_html(
    lines_data: list[tuple[int, int, str]],
    content: dict,
    day: int,
    range_str: str,
    start_global: int,
    end_global: int,
) -> str:
    progress_pct = round(end_global / TOTAL_LINES * 100, 1)

    # ── Latin text table ──────────────────────────────────────────────────────
    latin_rows = []
    prev_book = None
    for book, line_num, text in lines_data:
        if book != prev_book:
            if prev_book is not None:
                latin_rows.append('<tr><td colspan="2" style="padding:6px 0 2px;"></td></tr>')
            latin_rows.append(
                f'<tr><td colspan="2" style="font-family:Georgia,serif;font-size:11px;'
                f'color:#8B6914;font-style:italic;padding:2px 0 5px;">Book {BOOK_NAMES[book-1]}</td></tr>'
            )
            prev_book = book
        latin_rows.append(
            f'<tr>'
            f'<td style="font-family:monospace;font-size:12px;color:#aaa;'
            f'padding:1px 14px 1px 0;white-space:nowrap;vertical-align:top;">{line_num}</td>'
            f'<td style="font-family:\'Courier New\',Courier,monospace;font-size:14px;'
            f'color:#1a1a1a;padding:1px 0;line-height:1.75;">{_esc(text)}</td>'
            f'</tr>'
        )
    latin_table = "\n".join(latin_rows)

    # ── Vocabulary ────────────────────────────────────────────────────────────
    vocab_items = []
    for word in content.get('vocabulary', []):
        parallel = word.get('parallel')
        parallel_html = (
            f'<br><span style="color:#777;font-size:12px;font-style:italic;">↳ {_esc(parallel)}</span>'
            if parallel else ""
        )
        vocab_items.append(
            f'<div style="margin-bottom:13px;padding-bottom:13px;border-bottom:1px solid #f0e8d8;">'
            f'<span style="font-family:\'Courier New\',monospace;font-size:14px;color:#5c3d11;font-weight:bold;">'
            f'{_esc(word.get("lemma",""))}</span> '
            f'<span style="color:#999;font-size:11px;font-style:italic;">{_esc(word.get("pos",""))}</span><br>'
            f'<span style="color:#333;font-size:14px;">{_esc(word.get("definition",""))}</span>'
            f'{parallel_html}'
            f'</div>'
        )
    vocab_html = "\n".join(vocab_items)

    # ── Scansion ──────────────────────────────────────────────────────────────
    sc = content.get('scansion_example', {})
    ch = content.get('scansion_challenge', {})

    # ── Fact of the day ───────────────────────────────────────────────────────
    fact = content.get('fact_of_the_day', {})
    fact_colors = {
        'compositional technique': '#2d5a27',
        'manuscript tradition':    '#1a3a5c',
        'reception history':       '#5c1a3a',
        'metrical anomaly':        '#5c3d11',
        'intertextual echo':       '#1a455c',
        'biographical context':    '#3d2d5c',
    }
    fact_color = fact_colors.get(fact.get('category', ''), '#555')

    # ── Assemble HTML ─────────────────────────────────────────────────────────
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
      Day {day:,} &nbsp;·&nbsp; Lines {start_global:,}–{end_global:,} of {TOTAL_LINES:,} &nbsp;·&nbsp; {progress_pct}% complete
    </div>
    <h1 style="margin:0;font-size:24px;font-weight:normal;color:#f5f0e8;letter-spacing:0.3px;">
      {_esc(range_str)}
    </h1>
    <!-- progress bar -->
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
    <h2 style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#8B6914;margin:0 0 12px;font-weight:normal;">The Text</h2>
    <div style="background:#f9f6f0;border:1px solid #e5d9c5;border-radius:3px;padding:18px 20px;margin-bottom:28px;overflow-x:auto;">
      <table style="border-collapse:collapse;width:100%;">
        {latin_table}
      </table>
    </div>

    <!-- SCANSION -->
    <h2 style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#8B6914;margin:0 0 12px;font-weight:normal;">Scansion</h2>
    <div style="margin-bottom:28px;">
      <div style="background:#f0f5f0;border:1px solid #c0d5c0;border-radius:3px;padding:16px 18px;margin-bottom:10px;">
        <div style="font-size:10px;color:#666;margin-bottom:6px;letter-spacing:1px;">LINE {sc.get("line_num","")}</div>
        <div style="font-family:'Courier New',monospace;font-size:13px;color:#1a1a1a;margin-bottom:8px;">{_esc(sc.get("line_text",""))}</div>
        <div style="font-family:'Courier New',monospace;font-size:13px;color:#2a5a2a;letter-spacing:1px;">{_esc(sc.get("scansion",""))}</div>
        <div style="margin-top:10px;font-size:13px;color:#444;line-height:1.7;">{sc.get("note","")}</div>
      </div>
      <div style="background:#fff8ee;border:1px solid #e8d0a0;border-radius:3px;padding:16px 18px;">
        <div style="font-size:10px;color:#8B6914;margin-bottom:6px;letter-spacing:1px;font-weight:bold;">YOUR TURN — scan this before looking:</div>
        <div style="font-family:'Courier New',monospace;font-size:13px;color:#1a1a1a;">{_esc(ch.get("line_text",""))} <span style="color:#bbb;font-size:11px;">(line {ch.get("line_num","")})</span></div>
      </div>
    </div>

    <!-- VOCABULARY -->
    <h2 style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#8B6914;margin:0 0 12px;font-weight:normal;">Vocabulary</h2>
    <div style="margin-bottom:28px;">
      {vocab_html}
    </div>

    <!-- CLOSE READING -->
    <h2 style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#8B6914;margin:0 0 12px;font-weight:normal;">Close Reading</h2>
    <div style="background:#f5f0ff;border:1px solid #cbbfdf;border-radius:3px;padding:18px 20px;margin-bottom:28px;font-size:15px;line-height:1.8;color:#2a1a44;">
      {content.get("close_reading","")}
    </div>

    <!-- FACT OF THE DAY -->
    <h2 style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#8B6914;margin:0 0 12px;font-weight:normal;">
      Aeneid Fact &nbsp;·&nbsp; <span style="color:{fact_color};font-weight:normal;text-transform:none;">{fact.get("category","").title()}</span>
    </h2>
    <div style="border-left:3px solid {fact_color};padding-left:16px;color:#333;line-height:1.8;font-size:14px;margin-bottom:24px;">
      {fact.get("content","")}
    </div>

    <!-- FOOTER -->
    <div style="border-top:1px solid #e5d9c5;padding-top:16px;font-size:11px;color:#bbb;text-align:center;letter-spacing:1px;">
      ARMA VIRUMQUE CANO &nbsp;·&nbsp; DAY {day}
    </div>

  </div>
</div>
</body>
</html>"""


# ── Gmail SMTP ─────────────────────────────────────────────────────────────────

def send_email(html: str, subject: str) -> None:
    user      = os.environ['GMAIL_USER']
    password  = os.environ['GMAIL_APP_PASSWORD']
    recipient = os.environ.get('RECIPIENT_EMAIL', user)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = f"Daily Aeneid <{user}>"
    msg['To']      = recipient
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, recipient, msg.as_string())

    print(f"  Sent to {recipient}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='Send daily Aeneid email')
    parser.add_argument('--day', type=int, metavar='N',
                        help='Override reading day number (default: today)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Save preview.html instead of sending')
    parser.add_argument('--no-ai', action='store_true',
                        help='Skip Claude API call (layout test only, placeholder content)')
    args = parser.parse_args()

    aeneid_path = Path(__file__).parent / 'aeneid.json'
    if not aeneid_path.exists():
        print("ERROR: aeneid.json not found. Run fetch_text.py first.", file=sys.stderr)
        sys.exit(1)

    with open(aeneid_path, encoding='utf-8') as f:
        aeneid = json.load(f)

    if args.day:
        day = args.day
    else:
        today = date.today()
        day = reading_day_for(today)
        if day is None:
            if today < START_DATE:
                print(f"Reading plan starts {START_DATE}.")
            elif today.weekday() == REST_WEEKDAY:
                print("Wednesday — rest day. No email sent.")
            else:
                print("No reading day calculated.")
            return

    line_range = global_line_range(day)
    if line_range is None:
        print("The Aeneid is complete. Congratulations.")
        return

    start_global, end_global = line_range
    lines_data = extract_lines(aeneid, start_global, end_global)
    range_str  = describe_range(lines_data)

    print(f"Day {day}: {range_str} ({len(lines_data)} lines, global {start_global}–{end_global})")

    if args.no_ai:
        # Skeleton content for layout testing without an API key
        content = {
            "scansion_example": {"line_num": lines_data[0][1], "line_text": lines_data[0][2],
                "scansion": "(scansion — run without --no-ai)", "note": ""},
            "scansion_challenge": {"line_num": lines_data[-1][1], "line_text": lines_data[-1][2]},
            "vocabulary": [{"lemma": "—", "pos": "—", "definition": "Vocabulary generated by Claude API.", "parallel": None}],
            "close_reading": "Close reading question generated by Claude API (remove --no-ai).",
            "narrative_anchor": "Narrative context generated by Claude API (remove --no-ai).",
            "fact_of_the_day": {"category": "compositional technique", "content": "Fact generated by Claude API (remove --no-ai)."},
        }
    else:
        print("Generating scholarly content via Claude API...")
        content = generate_content(lines_data, day, range_str)

    html    = build_html(lines_data, content, day, range_str, start_global, end_global)
    subject = f"Aeneid · {range_str} · Day {day}"

    if args.dry_run:
        out = Path('preview.html')
        out.write_text(html, encoding='utf-8')
        print(f"Dry run — saved to {out}")
    else:
        print("Sending email...")
        send_email(html, subject)


if __name__ == '__main__':
    main()
