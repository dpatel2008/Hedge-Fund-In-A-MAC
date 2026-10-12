"""
sec_daily_watcher.py
Finds the most recent EDGAR daily index and saves every filing of the types
we watch to all_filings.csv.

SEC publishes each day's index around 10 PM Eastern, so a morning run reads
the previous business day. If that day's file is not there yet (or it was a
weekend or holiday), the script steps back to the latest day that exists.
"""

import csv
import re
import sys
import time
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

USER_AGENT = "Dillen Patel dillenpatel2008@gmail.com"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

FORM_TYPES = [
    "S-1", "S-3", "424B5", "4", "8-K",
    "SC 13D", "SCHEDULE 13D",
    "NT 10-Q", "NT 10-K", "10-K", "10-Q",
]

OUTPUT_CSV = "all_filings.csv"
MAX_DAYS_BACK = 7


def index_url(d: date) -> str:
    quarter = (d.month - 1) // 3 + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/"
        f"{d.year}/QTR{quarter}/form.{d.strftime('%Y%m%d')}.idx"
    )


def download_index(d: date, tries: int = 4):
    """Returns the index text, or None if SEC does not have that day's file.
    A network hiccup or an SEC "busy" answer is tried again a few times before giving up."""
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(index_url(d), headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            if attempt == tries:
                raise
            print(f"  Network problem reaching the SEC ({e}). Trying again in {15 * attempt} seconds...")
            time.sleep(15 * attempt)
            continue
        if resp.status_code in (403, 404):
            return None
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < tries:
            print(f"  The SEC answered {resp.status_code} (busy). Trying again in {15 * attempt} seconds...")
            time.sleep(15 * attempt)
            continue
        resp.raise_for_status()
        return resp.text
    return None


def find_latest_index():
    today = datetime.now(ZoneInfo("America/New_York")).date()
    for days_back in range(MAX_DAYS_BACK + 1):
        d = today - timedelta(days=days_back)
        if d.weekday() >= 5:
            continue
        text = download_index(d)
        if text:
            print(f"Using the EDGAR daily index for {d.isoformat()}.")
            return d, text
        print(f"  No index for {d.isoformat()} yet, trying the day before...")
    return None, None


TAIL = re.compile(r"(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/\S+)\s*$")


def parse_index(text: str):
    """Reads each line from the right side (file name, then date, then CIK), so it still
    works when the SEC's columns are not exactly where we expect."""
    lines = text.splitlines()

    start_idx = None
    for i, line in enumerate(lines):
        if line.startswith("----"):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    filings = []
    for line in lines[start_idx:]:
        if not line.strip():
            continue
        form_type = line[0:12].strip()
        if form_type not in FORM_TYPES:
            continue
        tail = TAIL.search(line)
        if not tail:
            continue
        date_filed, file_name = tail.group(1), tail.group(2)
        head = line[12:tail.start()]
        if head.startswith("/A"):
            continue  # an amendment such as SCHEDULE 13D/A, not a new filing
        parts = head.split()
        if not parts or not parts[-1].isdigit():
            continue
        cik = parts[-1]
        company_name = head[:head.rstrip().rfind(cik)].strip()
        filings.append({
            "form_type": form_type,
            "company": company_name,
            "cik": cik,
            "date_filed": date_filed,
            "url": f"https://www.sec.gov/Archives/{file_name}",
        })
    return filings


def write_csv(filings):
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["form_type", "company", "cik", "date_filed", "url"])
        writer.writeheader()
        for filing in filings:
            writer.writerow(filing)


def main():
    d, text = find_latest_index()
    if text is None:
        print("ERROR: no EDGAR daily index found in the last week.")
        print("If this keeps happening, check that USER_AGENT has your real name and email.")
        sys.exit(1)

    filings = parse_index(text)
    print(f"Found {len(filings)} filing(s) across all watched types:")
    counts = {}
    for f in filings:
        counts[f["form_type"]] = counts.get(f["form_type"], 0) + 1
    for form_type, count in sorted(counts.items()):
        print(f"  {form_type}: {count}")

    write_csv(filings)
    print(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
