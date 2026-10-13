"""
lab_sec.py
Everything the lab reads from the SEC (all free):
  * the quarterly list of every filing of every form type
  * the SEC's insider-trading data sets (every Form 4, already turned into tables by the SEC)
  * full-text search, to find 10-Ks and 10-Qs that say there is substantial doubt the company can keep going
  * the text of 10-Ks, 10-Qs and 8-Ks, read with the exact same rules as the daily scanner (a little more each run)
  * the SEC's list of tickers
Saved files are reused, so each run only downloads what is new.
"""

import csv
import gzip
import io
import os
import re
import threading
import zipfile
from datetime import date, timedelta

from lab_core import (DATA_DIR, SEC_EFTS, SEC_WWW, TEXT_WORKERS, log, norm_cik, parse_day, quarter_end,
                      quarters_between, read_csv, read_json, stable_order, today_et, write_csv, write_json)

SEC_DIR = os.path.join(DATA_DIR, "sec")
INDEX_DIR = os.path.join(SEC_DIR, "index")
INSIDER_DIR = os.path.join(SEC_DIR, "insider")
GC_DIR = os.path.join(SEC_DIR, "going_concern")
SCAN_DIR = os.path.join(SEC_DIR, "textscan")
TICKERS_FILE = os.path.join(SEC_DIR, "company_tickers.json")

INDEX_FIELDS = ["form", "cik", "date", "accession", "company"]
INSIDER_FIELDS = ["accession", "filing_date", "issuer_cik", "symbol", "owners", "n_owners", "is_exec", "titles",
                  "relationship", "planned", "saw_sale", "sold_shares", "sold_dollars", "saw_buy", "bought_shares",
                  "bought_dollars", "owned_after_buy"]
GC_FIELDS = ["accession", "cik", "date", "form", "sic", "company"]

# Phrases that appear when a company or its auditor actually HAS substantial doubt (the general "we looked for
# substantial doubt" wording is left out on purpose). Each candidate is then read and checked with the scanner's rule.
GC_PHRASES = [
    "these conditions raise substantial doubt",
    "these factors raise substantial doubt",
    "these matters raise substantial doubt",
    "these circumstances raise substantial doubt",
    "these uncertainties raise substantial doubt",
    "which raises substantial doubt",
    "which raise substantial doubt",
    "that raise substantial doubt about its ability to continue as a going concern",
    "raises substantial doubt about its ability to continue as a going concern",
    "there is substantial doubt about",
    "substantial doubt exists about",
    "has substantial doubt about",
]
GC_TEXT_BYTES = 3_000_000        # the same as the daily scanner
EIGHTK_TEXT_BYTES = 600_000      # the same as the daily scanner


# ---------------------------------------------------------------- the quarterly list of every filing

TAIL = re.compile(r"(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/\S+)\s*$")


def parse_form_index(text):
    """Yields (form, cik, date, accession, company) for every filing in an EDGAR form.idx file."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines[:200]):
        if line.startswith("----"):
            start = i + 1
            break
    if start is None:
        return
    for line in lines[start:]:
        if not line.strip():
            continue
        tail = TAIL.search(line)
        if not tail:
            continue
        when = parse_day(tail.group(1))
        path = tail.group(2)
        k = 12                                            # the form type column is 12 characters wide
        if line[12:14] == "/A":                           # a form name longer than its column, e.g. SCHEDULE 13D/A
            k = 14
        form = line[:k].strip()
        head = line[k:tail.start()]
        parts = head.split()
        if not form or not parts or not parts[-1].isdigit() or when is None:
            continue
        cik = parts[-1]
        company = head[:head.rstrip().rfind(cik)].strip()
        name = os.path.basename(path)
        accession = name[:-4] if name.endswith(".txt") else name
        yield form, norm_cik(cik), when, accession, company


def index_path(y, q):
    return os.path.join(INDEX_DIR, f"{y}Q{q}.csv.gz")


def update_indexes(sec, start, end, budget, status):
    info = status.setdefault("sec_lists", {"quarters": 0, "problems": []})
    today = today_et()
    for y, q in quarters_between(start, end):
        path = index_path(y, q)
        meta = read_json(path + ".json", {}) or {}
        if os.path.exists(path) and (meta.get("complete") or meta.get("downloaded") == today.isoformat()):
            info["quarters"] += 1
            continue
        if not budget.downloads_allowed() or sec.stopped:
            break
        log(f"SEC: downloading the list of every filing for {y} Q{q}...")
        resp = sec.get(f"{SEC_WWW}/Archives/edgar/full-index/{y}/QTR{q}/form.idx", timeout=600)
        if resp is None:
            info["problems"].append(f"Could not download the filing list for {y} Q{q}.")
            continue
        text = resp.content.decode("latin-1")
        rows = [[f, c, d.isoformat(), a, n] for f, c, d, a, n in parse_form_index(text)]
        if not rows:
            info["problems"].append(f"The filing list for {y} Q{q} was empty or unreadable.")
            continue
        write_csv(path, INDEX_FIELDS, rows)
        write_json(path + ".json", {"complete": today > quarter_end(y, q) + timedelta(days=7),
                                    "downloaded": today.isoformat(), "rows": len(rows)})
        info["quarters"] += 1
        log(f"  {len(rows):,} filings listed for {y} Q{q}.")


def read_index(start, end, keep=None):
    """Yields (form, cik, date, accession, company) for saved filings between start and end (dates).
    keep(form) can skip form types early to save memory."""
    for y, q in quarters_between(start, end):
        path = index_path(y, q)
        if not os.path.exists(path):
            continue
        try:
            with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                if next(reader, None) != INDEX_FIELDS:
                    raise EOFError("unreadable header")
                s, e = start.isoformat(), end.isoformat()
                for row in reader:
                    if len(row) != 5 or not (s <= row[2] <= e):
                        continue
                    if keep is not None and not keep(row[0]):
                        continue
                    yield row[0], row[1], row[2], row[3], row[4]
        except (OSError, EOFError, csv.Error, UnicodeDecodeError):
            log(f"  The saved filing list for {y} Q{q} is damaged; it will be downloaded again.")
            for p in (path, path + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------- insider trading (Form 4)

def list_insider_sets(sec):
    resp = sec.get(f"{SEC_WWW}/dera/data/form-345", timeout=60)
    if resp is None:
        return {}
    links = {}
    for href, y, q in re.findall(r'href="([^"]*?(\d{4})q([1-4])_form345\.zip)"', resp.text, re.IGNORECASE):
        if href.startswith("/"):
            href = SEC_WWW + href
        elif not href.startswith("http"):
            href = SEC_WWW + "/" + href
        links[(int(y), int(q))] = href
    return links


def _tsv(z, prefix):
    for name in z.namelist():
        base = os.path.basename(name).upper()
        if base.startswith(prefix) and base.endswith((".TSV", ".TXT")):
            f = io.TextIOWrapper(z.open(name), encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
            header = [h.strip().strip("﻿").strip('"').upper() for h in next(reader, [])]
            for row in reader:
                if len(row) == len(header):
                    yield dict(zip(header, row))
            return
    raise ValueError(f"{prefix} table not found in the insider data file")


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_insider_zip(content):
    """Turns one quarter of the SEC's insider data into one row per Form 4, with the same numbers the daily
    scanner works out from each Form 4 (who sold, who bought on the open market, and for how much)."""
    import form4_cluster_check as live
    csv.field_size_limit(10_000_000)
    z = zipfile.ZipFile(io.BytesIO(content))
    subs = {}
    for r in _tsv(z, "SUBMISSION"):
        if (r.get("DOCUMENT_TYPE") or "").strip() != "4":
            continue
        planned = (r.get("AFF10B5ONE") or "").strip().upper()
        subs[r["ACCESSION_NUMBER"]] = {
            "accession": r["ACCESSION_NUMBER"],
            "filing_date": parse_day(r.get("FILING_DATE")),
            "issuer_cik": norm_cik(r.get("ISSUERCIK")),
            "symbol": (r.get("ISSUERTRADINGSYMBOL") or "").strip().upper(),
            "planned": "1" if planned in ("1", "Y", "YES", "TRUE", "X") else ("0" if planned in ("0", "N", "NO", "FALSE") else ""),
            "owners": [], "titles": [], "rel": [],
            "saw_sale": False, "sold_shares": 0.0, "sold_dollars": 0.0,
            "saw_buy": False, "bought_shares": 0.0, "bought_dollars": 0.0, "owned_after_buy": 0.0,
        }
    for r in _tsv(z, "REPORTINGOWNER"):
        s = subs.get(r.get("ACCESSION_NUMBER"))
        if s is None:
            continue
        s["owners"].append((r.get("RPTOWNERNAME") or "").strip())
        s["titles"].append((r.get("RPTOWNER_TITLE") or "").strip())
        s["rel"].append((r.get("RPTOWNER_RELATIONSHIP") or "").strip())
    for r in _tsv(z, "NONDERIV_TRANS"):
        s = subs.get(r.get("ACCESSION_NUMBER"))
        if s is None:
            continue
        code = (r.get("TRANS_CODE") or "").strip().upper()
        shares = _f(r.get("TRANS_SHARES"))
        price = _f(r.get("TRANS_PRICEPERSHARE"))
        if code == "S":
            s["saw_sale"] = True
            s["sold_shares"] += shares
            s["sold_dollars"] += shares * price
        elif code == "P":
            s["saw_buy"] = True
            s["bought_shares"] += shares
            s["bought_dollars"] += shares * price
            s["owned_after_buy"] = max(s["owned_after_buy"], _f(r.get("SHRS_OWND_FOLWNG_TRANS")))
    rows = []
    for s in subs.values():
        if s["filing_date"] is None or not s["issuer_cik"]:
            continue
        owners = [o for o in s["owners"] if o]
        is_exec = any(t and live.EXEC_TITLE.search(t) for t in s["titles"])
        rows.append([s["accession"], s["filing_date"].isoformat(), s["issuer_cik"], s["symbol"],
                     " & ".join(owners) if owners else "Unknown", len(owners), int(is_exec),
                     " | ".join(t for t in s["titles"] if t), " | ".join(x for x in s["rel"] if x), s["planned"],
                     int(s["saw_sale"]), round(s["sold_shares"], 2), round(s["sold_dollars"], 2),
                     int(s["saw_buy"]), round(s["bought_shares"], 2), round(s["bought_dollars"], 2),
                     round(s["owned_after_buy"], 2)])
    return rows


def update_insider(sec, start, end, budget, status):
    info = status.setdefault("insider", {"quarters": 0, "problems": []})
    needed = [(y, q) for y, q in quarters_between(start, end) if not os.path.exists(os.path.join(INSIDER_DIR, f"{y}Q{q}.csv.gz"))]
    info["quarters"] = len(quarters_between(start, end)) - len(needed)
    if not needed:
        return
    links = list_insider_sets(sec)
    if not links:
        info["problems"].append("Could not read the SEC's list of insider data files.")
        return
    for y, q in needed:
        url = links.get((y, q))
        if not url:
            continue                       # that quarter is not published yet
        if not budget.downloads_allowed() or sec.stopped:
            break
        log(f"SEC: downloading insider trading data for {y} Q{q}...")
        resp = sec.get(url, timeout=600)
        if resp is None:
            info["problems"].append(f"Could not download insider data for {y} Q{q}.")
            continue
        try:
            rows = parse_insider_zip(resp.content)
        except (zipfile.BadZipFile, ValueError, KeyError) as e:
            info["problems"].append(f"Could not read insider data for {y} Q{q} ({e}).")
            continue
        write_csv(os.path.join(INSIDER_DIR, f"{y}Q{q}.csv.gz"), INSIDER_FIELDS, rows)
        info["quarters"] += 1
        log(f"  {len(rows):,} Form 4 filings for {y} Q{q}.")


def read_insider(start, end):
    """Yields the saved Form 4 rows filed between start and end (one quarter file at a time, to save memory)."""
    s, e = start.isoformat(), end.isoformat()
    for y, q in quarters_between(start, end):
        path = os.path.join(INSIDER_DIR, f"{y}Q{q}.csv.gz")
        if not os.path.exists(path):
            continue
        try:
            with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames != INSIDER_FIELDS:
                    raise EOFError("unreadable header")
                for r in reader:
                    if s <= r.get("filing_date", "") <= e:
                        yield r
        except (OSError, EOFError, csv.Error, UnicodeDecodeError):
            log(f"  The saved insider data for {y} Q{q} is damaged; it will be downloaded again.")
            try:
                os.remove(path)
            except OSError:
                pass


def insider_quarters_covered(start, end):
    return [(y, q) for y, q in quarters_between(start, end) if os.path.exists(os.path.join(INSIDER_DIR, f"{y}Q{q}.csv.gz"))]


# ---------------------------------------------------------------- tickers

def update_tickers(sec, status):
    resp = sec.get(f"{SEC_WWW}/files/company_tickers.json", timeout=120)
    if resp is None:
        status.setdefault("problems", []).append("Could not download the SEC ticker list; the saved one is used.")
        return
    try:
        data = resp.json()
    except ValueError:
        return
    write_json(TICKERS_FILE, data)


SYMBOL_OK = re.compile(r"^[A-Z]{1,6}(?:[.\-][A-Z]{1,2})?$")


def clean_symbol(raw):
    s = (raw or "").strip().upper()
    for prefix in ("NYSE:", "NASDAQ:", "NYSEAMERICAN:", "AMEX:", "OTC:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    s = re.split(r"[\s,;]+", s)[0].strip(".") if s else ""
    s = s.replace("/", ".")
    if s in ("NONE", "N/A", "NA", "NULL") or not SYMBOL_OK.match(s):
        return ""
    return s


def symbol_variants(s):
    out = [s]
    for v in (s.replace("-", "."), s.replace(".", "-"), s.replace(".", "").replace("-", "")):
        if v and v not in out:
            out.append(v)
    return out


class TickerMap:
    """Which ticker a company (CIK) traded under on a given day. Uses the tickers insiders wrote on their Form 4s
    around that day first (so companies that later changed ticker or were delisted still match), then the SEC's
    current list. A ticker written on only a few filings (a typo) loses to the one written on most of them."""

    def __init__(self, insider_rows):
        seen = {}
        for r in insider_rows:
            sym = clean_symbol(r.get("symbol"))
            d = parse_day(r.get("filing_date"))
            cik = r.get("issuer_cik")
            if sym and d and cik:
                seen.setdefault(cik, []).append((d.toordinal(), sym))
        self.runs = {}
        for cik, pairs in seen.items():
            pairs.sort()
            runs = []
            for o, sym in pairs:
                if runs and runs[-1][2] == sym:
                    runs[-1][1] = o
                    runs[-1][3] += 1
                else:
                    runs.append([o, o, sym, 1])
            self.runs[cik] = runs
        self.current = {}
        data = read_json(TICKERS_FILE, {}) or {}
        entries = data.values() if isinstance(data, dict) else data
        for e in entries:
            if not isinstance(e, dict):
                continue
            cik, sym = norm_cik(e.get("cik_str")), clean_symbol(e.get("ticker"))
            if cik and sym:
                self.current.setdefault(cik, []).append(sym)
        for cik in self.current:
            self.current[cik].sort(key=len)          # the shortest ticker is usually the common stock

    def known(self, cik):
        return cik in self.runs or cik in self.current

    def candidates(self, cik, d):
        o = d.toordinal()
        best = {}
        for first, last, sym, count in self.runs.get(cik, ()):
            distance = 0 if first <= o <= last else min(abs(o - first), abs(o - last))
            key = (0 if distance == 0 else (1 if distance <= 400 else 2), -count, distance)
            if sym not in best or key < best[sym]:
                best[sym] = key
        out = [sym for sym, _ in sorted(best.items(), key=lambda kv: kv[1])]
        for sym in self.current.get(cik, []):
            if sym not in out:
                out.append(sym)
        return out


# ---------------------------------------------------------------- going concern (full-text search)

def _months(start, end):
    """(first day, last day searched, real last day of the month) for each month from start to end."""
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
        month_end = nxt - timedelta(days=1)
        out.append((d, min(month_end, end), month_end))
        d = nxt
    return out


def _efts(sec, phrase, d0, d1, found, depth=0):
    """Collects every 10-K / 10-Q with the phrase filed between d0 and d1. Returns False if the SEC did not answer."""
    params = {"q": f'"{phrase}"', "forms": "10-K,10-Q", "dateRange": "custom",
              "startdt": d0.isoformat(), "enddt": d1.isoformat()}
    offset = 0
    while True:
        params["from"] = offset
        resp = sec.get(f"{SEC_EFTS}/LATEST/search-index", params=params, timeout=90)
        if resp is None:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        if not isinstance(data, dict) or not isinstance(data.get("hits"), dict):
            return False                               # an error answer: the month is searched again next run
        hits = data["hits"]
        total = (hits.get("total") or {}).get("value", 0) or 0
        if offset == 0 and total >= 9000 and d1 > d0 and depth < 8:
            mid = d0 + timedelta(days=(d1 - d0).days // 2)
            return (_efts(sec, phrase, d0, mid, found, depth + 1) and
                    _efts(sec, phrase, mid + timedelta(days=1), d1, found, depth + 1))
        page = hits.get("hits") or []
        for h in page:
            src = h.get("_source") or {}
            form = (src.get("form") or "").strip()
            if form not in ("10-K", "10-Q"):
                continue
            adsh = src.get("adsh") or (h.get("_id") or "").split(":")[0]
            ciks = src.get("ciks") or []
            when = parse_day(src.get("file_date"))
            if not adsh or not ciks or when is None:
                continue
            sics = src.get("sics") or [""]
            names = src.get("display_names") or [""]
            found[adsh] = [adsh, norm_cik(ciks[0]), when.isoformat(), form, str(sics[0] or ""), str(names[0] or "")]
        offset += len(page)
        if not page or offset >= total or offset >= 9900:
            return True


def update_going_concern(sec, start, end, budget, status):
    info = status.setdefault("going_concern", {"problems": []})
    store = os.path.join(GC_DIR, "candidates.csv.gz")
    months_file = os.path.join(GC_DIR, "months_done.json")
    done = read_json(months_file, {}) or {}
    rows = {r["accession"]: [r.get(k, "") for k in GC_FIELDS] for r in read_csv(store) if r.get("accession")}
    if not rows:
        done = {}               # nothing saved (or the saved list was damaged): search every month again
    today = today_et()
    failed = []
    for m0, m1, month_end in _months(start, end):
        key = m0.strftime("%Y-%m")
        if done.get(key):
            continue
        if not budget.downloads_allowed() or sec.stopped:
            break
        found = {}
        ok = all(_efts(sec, phrase, m0, m1, found) for phrase in GC_PHRASES)
        if not ok:
            failed.append(key)
            continue
        rows.update(found)
        if m1 == month_end and today > month_end + timedelta(days=10):
            done[key] = True                           # a finished month never needs searching again
        write_csv(store, GC_FIELDS, list(rows.values()))
        write_json(months_file, done)
        log(f"SEC: {len(found)} going-concern candidates in {key}.")
    if failed:
        info["problems"].append(f"Full-text search did not answer for {len(failed)} month(s) ({failed[0]} to "
                                f"{failed[-1]}); the next run tries those again.")
    info["candidates"] = len(rows)


def read_going_concern():
    return read_csv(os.path.join(GC_DIR, "candidates.csv.gz"))


# ---------------------------------------------------------------- reading filings with the daily scanner's rules

def filing_url(cik, accession):
    return f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{accession}.txt"


def read_going_concern_flag(sec, url):
    """Reads a 10-K or 10-Q the way going_concern_check.py does (up to 3 MB, stopping as soon as real going-concern
    language is found). Returns 1, 0, or None if the filing could not be downloaded."""
    import going_concern_check as live
    import requests
    resp = sec.get(url, stream=True, timeout=60)
    if resp is None:
        return None
    tail, size = "", 0
    try:
        for chunk in resp.iter_content(chunk_size=262144):
            if not chunk:
                continue
            size += len(chunk)
            combined = tail + chunk.decode("utf-8", errors="ignore")
            if live.find_going_concern(live.clean(combined)):
                return 1
            tail = combined[-live.OVERLAP_CHARS:]
            if size >= GC_TEXT_BYTES:
                break
    except requests.RequestException:
        return None
    finally:
        resp.close()
    return 0


def check_8k(text):
    import positive_8k_check as live
    cleaned = live.clean(text)
    return ";".join(live.find_good_news(cleaned).keys()), ";".join(live.find_bad_news(cleaned).keys())


SCAN_FIELDS = {"gc": ["accession", "flag"], "8k": ["accession", "good", "bad"]}


def read_scans(kind):
    """{accession: row} of filings already read."""
    path = os.path.join(SCAN_DIR, f"{kind}.csv")
    out = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if header and len(row) == len(header) and row[0]:
                    out[row[0]] = dict(zip(header, row))
    except (OSError, csv.Error):
        pass
    return out


def scan_filings(sec, budget, kind, tasks, limit=None):
    """Reads filings (tasks: list of (accession, cik)) with several workers and saves what the scanner's rules
    find. Stops when the time budget says so; the rest is done next run. Returns how many were read."""
    path = os.path.join(SCAN_DIR, f"{kind}.csv")
    os.makedirs(SCAN_DIR, exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    if not new_file:
        with open(path, "rb") as f:                     # finish a line cut off by a crash, so the next row is clean
            f.seek(-1, os.SEEK_END)
            ends_cleanly = f.read(1) == b"\n"
        if not ends_cleanly:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
    out = open(path, "a", newline="", encoding="utf-8")
    writer = csv.writer(out)
    if new_file:
        writer.writerow(SCAN_FIELDS[kind])
    lock = threading.Lock()
    todo = iter(tasks if limit is None else tasks[:limit])
    counts = {"read": 0, "failed": 0}

    def worker():
        while budget.downloads_allowed() and not sec.stopped:
            with lock:
                task = next(todo, None)
            if task is None:
                return
            accession, cik = task
            try:
                if kind == "gc":
                    flag = read_going_concern_flag(sec, filing_url(cik, accession))
                    result = None if flag is None else [flag]
                else:
                    text = sec.read_capped(filing_url(cik, accession), EIGHTK_TEXT_BYTES)
                    result = None if text is None else list(check_8k(text))
            except Exception as e:                       # one odd filing must not stop the rest
                log(f"  Could not check {accession}: {type(e).__name__}")
                continue
            if result is None:
                with lock:
                    counts["failed"] += 1
                continue
            with lock:
                writer.writerow([accession] + result)
                counts["read"] += 1
                if counts["read"] % 200 == 0:
                    out.flush()
                if counts["read"] % 2000 == 0:
                    log(f"  Read {counts['read']:,} {'10-K/10-Q' if kind == 'gc' else '8-K'} filings so far this run.")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, TEXT_WORKERS))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out.flush()
    os.fsync(out.fileno())
    out.close()
    return counts


def ordered(tasks):
    return sorted(tasks, key=lambda t: stable_order(t[0]))
