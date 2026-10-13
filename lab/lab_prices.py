"""
lab_prices.py
Daily prices for every US stock. One free "grouped daily" call returns every stock's open, high, low, close and
volume for one day, including companies that later went bankrupt or were delisted (so the tests are honest).
Saved days are kept, so later runs only download what is new. Prices are saved exactly as they traded and are
adjusted for stock splits when loaded.
"""

import csv
import glob
import gzip
import os
from bisect import bisect_right
from datetime import datetime, timedelta

import numpy as np

from lab_core import (DATA_DIR, ET, PRICE_YEARS, PriceClient, log, parse_day, read_csv, read_json, today_et,
                      write_csv, write_json, write_text)

PRICE_DIR = os.path.join(DATA_DIR, "prices")
DAY_DIR = os.path.join(PRICE_DIR, "daily")
SPLITS_FILE = os.path.join(PRICE_DIR, "splits.csv")
TYPES_FILE = os.path.join(PRICE_DIR, "ticker_types.csv")       # which symbols are common stocks, funds, ...
TYPES_MAX_AGE_DAYS = 7
STATE_FILE = os.path.join(PRICE_DIR, "state.json")
BENCHMARKS = ("SPY", "IWM")


def day_path(d):
    return os.path.join(DAY_DIR, f"{d.isoformat()}.csv.gz")


def closed_path(d):
    return os.path.join(DAY_DIR, f"{d.isoformat()}.closed")


def saved_days():
    out = []
    for path in glob.glob(os.path.join(DAY_DIR, "*.csv.gz")):
        d = parse_day(os.path.basename(path)[:10])
        if d:
            out.append(d)
    return sorted(out)


def wanted_days(today, years, floor=None):
    start = today - timedelta(days=int(years * 365.25))
    if floor:
        start = max(start, floor + timedelta(days=1))
    last = today if datetime.now(ET).hour >= 20 else today - timedelta(days=1)
    days, d = [], start
    while d <= last:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def download_prices(api_key, budget, status):
    """Downloads the missing days, newest first, until done or the time budget says stop."""
    info = status.setdefault("prices", {})
    if not api_key:
        info["problem"] = "No POLYGON_API_KEY secret was found, so no new prices were downloaded."
        log("Prices: " + info["problem"])
        return
    client = PriceClient(api_key)
    state = read_json(STATE_FILE, {}) or {}
    floor = parse_day(state.get("floor", ""))
    today = today_et()
    todo = [d for d in reversed(wanted_days(today, PRICE_YEARS, floor))
            if not os.path.exists(day_path(d)) and not os.path.exists(closed_path(d))]
    log(f"Prices: {len(todo)} day(s) to download (about {len(todo) * client.gap / 60:.0f} minutes at the free speed).")
    got, refused_in_a_row = 0, 0
    for d in todo:
        if not budget.downloads_allowed():
            info["note"] = "Stopped early to leave time for the report. The next run continues."
            break
        code, data = client.get_json(f"/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}",
                                     {"adjusted": "false", "include_otc": "false"})
        if code == 200 and isinstance(data, dict):
            rows = []
            for r in data.get("results") or []:
                t, c = r.get("T"), r.get("c")
                if not t or c is None:
                    continue
                rows.append([t, r.get("o"), r.get("h"), r.get("l"), c, r.get("v")])
            refused_in_a_row = 0
            if rows:
                write_csv(day_path(d), ["T", "o", "h", "l", "c", "v"], rows)
                got += 1
                if got % 25 == 0:
                    log(f"  Prices: {got} of {len(todo)} days saved (now at {d.isoformat()}).")
            elif (today - d).days >= 4:
                write_text(closed_path(d), "")       # a market holiday
            continue
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or "")
        if code in (401, 403):
            if code == 401 or "api key" in message.lower() or "apikey" in message.lower():
                info["problem"] = f"The price service did not accept the API key ({message[:120]})."
                log("Prices: " + info["problem"])
                return
            if (today - d).days > 600:
                state["floor"] = d.isoformat()
                write_json(STATE_FILE, state)
                log(f"  Prices from {d.isoformat()} and earlier are not in the free plan. Stopping there.")
                break
            if (today - d).days <= 5:
                log(f"  Prices for {d.isoformat()} are not ready yet; the next run gets them.")
                continue
            refused_in_a_row += 1
            log(f"  The price service refused {d.isoformat()} ({code}: {message[:120]}).")
            if refused_in_a_row >= 3:
                info["problem"] = f"The price service refused several days in a row ({code}: {message[:120]})."
                break
            continue
        if code == 0:
            info["problem"] = "Could not reach the price service. The next run tries again."
            log("  " + info["problem"])
            break
        log(f"  The price service answered {code} for {d.isoformat()} ({message[:80]}). Skipping that day for now.")
    info["downloaded_days"] = got
    refresh_splits(client, status)
    refresh_ticker_types(client, status)
    info["calls"] = client.calls


def refresh_splits(client, status):
    """Every stock split in the period, so prices before a split can be put on the same footing as after it."""
    info = status.setdefault("prices", {})
    days = saved_days()
    since = (days[0] if days else today_et() - timedelta(days=int(PRICE_YEARS * 365.25))) - timedelta(days=5)
    attempts = [
        ("/stocks/v1/splits", {"execution_date.gte": since.isoformat(), "limit": 5000, "sort": "execution_date.asc"}),
        ("/v3/reference/splits", {"execution_date.gte": since.isoformat(), "limit": 1000, "order": "asc",
                                  "sort": "execution_date"}),
    ]
    for path, params in attempts:
        rows, url, first, pages = [], path, True, 0
        ok = True
        while url and pages < 60:
            code, data = client.get_json(url, params if first else None)
            first = False
            pages += 1
            if code != 200 or not isinstance(data, dict):
                ok = False
                break
            for r in data.get("results") or []:
                try:
                    f, t = float(r.get("split_from") or 0), float(r.get("split_to") or 0)
                except (TypeError, ValueError):
                    continue
                if r.get("ticker") and r.get("execution_date") and f > 0 and t > 0:
                    rows.append([r["ticker"], str(r["execution_date"])[:10], f, t])
            url = data.get("next_url")
        if ok:
            write_csv(SPLITS_FILE, ["ticker", "execution_date", "split_from", "split_to"], rows)
            info["splits"] = len(rows)
            log(f"Prices: {len(rows)} stock splits on record since {since.isoformat()}.")
            return
    info["splits_problem"] = "Could not download the list of stock splits; the saved list (if any) is used."
    log("  " + info["splits_problem"])


def refresh_ticker_types(client, status):
    """The price service's list of every listed symbol and its type (CS = common stock, ETF = fund, ...), so the
    stat-arb test only pairs real companies. Downloaded about once a week (about 12 calls)."""
    info = status.setdefault("prices", {})
    try:
        age = (datetime.now().timestamp() - os.path.getmtime(TYPES_FILE)) / 86400
    except OSError:
        age = None
    if age is not None and age < TYPES_MAX_AGE_DAYS:
        return
    rows, url, first, pages = [], "/v3/reference/tickers", True, 0
    params = {"market": "stocks", "active": "true", "limit": 1000, "order": "asc", "sort": "ticker"}
    while url and pages < 40:
        code, data = client.get_json(url, params if first else None)
        first = False
        pages += 1
        if code != 200 or not isinstance(data, dict):
            info["types_problem"] = (f"Could not download the list of stock types ({code}); the stat-arb test uses "
                                     f"the saved list, or waits for the next run.")
            log("  " + info["types_problem"])
            return
        for r in data.get("results") or []:
            if r.get("ticker"):
                rows.append([r["ticker"], r.get("type") or ""])
        url = data.get("next_url")
    if url:                                     # stopped before the end of the list: keep the saved one
        info["types_problem"] = "The list of stock types was longer than expected; the saved list (if any) is used."
        log("  " + info["types_problem"])
        return
    if rows:
        write_csv(TYPES_FILE, ["ticker", "type"], rows)
        info["types"] = len(rows)
        log(f"Prices: {len(rows):,} listed symbols and their types saved.")


# ---------------------------------------------------------------- loading

class Panel:
    """Every saved day for every stock, as tables: rows are trading days, columns are stocks."""

    def __init__(self, days, tickers, O, H, L, C, V, split_notes):
        self.days = days
        self.tickers = tickers
        self.col = {t: j for j, t in enumerate(tickers)}
        self.O, self.H, self.L, self.C, self.V = O, H, L, C, V
        self.split_notes = split_notes
        self.ordinals = [d.toordinal() for d in days]

    def index_after(self, d):
        """The first trading day strictly after calendar date d (or len(days) if none)."""
        return bisect_right(self.ordinals, d.toordinal())

    def index_on_or_after(self, d):
        i = bisect_right(self.ordinals, d.toordinal() - 1)
        return i


def _num(x):
    try:
        v = float(x)
        return v if v == v else np.nan
    except (TypeError, ValueError):
        return np.nan


def load_panel(start=None, end=None):
    days = [d for d in saved_days() if (start is None or d >= start) and (end is None or d <= end)]
    if not days:
        return None
    names, compact, kept_days = {}, [], []
    for d in days:
        js, vals = [], []
        try:
            with gzip.open(day_path(d), "rt", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header == ["T", "o", "h", "l", "c", "v"]:
                    for row in reader:
                        if len(row) != 6 or not row[0]:
                            continue
                        j = names.get(row[0])
                        if j is None:
                            j = names[row[0]] = len(names)
                        js.append(j)
                        vals.append([_num(x) for x in row[1:]])
        except (OSError, EOFError, csv.Error, UnicodeDecodeError):
            js = []
        if not js:
            try:
                os.remove(day_path(d))                  # a damaged day file is deleted, so the next run downloads it again
            except OSError:
                pass
            continue
        compact.append((np.array(js, dtype=np.int64), np.array(vals, dtype=np.float32)))
        kept_days.append(d)
    if not kept_days:
        return None
    days = kept_days
    nd, nt = len(days), len(names)
    O, H, L, C, V = (np.full((nd, nt), np.nan, dtype=np.float32) for _ in range(5))
    for i, (js, vals) in enumerate(compact):
        O[i, js], H[i, js], L[i, js], C[i, js], V[i, js] = vals[:, 0], vals[:, 1], vals[:, 2], vals[:, 3], vals[:, 4]
    compact = None
    tickers = [None] * nt
    for t, j in names.items():
        tickers[j] = t
    notes = adjust_for_splits(days, names, O, H, L, C, V)
    return Panel(days, tickers, O, H, L, C, V, notes)


def adjust_for_splits(days, names, O, H, L, C, V):
    """Puts every price on today's share basis. Uses the official split list, then catches any reverse split the
    list missed: an overnight jump UP of almost exactly 3x, 4x, 5x ... is a reverse split, not a real move. (Drops are
    left alone: a real crash can look like a split, and forward splits are almost always on the official list.)"""
    ordinals = [d.toordinal() for d in days]
    applied, guessed = 0, 0
    known = {}
    for r in read_csv(SPLITS_FILE):
        j = names.get(r.get("ticker"))
        when = parse_day(r.get("execution_date"))
        f, t = _num(r.get("split_from")), _num(r.get("split_to"))
        if j is None or when is None or not (f > 0 and t > 0) or f == t:
            continue
        cut = bisect_right(ordinals, when.toordinal() - 1)       # rows before the execution date
        if cut <= 0:
            continue
        factor = f / t
        for arr in (O, H, L, C):
            arr[:cut, j] *= factor
        V[:cut, j] /= factor
        known.setdefault(j, []).append(cut)
        applied += 1
    # splits missing from the list
    prev_close = np.vstack([np.full((1, C.shape[1]), np.nan, dtype=np.float32), C[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = O / prev_close
    big = np.argwhere(np.isfinite(ratio) & (ratio >= 2.9))
    for i, j in big:
        r = float(ratio[i, j])
        n = round(r)
        exact = abs(r - n) / n < 0.015
        near_known = any(abs(i - k) <= 3 for k in known.get(j, []))
        if not exact or near_known or n < 3:
            continue
        factor = float(n)                               # older prices x n puts them on the new basis
        for arr in (O, H, L, C):
            arr[:i, j] *= factor
        V[:i, j] /= factor
        known.setdefault(j, []).append(int(i))
        guessed += 1
    return {"from_list": applied, "guessed": guessed}
