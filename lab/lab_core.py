"""
lab_core.py
Shared settings and helpers for the Signal Lab, the research lab that measures which signals really work.
The lab only reads public data and writes reports. It never places orders and never touches the tracker.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

import requests

LAB_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(LAB_DIR)
for _folder in (PROJECT, os.path.join(PROJECT, "tracker")):
    if _folder not in sys.path:
        sys.path.append(_folder)

DATA_DIR = os.environ.get("LAB_DATA_DIR") or os.path.join(PROJECT, "lab_data")
RESULTS_DIR = os.environ.get("LAB_RESULTS_DIR") or os.path.join(LAB_DIR, "results")
ET = ZoneInfo("America/New_York")

USER_AGENT = "Dillen Patel dillenpatel2008@gmail.com"

# ---------------------------------------------------------------- settings

PRICE_YEARS = float(os.environ.get("LAB_PRICE_YEARS", "2"))   # free price data covers about 2 years
HORIZONS = (1, 5, 10, 20, 60)        # trading days after the trade is opened
MAIN_HORIZON = 10                    # about the tracker's 14-calendar-day hold
START_EQUITY = 1_000_000             # the replay of the tracker's rules starts with this much
SHORT_BORROW_PER_YEAR = 0.03         # assumed yearly cost of borrowing shares to short
CLIP_RETURN = 1.0                    # averages count any single move as at most +/-100%
MIN_EVENTS = 30                      # fewer events than this = "too few to judge"
NEW_IDEA_MIN_EVENTS = 100            # a brand-new signal needs at least this many events
NEW_IDEA_MIN_T = 3.0                 # and a t-stat of 3 or more (the bar used in finance research)

SEC_WWW = os.environ.get("LAB_SEC_WWW", "https://www.sec.gov")
SEC_EFTS = os.environ.get("LAB_SEC_EFTS", "https://efts.sec.gov")
PRICE_BASES = [b.strip().rstrip("/") for b in
               os.environ.get("LAB_PRICE_BASES", "https://api.massive.com,https://api.polygon.io").split(",") if b.strip()]
SEC_PER_SECOND = float(os.environ.get("LAB_SEC_PER_SECOND", "8"))    # the SEC allows up to 10
PRICE_CALLS_PER_MINUTE = float(os.environ.get("LAB_PRICE_PER_MINUTE", "5"))   # the free price plan allows 5
TEXT_WORKERS = int(os.environ.get("LAB_TEXT_WORKERS", "4"))
RESERVE_MINUTES = float(os.environ.get("LAB_RESERVE_MINUTES", "30"))   # time kept for the study and the report
PAUSE_SCALE = float(os.environ.get("LAB_PAUSE_SCALE", "1"))             # only tests change this (shorter waits)


# ---------------------------------------------------------------- small helpers

def log(message):
    stamp = datetime.now(ET).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def today_et():
    return datetime.now(ET).date()


def parse_day(text):
    """Reads a date written as 2025-03-31, 20250331, 31-MAR-2025 or 03/31/2025. Returns None if it can't."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%m/%d/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text[:11] if fmt == "%d-%b-%Y" else text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def norm_cik(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits.zfill(10) if digits else ""


def stable_order(text):
    """A fixed pseudo-random number for a string, so a half-finished job still covers a fair sample."""
    return int(hashlib.md5(text.encode()).hexdigest()[:12], 16)


def quarters_between(start, end):
    out = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((y, q))
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def quarter_end(y, q):
    return date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1) - timedelta(days=1)


class Budget:
    """The lab's time limit. Downloads stop early enough to leave time for the study and the report."""

    def __init__(self, minutes):
        self.start = time.time()
        self.end = self.start + minutes * 60

    def left_minutes(self):
        return (self.end - time.time()) / 60

    def used_minutes(self):
        return (time.time() - self.start) / 60

    def downloads_allowed(self):
        return self.left_minutes() > RESERVE_MINUTES


# ---------------------------------------------------------------- files (every save is all-or-nothing)

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _atomic(path, data, mode):
    ensure_dir(os.path.dirname(path) or ".")
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    with open(tmp, mode) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_text(path, text):
    _atomic(path, text.encode("utf-8"), "wb")


def write_json(path, obj):
    write_text(path, json.dumps(obj, indent=1, default=str))


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_csv(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    if path.endswith(".gz"):
        data = gzip.compress(data, compresslevel=6)
    _atomic(path, data, "wb")


def read_csv(path):
    """Returns a list of dicts, or [] if the file is missing or damaged."""
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, EOFError, csv.Error, UnicodeDecodeError, gzip.BadGzipFile):
        return []


# ---------------------------------------------------------------- SEC downloads

class SecClient:
    """Talks to the SEC politely: at most SEC_PER_SECOND requests a second from all threads together,
    waits and tries again when the SEC is busy, and stops for this run if the SEC keeps refusing."""

    def __init__(self, per_second=SEC_PER_SECOND):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        self.gap = 1.0 / max(per_second, 0.1)
        self.lock = threading.Lock()
        self.next_time = 0.0
        self.paused_until = 0.0
        self.refusals = 0
        self.stopped = False
        self.requests = 0
        self.failures = 0

    def _wait_turn(self):
        with self.lock:
            now = time.time()
            slot = max(now, self.next_time, self.paused_until)
            self.next_time = slot + self.gap
        if slot > now:
            time.sleep(slot - now)

    def _refused(self, status):
        with self.lock:
            self.refusals += 1
            pause = (60 if status == 429 else 600) * PAUSE_SCALE
            if self.refusals > 4:
                if not self.stopped:
                    log("  The SEC keeps refusing requests. Stopping SEC downloads for this run (the next run continues).")
                self.stopped = True
            self.paused_until = max(self.paused_until, time.time() + pause)
        if not self.stopped:
            log(f"  The SEC asked us to slow down ({status}). Pausing {pause / 60:.0f} minute(s)...")

    def get(self, url, params=None, stream=False, timeout=60, tries=4):
        """Returns a requests.Response (status 200) or None. Never raises."""
        for attempt in range(1, tries + 1):
            if self.stopped:
                return None
            self._wait_turn()
            try:
                resp = self.session.get(url, params=params, stream=stream, timeout=timeout)
            except requests.RequestException as e:
                if attempt == tries:
                    self.failures += 1
                    log(f"  Could not reach {url} ({type(e).__name__}).")
                    return None
                time.sleep(5 * attempt * PAUSE_SCALE)
                continue
            self.requests += 1
            if resp.status_code == 200:
                with self.lock:
                    self.refusals = 0
                return resp
            status = resp.status_code
            text = ""
            try:
                text = resp.text[:2000].lower()          # error pages are small, even when streaming
            except Exception:
                pass
            resp.close()
            if status == 404:
                return None
            if status == 429 or (status == 403 and ("rate" in text or "automated" in text or "undeclared" in text)):
                self._refused(status)
                continue
            if status >= 500 and attempt < tries:
                time.sleep(10 * attempt * PAUSE_SCALE)
                continue
            self.failures += 1
            log(f"  The SEC answered {status} for {url}.")
            return None
        return None

    def read_capped(self, url, max_bytes, timeout=60):
        """Downloads at most max_bytes of a page. Returns text or None."""
        resp = self.get(url, stream=True, timeout=timeout)
        if resp is None:
            return None
        chunks, size = [], 0
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
        except requests.RequestException:
            if not chunks:
                return None
        finally:
            resp.close()
        return b"".join(chunks).decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- price downloads (Massive, formerly Polygon.io)

class PriceClient:
    """Free plan: 5 calls a minute. Tries api.massive.com first, then the old api.polygon.io address."""

    def __init__(self, api_key, per_minute=PRICE_CALLS_PER_MINUTE):
        self.key = api_key
        self.gap = 60.0 / max(per_minute, 0.1) * 1.05     # a little under the limit, to be safe
        self.next_time = 0.0
        self.base = None
        self.session = requests.Session()
        self.calls = 0

    def _wait_turn(self):
        now = time.time()
        if self.next_time > now:
            time.sleep(self.next_time - now)
        self.next_time = time.time() + self.gap

    def get_json(self, path_or_url, params=None, tries=4):
        """Returns (status, data). status 0 = could not reach any address."""
        params = dict(params or {})
        params["apiKey"] = self.key
        if path_or_url.startswith("http"):
            bases = [""]
        else:
            bases = [self.base] if self.base else PRICE_BASES
        for attempt in range(1, tries + 1):
            last = (0, None)
            for base in bases:
                url = path_or_url if path_or_url.startswith("http") else base + path_or_url
                self._wait_turn()
                try:
                    resp = self.session.get(url, params=params, timeout=60)
                except requests.RequestException:
                    last = (0, None)
                    continue
                self.calls += 1
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if resp.status_code == 404 and not self.base:
                    last = (404, data)
                    continue
                if resp.status_code == 429:
                    last = (429, data)
                    break
                if not path_or_url.startswith("http") and self.base is None and resp.status_code in (200, 401, 403):
                    self.base = base
                return resp.status_code, data
            if last[0] == 429:
                log("  The price service says we are going too fast. Waiting a minute...")
                time.sleep(65 * PAUSE_SCALE)
                continue
            if last[0] == 404:
                return last
            if attempt < tries:
                time.sleep(15 * attempt * PAUSE_SCALE)
                continue
            return last
        return (0, None)
