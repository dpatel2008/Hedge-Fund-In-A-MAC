"""
sec_daily_watcher.py
Finds the most recent EDGAR daily index and saves every filing of the types
we watch to all_filings.csv.

SEC publishes each day's index around 10 PM Eastern, so a morning run reads
the previous business day. If that day's file is not there yet (or it was a
weekend or holiday), the script steps back to the latest day that exists.

  python sec_daily_watcher.py --today
reads the filings made so far TODAY instead (the "Same-day SEC filing check"
workflow, which feeds the tracker's afternoon top-ups). Today's index does not
exist yet, so it asks EDGAR's latest-filings feed and EDGAR full-text search,
and keeps every filing either one finds.
"""

import csv
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
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


# ---------------------------------------------------------------- today's filings (--today)

LATEST_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
# the latest-filings feed matches the start of the form name ("4" also brings 424B5, "NT 10" brings NT 10-Q and NT 10-K)
LATEST_TYPES = ["4", "8-K", "S-1", "S-3", "SC 13D", "SCHEDULE 13D", "NT 10", "10-K", "10-Q"]
LATEST_PAGE = 100
LATEST_MAX_PAGES = 60          # 6,000 filings of one kind is far more than one day has
SEARCH_PAGE = 100
SEARCH_MAX_FROM = 9900         # full-text search stops at 10,000 results
PAUSE_SECONDS = 0.25           # the SEC allows 10 requests a second; this stays well under
ATOM = "{http://www.w3.org/2005/Atom}"
TITLE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{1,10})\) \((?P<role>[^)]*)\)\s*$")
FILED = re.compile(r"Filed:\s*(?:</b>)?\s*(\d{4}-\d{2}-\d{2})")
ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})")
CIK_IN_NAME = re.compile(r"\s*\((?:CIK\s*)?\d{4,10}\)\s*$|\s*\(CIK[^)]*\)")
TICKERS_IN_NAME = re.compile(r"\s*\([A-Z0-9.,\- ]{1,30}\)\s*$")


def _get(url, params, headers, tries=4):
    """One request to the SEC. A busy answer or a network hiccup is tried again a few times."""
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException:
            if attempt == tries:
                raise
            time.sleep(10 * attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < tries:
            time.sleep(10 * attempt)
            continue
        resp.raise_for_status()
        return resp
    return None


def _row(form_type, company, cik, day, accession):
    cik = str(int(cik))
    return {
        "form_type": form_type,
        "company": company.strip(),
        "cik": cik,
        "date_filed": day.strftime("%Y%m%d"),
        "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt",
    }


def parse_latest_feed(text, day):
    """(rows filed on `day`, True if the page also reached filings from before `day`). Skips amendments."""
    rows, older = [], False
    root = ET.fromstring(text)
    for entry in root.iter(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        summary = entry.findtext(f"{ATOM}summary") or ""
        found = TITLE.match(title)
        filed = FILED.search(summary)
        accession = ACCESSION.search((entry.findtext(f"{ATOM}id") or "") + " " + summary)
        if not (found and filed and accession):
            continue
        filed_day = datetime.strptime(filed.group(1), "%Y-%m-%d").date()
        if filed_day < day:
            older = True
            continue
        if filed_day > day:
            continue
        form_type = found.group("form").strip()
        if form_type not in FORM_TYPES:
            continue
        rows.append(_row(form_type, found.group("name"), found.group("cik"), day, accession.group(1)))
    return rows, older


def latest_feed_filings(day):
    """Today's filings from EDGAR's latest-filings feed (newest first, a page of 100 at a time).
    Raises only if not one kind of filing could be read."""
    headers = dict(HEADERS)
    rows, failed = [], []
    for form in LATEST_TYPES:
        got = 0
        try:
            for page in range(LATEST_MAX_PAGES):
                params = {"action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include",
                          "start": page * LATEST_PAGE, "count": LATEST_PAGE, "output": "atom"}
                resp = _get(LATEST_URL, params, headers)
                time.sleep(PAUSE_SECONDS)
                if resp is None:
                    break
                found, older = parse_latest_feed(resp.text, day)
                rows += found
                got += len(found)
                if older or resp.text.count("<entry>") < LATEST_PAGE:
                    break
        except Exception as e:
            failed.append(form)
            print(f"  latest-filings feed, {form}: stopped ({type(e).__name__}: {e})")
        print(f"  latest-filings feed, {form}: {got} row(s)")
    if len(failed) == len(LATEST_TYPES):
        raise RuntimeError("not one kind of filing could be read")
    return rows


def parse_search_hits(data, day):
    """Rows from one page of EDGAR full-text search results: one row for each company named on the filing."""
    rows = []
    for hit in (data.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        form_type = (src.get("form") or src.get("file_type") or "").strip()
        if form_type not in FORM_TYPES:
            continue
        if (src.get("file_date") or "") != day.isoformat():
            continue
        accession = src.get("adsh") or str(hit.get("_id") or "").split(":")[0]
        if not ACCESSION.fullmatch(accession or ""):
            continue
        names = src.get("display_names") or []
        for i, cik in enumerate(src.get("ciks") or []):
            if not str(cik).strip().isdigit():
                continue
            name = names[i] if i < len(names) else ""
            name = TICKERS_IN_NAME.sub("", CIK_IN_NAME.sub("", name)).strip()
            rows.append(_row(form_type, name, cik, day, accession))
    return rows


def search_filings(day):
    """Today's filings from EDGAR full-text search (asked one kind of filing at a time).
    Raises only if not one kind of filing could be read."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    rows, failed = [], []
    for form in FORM_TYPES:
        got = 0
        start = 0
        try:
            while start <= SEARCH_MAX_FROM:
                params = {"forms": form, "dateRange": "custom", "startdt": day.isoformat(),
                          "enddt": day.isoformat(), "from": start}
                resp = _get(SEARCH_URL, params, headers)
                time.sleep(PAUSE_SECONDS)
                if resp is None:
                    break
                data = resp.json()
                hits = (data.get("hits") or {}).get("hits") or []
                found = parse_search_hits(data, day)
                rows += found
                got += len(found)
                if len(hits) < SEARCH_PAGE:
                    break
                start += SEARCH_PAGE
        except Exception as e:
            failed.append(form)
            print(f"  full-text search, {form}: stopped ({type(e).__name__}: {e})")
        print(f"  full-text search, {form}: {got} row(s)")
    if len(failed) == len(FORM_TYPES):
        raise RuntimeError("not one kind of filing could be read")
    return rows


SOURCE_COUNTS = {}             # rows each EDGAR source gave in the last todays_filings() call


def todays_filings(day=None):
    """Every watched filing made so far today. Uses both EDGAR sources and keeps one row per filing and company.
    Raises only if both sources fail."""
    day = day or datetime.now(ZoneInfo("America/New_York")).date()
    found, problems = [], []
    SOURCE_COUNTS.clear()
    for name, source in (("latest-filings feed", latest_feed_filings), ("full-text search", search_filings)):
        try:
            rows = source(day)
            SOURCE_COUNTS[name] = len(rows)
            found += rows
        except Exception as e:
            SOURCE_COUNTS[name] = f"failed ({type(e).__name__})"
            problems.append(f"{name}: {type(e).__name__}: {e}")
            print(f"  The {name} did not work ({type(e).__name__}: {e}). Using the other source.")
    if len(problems) == 2:
        raise RuntimeError("both EDGAR sources failed: " + " | ".join(problems))
    rows, seen, one_url = [], set(), {}
    for row in found:
        filing = row["url"].rsplit("/", 1)[-1]
        row["url"] = one_url.setdefault(filing, row["url"])   # one address per filing (the Form 4 check opens each once)
        if (filing, row["cik"]) not in seen:
            seen.add((filing, row["cik"]))
            rows.append(row)
    return day, rows


def write_csv(filings):
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["form_type", "company", "cik", "date_filed", "url"])
        writer.writeheader()
        for filing in filings:
            writer.writerow(filing)


def main():
    if "--today" in sys.argv:
        d, filings = todays_filings()
        print(f"Found {len(filings)} filing(s) made so far on {d.isoformat()}:")
        counts = {}
        for f in filings:
            counts[f["form_type"]] = counts.get(f["form_type"], 0) + 1
        for form_type, count in sorted(counts.items()):
            print(f"  {form_type}: {count}")
        write_csv(filings)
        print(f"Wrote {OUTPUT_CSV}")
        if os.environ.get("GITHUB_ACTIONS"):          # a note on the workflow run, readable without the full log
            sources = ", ".join(f"{k}: {v}" for k, v in SOURCE_COUNTS.items())
            kinds = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            print(f"::notice title=Same-day filings::{len(filings)} filing rows ({kinds}). Sources: {sources}")
        return

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
