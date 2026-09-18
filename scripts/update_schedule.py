#!/usr/bin/env python3
"""
Update data/schedule.json with fresh entries scraped from the college's
"Зміни до розкладу" Google Sheets.

Runs unattended on GitHub Actions (no confirmation gate). Uses the Google
Visualization API's CSV export (gviz/tq) endpoint — the plain "/export?
format=csv" endpoint gets blocked (HTTP 400) when called from GitHub
Actions' datacenter IP ranges. The gviz endpoint can lag a live edit by a
few minutes due to server-side caching, which is acceptable for a job that
reruns every ~30 minutes.

Never touches dates before today (Europe/Kyiv). For today..+RANGE_DAYS it
re-checks the source and:
  - if the sheet's own header date matches the target date -> writes a full
    "ok" day with all groups/subjects/pairs.
  - if it doesn't match (college hasn't published that day yet) -> writes
    "no_data", UNLESS a good "ok" day is already stored for that date (never
    downgrade a day that was already filled in).
Room numbers are filled in only when the "Розміщення груп по аудиторіях" tab
carries a heading date that matches the target date; otherwise every room is
left as "—" rather than guessing.
"""
import csv
import io
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
RANGE_DAYS = 6  # today + this many days ahead
ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "schedule.json"

# Weekday index (Mon=0 .. Sat=5); Sunday has no classes and is skipped.
SPREADSHEETS = {
    0: "1lok-vuNC6Nx_Dx4w2vhRy8bnR0A6ssq2WUXtClGWj9Q",  # Понеділок
    1: "10UugoyVXw4mwzgFjqO6pnr1v5ofPDQRdjE8NGy_fVRQ",  # Вівторок
    2: "1VvEML21gmiHdYIMB2aq-B9Ea7n8w_F9YrtTsz5mtq50",  # Середа
    3: "1zPrelCai8jGVcZMREDGltl8yIpLGXqr_288uTwtjVG0",  # Четвер
    4: "1I0TjCHqnEwaNFQrTaj86z_iII-7i_Xl9s7JiIupEURo",  # П'ятниця
    5: "1Uk4LNAHU22luWeAYIidY5jQ2N5dyGlUrwI2SFphQ3pc",  # Субота
}
WEEKDAY_FULL = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
TAB_SUBJECTS_1 = 0
TAB_SUBJECTS_2 = 1587751514
TAB_ROOMS = 436522941

UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(UA_MONTHS) + r")\s+(\d{4})", re.UNICODE)
GROUP_RE = re.compile(r"^\d{2,3}(\(\d+\))?-[A-ZА-ЯЁІЇЄҐ]{1,4}$", re.UNICODE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
}


def fetch_csv(sheet_id: str, gid: int) -> str:
    # The undocumented "/export?format=csv" endpoint started returning
    # "400 Bad Request" for requests coming from GitHub Actions' IP ranges
    # (Google appears to be blocking it for cloud-CI datacenter IPs). The
    # official Google Visualization API endpoint below is more tolerant of
    # automated traffic; the tradeoff is a few minutes of server-side
    # caching, which is fine for a job that reruns every ~30 minutes.
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid={gid}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", errors="replace")


def parse_rows(text: str):
    return [row for row in csv.reader(io.StringIO(text))]


def extract_date_iso(rows) -> str | None:
    """Search the first few rows for a Ukrainian date phrase like '16 вересня 2026'."""
    head_text = " ".join(cell for row in rows[:4] for cell in row)
    m = DATE_RE.search(head_text)
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = UA_MONTHS[month_name]
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_subject_tab(rows):
    """Yields (pair, group, subject) tuples from one subjects tab."""
    header_cols = None
    pair_counter = 0
    for i, row in enumerate(rows):
        if not row:
            continue
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        col0 = cells[0]
        group_hits = sum(1 for c in cells[1:] if GROUP_RE.match(c))
        is_header = group_hits >= 2 and (i == 0 or col0 == "")
        if is_header:
            header_cols = cells
            pair_counter = 0
            continue
        if header_cols is None:
            continue
        pair_counter += 1
        if pair_counter > 6:
            continue
        for j in range(1, len(header_cols)):
            group = header_cols[j] if j < len(header_cols) else ""
            if not group or not GROUP_RE.match(group):
                continue
            subject = cells[j] if j < len(cells) else ""
            if subject in ("", "-", "—"):
                continue
            yield pair_counter, group, subject


def parse_room_tab(rows, target_iso: str):
    """Returns {(group, pair): room} if the tab's own heading date matches
    target_iso, else an empty dict (caller falls back to '—')."""
    heading_iso = extract_date_iso(rows[:5])
    if heading_iso != target_iso:
        return {}
    rooms = {}
    for row in rows:
        cells = [c.strip() for c in row]
        if not cells or not GROUP_RE.match(cells[0]):
            continue
        group = cells[0]
        for pair_idx, cell in enumerate(cells[1:7], start=1):
            if cell and cell not in ("-", "—"):
                rooms[(group, pair_idx)] = cell
    return rooms


def build_day(target_iso: str, weekday_idx: int) -> dict:
    sheet_id = SPREADSHEETS[weekday_idx]
    weekday_name = WEEKDAY_FULL[weekday_idx]
    try:
        rows1 = parse_rows(fetch_csv(sheet_id, TAB_SUBJECTS_1))
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ! fetch failed for {target_iso}: {e}", file=sys.stderr)
        return None  # leave existing data (if any) untouched

    sheet_date = extract_date_iso(rows1)
    if sheet_date != target_iso:
        return {"date": target_iso, "weekday": weekday_name, "status": "no_data", "entries": []}

    try:
        rows2 = parse_rows(fetch_csv(sheet_id, TAB_SUBJECTS_2))
    except (urllib.error.URLError, TimeoutError):
        rows2 = []

    try:
        rooms_rows = parse_rows(fetch_csv(sheet_id, TAB_ROOMS))
        rooms = parse_room_tab(rooms_rows, target_iso)
    except (urllib.error.URLError, TimeoutError):
        rooms = {}

    entries = []
    for rows in (rows1, rows2):
        for pair, group, subject in parse_subject_tab(rows):
            room = rooms.get((group, pair), "—")
            entries.append({"pair": pair, "group": group, "subject": subject, "room": room})

    return {"date": target_iso, "weekday": weekday_name, "status": "ok", "entries": entries}


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    store = {"updatedAt": None, "days": {}}
    if DATA_FILE.exists():
        store = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        store.setdefault("days", {})

    now_kyiv = datetime.now(KYIV)
    today = now_kyiv.date()

    for offset in range(0, RANGE_DAYS + 1):
        d = today + timedelta(days=offset)
        weekday_idx = d.weekday()  # Mon=0..Sun=6
        if weekday_idx == 6:
            continue  # Sunday — no classes, don't touch
        iso = d.isoformat()
        print(f"-> {iso} ({WEEKDAY_FULL[weekday_idx]})")
        day = build_day(iso, weekday_idx)
        if day is None:
            continue  # fetch failed outright — keep whatever was stored
        existing = store["days"].get(iso)
        if day["status"] == "no_data" and existing and existing.get("status") == "ok":
            print(f"   (keeping previously stored 'ok' data — source not yet updated)")
            continue
        store["days"][iso] = day
        print(f"   {day['status']} — {len(day['entries'])} entries")

    store["updatedAt"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    DATA_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {DATA_FILE} — {len(store['days'])} days total.")


if __name__ == "__main__":
    main()
