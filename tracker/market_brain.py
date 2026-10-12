"""
market_brain.py
The Market Brain. It studies as many years of market data as IBKR gives (saved in tracker_data/market_history)
and every day works out the market's "weather" from five signals:
  1. Trend     is SPY above its 200-day average?
  2. Momentum  is SPY up over the last year?
  3. Calm      how jumpy has SPY been over the last month?
  4. Breadth   how many of the 9 sector funds are above their own 200-day average?
  5. Credit    are junk bonds keeping up with safe Treasury bonds?
Each signal scores from -1 (bad) to +1 (good). Their average is the weather score:
  sunny (0.6 or more), fair (0.1 to 0.6), cloudy (-0.4 to 0.1), stormy (under -0.4).

Three parts use the weather (switches in brain_config.py):
  Part 1, weather dial: looks at every past day of each kind of weather and what the market did next.
          Weather that paid well for the risk gets more money in the stock strategies, weather that paid
          badly gets less. When on, it replaces the old "SPY under its 200-day average" rule.
  Part 2, strategy tilt: looks at how each strategy did in each kind of weather and moves a little money
          toward the ones that do best in today's weather. Money only moves between strategies.
  Part 3, settings tuning: every 3 months, replays a few settings for each strategy over its last 3 years
          and switches only if another setting clearly beat the normal one.
All learning looks only at the past, and every estimate is pulled back toward "normal" when there is little
evidence, so a few lucky days cannot fool it. Nothing here places an order.
"""

import csv
import html
import json
import math
import os
import time
from datetime import date, timedelta

import numpy as np

import brain_config as BC
import config as C
import sleeve_config as SC
import sleeve_strategies as S

STATE_FILE = "brain_state.json"
STATES = ["sunny", "fair", "cloudy", "stormy"]
STATE_LABEL = {"sunny": "Sunny", "fair": "Fair", "cloudy": "Cloudy", "stormy": "Stormy", "unknown": "Unknown"}
SIGNALS = ["trend", "momentum", "calm", "breadth", "credit"]
SLEEVE_LABEL = {"trend": "Trend", "sector": "Sector rotation", "momentum": "Momentum",
                "meanrev": "Mean reversion", "pairs": "Pairs"}
PARTS = ("WEATHER", "TILT", "TUNE")


def symbols():
    out = [BC.MARKET] + list(BC.SECTOR_ETFS) + list(BC.CREDIT)
    return list(dict.fromkeys(out))


def hist_dir():
    return os.path.join(C.DATA_DIR, BC.HISTORY_FOLDER)


def mode(part):
    """'off', 'watch' or 'on' for WEATHER, TILT or TUNE."""
    value = str(getattr(BC, part, "off")).strip().lower()
    return value if value in ("off", "watch", "on") else "off"


def enabled_sleeves():
    return [n for n in S.SLEEVE_ORDER if SC.SLEEVE_ENABLED.get(n)]


# ---------------------------------------------------------------- the long price history (one CSV per symbol)

def _file(symbol, folder=None):
    return os.path.join(folder or hist_dir(), symbol.replace("/", "_") + ".csv")


def load_rows(symbol, folder=None):
    rows = {}
    try:
        with open(_file(symbol, folder), newline="") as f:
            for r in csv.DictReader(f):
                try:
                    c = float(r["close"])
                except (KeyError, TypeError, ValueError):
                    continue
                if c > 0 and math.isfinite(c):
                    rows[r["date"]] = c
    except FileNotFoundError:
        pass
    return rows


def save_rows(symbol, rows, folder=None):
    folder = folder or hist_dir()
    os.makedirs(folder, exist_ok=True)
    path = _file(symbol, folder)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        for d, c in sorted(rows.items()):
            w.writerow([d, c])
    os.replace(tmp, path)


def _steady_ratio(old, new, dates):
    """If the shared days disagree by the same ratio (a split or price adjustment since the old download),
    returns that ratio. Returns None if they agree or if the gap is not steady (then nothing is changed)."""
    pairs = [(new[d], old[d]) for d in dates if old.get(d) and new.get(d)]
    if len(pairs) < 2:
        return None
    ratios = np.array([a / b for a, b in pairs])
    med = float(np.median(ratios))
    if abs(med - 1) <= 0.02 or not np.all(np.abs(ratios / med - 1) < 0.005):
        return None
    return med


def append_new(old, new, check_days=5):
    """Adds the days in `new` that come after the last day in `old`. If the last few shared days disagree by a
    steady ratio, the old prices are rescaled first. Returns (rows, changed, note)."""
    if not new:
        return old, False, None
    if not old:
        return dict(new), True, None
    last = max(old)
    shared = sorted(d for d in new if d in old)[-check_days:]
    note = None
    ratio = _steady_ratio(old, new, shared)
    out = dict(old)
    if ratio:
        out = {d: c * ratio for d, c in out.items()}
        note = f"older prices rescaled by {ratio:.4f} (split or adjustment)"
    added = {d: c for d, c in new.items() if d > last}
    out.update(added)
    return out, bool(added or ratio), note


def combine_full(old, new):
    """A fresh long download wins. Older days that are not in it are kept, rescaled to match if needed."""
    if not old:
        return dict(new), None
    shared = sorted(d for d in new if d in old)
    ratio = _steady_ratio(old, new, shared[:20] + shared[-20:])
    out = {d: (c * ratio if ratio else c) for d, c in old.items()}
    out.update(new)
    return out, (f"older prices rescaled by {ratio:.4f}" if ratio else None)


def load_history(folder=None, before=None):
    """{symbol: {"dates", "close"}} for every brain symbol that has saved history (only days before `before`)."""
    out = {}
    for s in symbols():
        rows = load_rows(s, folder)
        if before:
            rows = {d: c for d, c in rows.items() if d < before}
        if rows:
            dates = sorted(rows)
            out[s] = {"dates": dates, "close": [rows[d] for d in dates]}
    return out


def _long_bars(adapter, symbol, duration):
    """Daily bars for a long period. Uses a longer wait than normal requests, because 30 years takes a while."""
    ib = getattr(adapter, "ib", None)
    broker = getattr(adapter, "broker", None)
    if ib is None or broker is None or not hasattr(broker, "_stock"):
        return adapter.daily_bars(symbol, duration)
    contract = broker._stock(symbol)
    if contract is None:
        return []
    args = dict(endDateTime="", durationStr=duration, barSizeSetting="1 day", whatToShow="TRADES",
                useRTH=True, formatDate=1)
    try:
        try:
            bars = ib.reqHistoricalData(contract, timeout=300, **args)
        except TypeError:                               # an older ib_async without the timeout option
            bars = ib.reqHistoricalData(contract, **args)
    except Exception:
        return []
    out = []
    for b in bars or []:
        try:
            c = float(b.close)
        except (TypeError, ValueError):
            continue
        if c > 0 and math.isfinite(c):
            out.append({"date": str(b.date)[:10], "close": c})
    return out


def fetch_long(adapter, today_iso, folder=None, log=print, clock=time.time):
    """Downloads as many years as IBKR gives for every brain symbol and saves them. Safe to run again.
    today_iso: today's date (Eastern); today's unfinished day is left out."""
    sent = []
    results = {}
    for i, s in enumerate(symbols(), 1):
        got = []
        for years in BC.FETCH_TRY_YEARS:
            now = clock()
            sent = [t for t in sent if now - t < 600]
            if len(sent) >= SC.HISTORY_REQUESTS_PER_10_MIN:
                wait = 600 - (now - sent[0]) + 1
                log(f"  waiting {wait:.0f}s so IBKR is not asked for history too fast...")
                adapter.sleep(wait)
            sent.append(clock())
            got = _long_bars(adapter, s, f"{years} Y")
            if got:
                break
        old = load_rows(s, folder)
        new = {b["date"]: b["close"] for b in got if b.get("close") and b["date"] < today_iso}
        if new:
            rows, note = combine_full(old, new)
            save_rows(s, rows, folder)
            first, last = min(rows), max(rows)
            yrs = (date.fromisoformat(last) - date.fromisoformat(first)).days / 365.25
            results[s] = {"first": first, "last": last, "days": len(rows), "years": yrs}
            log(f"  {i}/{len(symbols())} {s}: {len(rows)} days, {first} to {last} ({yrs:.1f} years)"
                + (f" - {note}" if note else ""))
        else:
            results[s] = None
            log(f"  {i}/{len(symbols())} {s}: no data from IBKR" + (" (kept the saved copy)" if old else ""))
    return results


def update_from_store(store, folder=None, notes=None):
    """Adds the newest days from the daily price download to the long history."""
    for s in symbols():
        try:
            d = store.get(s)
        except Exception:
            d = None
        if not d:
            continue
        old = load_rows(s, folder)
        rows, changed, note = append_new(old, dict(zip(d["dates"], d["close"])))
        if changed:
            save_rows(s, rows, folder)
        if note and notes is not None:
            notes.append(f"{s}: {note}")


# ---------------------------------------------------------------- the weather

def _first(a):
    idx = np.where(~np.isnan(a))[0]
    return int(idx[0]) if len(idx) else None


def _align(hist, calendar):
    """Every symbol on SPY's calendar, last price carried forward, NaN before the symbol's first day."""
    index = {d: i for i, d in enumerate(calendar)}
    out = {}
    for s, d in hist.items():
        arr = np.full(len(calendar), np.nan)
        for day, c in zip(d["dates"], d["close"]):
            i = index.get(day)
            if i is not None:
                arr[i] = c
        f = _first(arr)
        if f is None:
            continue
        last = arr[f]
        for i in range(f, len(arr)):
            if np.isnan(arr[i]):
                arr[i] = last
            else:
                last = arr[i]
        out[s] = arr
    return out


def _sma(a, k):
    out = np.full(len(a), np.nan)
    f = _first(a)
    if f is None or len(a) - f < k:
        return out
    cs = np.concatenate([[0.0], np.cumsum(a[f:])])
    out[f + k - 1:] = (cs[k:] - cs[:-k]) / k
    return out


def _rolling_vol(p, d):
    """Yearly volatility of the last d daily moves (the same math as the old regime rule)."""
    out = np.full(len(p), np.nan)
    f = _first(p)
    if f is None or len(p) - f <= d:
        return out
    lr = np.diff(np.log(p[f:]))
    win = np.lib.stride_tricks.sliding_window_view(lr, d)
    out[f + d:] = win.std(axis=1, ddof=1) * math.sqrt(252)
    return out


def classify(score):
    if score is None or not np.isfinite(score):
        return -1
    if score >= BC.SUNNY_AT:
        return 0
    if score >= BC.FAIR_AT:
        return 1
    if score >= BC.CLOUDY_AT:
        return 2
    return 3


def weather_series(hist):
    """The weather for every day in the history, each day using only prices up to that day's close.
    Returns None if there is no SPY history."""
    spy = hist.get(BC.MARKET)
    if not spy or len(spy["dates"]) < 30:
        return None
    cal = list(spy["dates"])
    n = len(cal)
    al = _align(hist, cal)
    p = al[BC.MARKET]
    sig, raw = {}, {}

    avg = _sma(p, BC.TREND_DAYS)
    raw["trend_gap"] = p / avg - 1
    sig["trend"] = np.where(np.isnan(avg), np.nan, np.where(p > avg, 1.0, -1.0))

    k = BC.MOMENTUM_DAYS
    mom = np.full(n, np.nan)
    if n > k:
        mom[k:] = p[k:] / p[:-k] - 1
    raw["year_return"] = mom
    sig["momentum"] = np.where(np.isnan(mom), np.nan, np.where(mom > 0, 1.0, -1.0))

    vol = _rolling_vol(p, BC.VOL_DAYS)
    raw["vol"] = vol
    sig["calm"] = np.clip(1 - 2 * (vol - BC.VOL_CALM) / (BC.VOL_STORM - BC.VOL_CALM), -1.0, 1.0)

    valid = np.zeros(n)
    above = np.zeros(n)
    for s in BC.SECTOR_ETFS:
        if s not in al:
            continue
        a = al[s]
        m = _sma(a, BC.BREADTH_DAYS)
        ok = ~np.isnan(m)
        valid += ok
        above += ok & (a > m)
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = np.where(valid >= 5, above / np.maximum(valid, 1), np.nan)
    raw["sectors_up"], raw["sectors_total"] = above, valid
    sig["breadth"] = 2 * frac - 1

    hy, ief = BC.CREDIT
    if hy in al and ief in al:
        ratio = al[hy] / al[ief]
        m = _sma(ratio, BC.CREDIT_DAYS)
        raw["credit_gap"] = ratio / m - 1
        sig["credit"] = np.where(np.isnan(m), np.nan, np.where(ratio > m, 1.0, -1.0))
    else:
        raw["credit_gap"] = np.full(n, np.nan)
        sig["credit"] = np.full(n, np.nan)

    stack = np.vstack([sig[x] for x in SIGNALS])
    have = ~np.isnan(stack)
    count = have.sum(axis=0)
    total = np.where(have, stack, 0.0).sum(axis=0)
    usable = (count >= 3) & ~np.isnan(sig["trend"]) & ~np.isnan(sig["calm"])
    score = np.where(usable, total / np.maximum(count, 1), np.nan)
    codes = np.array([classify(x) for x in score], dtype=int)
    return {"dates": cal, "price": p, "score": score, "codes": codes, "signals": sig, "raw": raw,
            "first": {s: v["dates"][0] for s, v in hist.items() if v.get("dates")}}


def next_day_returns(p):
    """r[t] = the market's move from day t to day t+1, plus SPY's dividend, roughly (the IBKR prices leave it out)."""
    r = np.full(len(p), np.nan)
    if len(p) > 1:
        r[:-1] = p[1:] / p[:-1] - 1 + BC.SPY_DIVIDEND_YIELD / 252
    return r


# ---------------------------------------------------------------- Part 1: the weather dial

def _monotone(values, weights):
    """Makes the numbers go down (or stay level) from sunny to stormy by averaging any pair that is out of order.
    Better weather should never get less money than worse weather; an out-of-order pair is noise."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0]:
            a, b = blocks[i], blocks[i + 1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1]), a[1] + b[1], a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, _, count in blocks:
        out += [v] * count
    return out


def learn_dial(codes, r, end):
    """Learns how much to invest in each weather from days 0..end-1 (the weather of day t and the market's
    move to day t+1). The estimate is Kelly-style: average gain divided by the swing squared. The gain starts from
    the market's long-run average (PRIOR_RETURN) and moves toward what that weather really did as its days add up.
    Better weather never gets less than worse weather. Scaled so the average day is 1.0 (normal size).
    Returns None if there are fewer than LEARN_MIN_DAYS usable days."""
    end = min(int(end), len(codes))
    c = np.asarray(codes[:end])
    x = np.asarray(r[:end], dtype=float)
    ok = (c >= 0) & np.isfinite(x)
    n = int(ok.sum())
    if n < BC.LEARN_MIN_DAYS:
        return None
    xa, ca = x[ok], c[ok]
    m = BC.PRIOR_RETURN / 252
    v = float(xa.var(ddof=1))
    k = BC.LEARN_SHRINK_DAYS
    raw, stats = {}, {}
    for i, s in enumerate(STATES):
        xs = xa[ca == i]
        ns = len(xs)
        ms = float(xs.mean()) if ns else m
        vs = float(xs.var(ddof=1)) if ns > 2 else v
        mt = (ns * ms + k * m) / (ns + k)
        vt = (ns * vs + 63 * v) / (ns + 63)
        raw[s] = mt / vt if vt > 0 else 0.0
        stats[s] = {
            "days": ns, "share": ns / n,
            "ret_year": ms * 252 if ns else None,
            "vol_year": math.sqrt(vs * 252) if ns > 2 else None,
            "sharpe": (ms / math.sqrt(vs) * math.sqrt(252)) if ns > 2 and vs > 0 else None,
        }
    ordered = _monotone([raw[s] for s in STATES], [max(stats[s]["days"], 1) for s in STATES])
    raw = dict(zip(STATES, ordered))
    avg = sum(stats[s]["share"] * raw[s] for s in STATES)
    if avg <= 0:
        dial = {s: 1.0 for s in STATES}
    else:
        dial = {s: float(min(max(raw[s] / avg, BC.DIAL_MIN), BC.DIAL_MAX)) for s in STATES}
    return {"dial": dial, "stats": stats, "days": n}


# ---------------------------------------------------------------- Parts 2 and 3: the strategies' own records

def sleeve_returns(name, data, params=None, cost_bps=5.0):
    """Replays one strategy (same rules as live) and returns {date: that day's return}."""
    import backtest
    res = backtest.simulate(name, data, cost_bps, params=params)
    out, prev = {}, 1.0
    for d, v in zip(res["dates"], res["nav"]):
        out[d] = v / prev - 1 if prev else 0.0
        prev = v
    return out


def _sharpe(values):
    a = np.asarray(values, dtype=float)
    if len(a) < 3:
        return None
    sd = float(np.std(a, ddof=1))
    return float(np.mean(a) / sd * math.sqrt(252)) if sd > 0 else None


def prev_state_map(wx):
    """{date: weather code of the trading day before it}."""
    out = {}
    for i in range(1, len(wx["dates"])):
        out[wx["dates"][i]] = int(wx["codes"][i - 1])
    return out


def money_neutral(mults, base):
    """Scales the ups or the downs so that, weighted by base share, the total stays the same."""
    extra = sum(base[k] * max(0.0, m - 1) for k, m in mults.items())
    freed = sum(base[k] * max(0.0, 1 - m) for k, m in mults.items())
    out = dict(mults)
    if extra > freed + 1e-12 and extra > 0:
        keep = freed / extra
        out = {k: (1 + (m - 1) * keep if m > 1 else m) for k, m in out.items()}
    elif freed > extra + 1e-12 and freed > 0:
        keep = extra / freed
        out = {k: (1 - (1 - m) * keep if m < 1 else m) for k, m in out.items()}
    return out


def learn_tilt(sleeve_rets, prev_state, base, end_date):
    """For each weather, how much more or less money each strategy should get, from its returns before end_date.
    Returns {"tilt": {state: {sleeve: mult}}, "sharpe": {sleeve: {state: value}}, "days": {sleeve: n}}."""
    shr = {}
    days = {}
    for k, ser in sleeve_rets.items():
        pairs = [(prev_state.get(d, -1), r) for d, r in ser.items() if d < end_date]
        pairs = [(c, r) for c, r in pairs if c >= 0 and math.isfinite(r)]
        days[k] = len(pairs)
        if len(pairs) < BC.TILT_MIN_DAYS:
            continue
        overall = _sharpe([r for _, r in pairs]) or 0.0
        shr[k] = {}
        for i, s in enumerate(STATES):
            rs = [r for c, r in pairs if c == i]
            sh = _sharpe(rs) if len(rs) >= 20 else None
            ns = len(rs) if sh is not None else 0
            shr[k][s] = (ns * (sh or 0.0) + BC.TILT_SHRINK_DAYS * overall) / (ns + BC.TILT_SHRINK_DAYS)
    tilt = {s: {k: 1.0 for k in sleeve_rets} for s in STATES}
    if len(shr) >= 2:
        for s in STATES:
            w = sum(base.get(k, 0.0) for k in shr) or 1.0
            avg = sum(base.get(k, 0.0) * shr[k][s] for k in shr) / w
            mults = {k: min(max(1 + BC.TILT_STRENGTH * (shr[k][s] - avg), BC.TILT_MIN), BC.TILT_MAX) for k in shr}
            mults = money_neutral(mults, {k: base.get(k, 0.0) for k in shr})
            tilt[s].update(mults)
    return {"tilt": tilt, "sharpe": shr, "days": days}


def apply_tilt(alloc, tilt, base):
    """This month's shares times the tilt, kept between the allocator's limits. The total never goes up."""
    out = {}
    for k, a in alloc.items():
        v = a * tilt.get(k, 1.0)
        b = base.get(k)
        if b:
            v = min(max(v, SC.ALLOC_MIN_MULT * b), SC.ALLOC_MAX_MULT * b)
        out[k] = v
    total, target = sum(out.values()), sum(alloc.values())
    if total > target + 1e-12:
        ups = {k: out[k] - alloc[k] for k in out if out[k] > alloc[k]}
        give = sum(ups.values())
        if give > 0:
            cut = min(1.0, (total - target) / give)
            for k, u in ups.items():
                out[k] -= u * cut
    return out


def normal_settings(name):
    return json.loads(json.dumps({"trend": SC.TREND, "sector": SC.SECTOR, "momentum": SC.MOMENTUM,
                                  "meanrev": SC.MEANREV, "pairs": SC.PAIRS}.get(name, {})))


def candidate_list(name):
    return [{}] + [dict(c) for c in BC.CANDIDATES.get(name, [])]


def params_label(params):
    if not params:
        return "normal"
    return ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in sorted(params.items()))


def pick_settings(series, end_date, current=None):
    """series: {label: {date: return}}. Judges each on its last TUNE_WINDOW_DAYS returns before end_date.
    Returns (label, {label: sharpe})."""
    scores = {}
    for lab, ser in series.items():
        rs = [ser[d] for d in sorted(ser) if d < end_date][-BC.TUNE_WINDOW_DAYS:]
        if len(rs) >= BC.TUNE_MIN_DAYS:
            sh = _sharpe(rs)
            if sh is not None:
                scores[lab] = sh
    if "normal" not in scores:
        return "normal", scores
    best = max(scores, key=scores.get)
    if current in scores and scores[current] >= scores[best] - BC.TUNE_KEEP_MARGIN:
        return current, scores
    if scores[best] >= scores["normal"] + BC.TUNE_MARGIN:
        return best, scores
    return "normal", scores


def tune_month(month_iso):
    """True in the months when settings are re-picked (January, April, July, October for every 3 months)."""
    m = int(month_iso[5:7])
    return (m - 1) % max(1, int(BC.TUNE_EVERY_MONTHS)) == 0


# ---------------------------------------------------------------- the monthly learned state

def settings_key():
    key = {k: getattr(BC, k) for k in dir(BC) if k.isupper() and k not in PARTS}
    key["sleeves"] = enabled_sleeves()
    key["strategy_settings"] = {"TREND": SC.TREND, "SECTOR": SC.SECTOR, "MOMENTUM": SC.MOMENTUM,
                                "MEANREV": SC.MEANREV, "PAIRS": SC.PAIRS}
    return json.loads(json.dumps(key, default=str))


def _path(name):
    return os.path.join(C.DATA_DIR, name)


def load_state():
    try:
        with open(_path(STATE_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = _path(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, _path(STATE_FILE))


def compute_state(today_iso, wx, price_dir=None, previous=None, log=None):
    """Learns the dial (Part 1), the tilt (Part 2) and the settings (Part 3) from everything before today."""
    import allocator
    month = today_iso[:7]
    names = enabled_sleeves()
    state = {"month": month, "date": today_iso, "settings": settings_key(), "complete": True,
             "parts": [], "notes": []}
    learned = learn_dial(wx["codes"], next_day_returns(wx["price"]), len(wx["dates"])) if wx else None
    state["dial"] = learned["dial"] if learned else {s: 1.0 for s in STATES}
    state["dial_stats"] = learned["stats"] if learned else {}
    state["dial_days"] = learned["days"] if learned else 0
    if wx:
        state["history"] = {"first": wx["dates"][0], "last": wx["dates"][-1],
                            "years": (date.fromisoformat(wx["dates"][-1]) - date.fromisoformat(wx["dates"][0])).days / 365.25,
                            "first_by_symbol": wx["first"]}
    if not learned:
        state["notes"].append("not enough market history to learn the dial yet (needs 3 years); normal size used")
    state["parts"].append("WEATHER")

    need_tilt, need_tune = mode("TILT") != "off", mode("TUNE") != "off"
    if not (need_tilt or need_tune) or not names:
        return state
    import backtest
    data = backtest.load_prices(price_dir or os.path.join(C.DATA_DIR, "prices"))
    if "SPY" not in data:
        state["complete"] = False
        state["notes"].append("no saved strategy price history yet; tilt and tuning wait")
        return state
    normal = {}
    for k in names:
        try:
            normal[k] = sleeve_returns(k, data)
        except Exception as e:
            state["complete"] = False
            state["notes"].append(f"could not replay {k}: {e}")
    if need_tilt and wx:
        base = allocator.base_allocations()
        t = learn_tilt(normal, prev_state_map(wx), base, today_iso)
        state["tilt"] = t["tilt"]
        state["tilt_sharpe"] = t["sharpe"]
        state["tilt_days"] = t["days"]
        state["parts"].append("TILT")
    if need_tune:
        prev_tune = (previous or {}).get("tune") or {}
        state["tune"] = {}
        for k in normal:
            prev = prev_tune.get(k)
            cands = candidate_list(k)
            cands_key = json.loads(json.dumps(cands))
            keep = (prev and prev.get("candidates") == cands_key and prev.get("normal") == normal_settings(k)
                    and prev.get("chosen_month") and not (tune_month(month) and prev.get("chosen_month") != month))
            if keep:
                state["tune"][k] = prev
                continue
            series, by_label = {"normal": normal[k]}, {"normal": {}}
            for c in cands[1:]:
                lab = params_label(c)
                by_label[lab] = c
                try:
                    series[lab] = sleeve_returns(k, data, c)
                except Exception as e:
                    state["notes"].append(f"could not replay {k} with {lab}: {e}")
            choice, scores = pick_settings(series, today_iso, (prev or {}).get("label"))
            state["tune"][k] = {"label": choice, "params": by_label.get(choice, {}), "scores": scores,
                                "chosen_month": month, "candidates": cands_key, "normal": normal_settings(k)}
            if log:
                log(f"  settings for {k}: {choice}")
        state["parts"].append("TUNE")
    return state


def current_state(today_iso, wx, save=False, price_dir=None):
    state = load_state()
    needed = {"WEATHER"} | ({"TILT"} if mode("TILT") != "off" else set()) | ({"TUNE"} if mode("TUNE") != "off" else set())
    last = wx["dates"][-1] if wx else None
    fresh = (state and state.get("month") == today_iso[:7] and state.get("settings") == settings_key()
             and state.get("complete") and needed <= set(state.get("parts") or [])
             and (state.get("history") or {}).get("first") == (wx["dates"][0] if wx else None))
    if fresh and last and (state.get("history") or {}).get("last", "") < last and state.get("dial_days", 0) == 0:
        fresh = False                                    # learning was waiting for history that has arrived
    if not fresh:
        state = compute_state(today_iso, wx, price_dir, previous=state)
        if save and state["complete"]:
            save_state(state)
    return state


# ---------------------------------------------------------------- today's decision

def _expected_last(today_iso):
    d = date.fromisoformat(today_iso) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def weather_today(wx):
    """The latest day's weather with the numbers behind it."""
    if not wx:
        return {"state": "unknown", "score": None, "as_of": None, "signals": {}, "details": {}}
    i = len(wx["dates"]) - 1
    code = int(wx["codes"][i])

    def val(a):
        x = a[i]
        return None if x is None or not np.isfinite(x) else float(x)

    details = {
        "trend_gap": val(wx["raw"]["trend_gap"]), "year_return": val(wx["raw"]["year_return"]),
        "vol": val(wx["raw"]["vol"]), "sectors_up": val(wx["raw"]["sectors_up"]),
        "sectors_total": val(wx["raw"]["sectors_total"]), "credit_gap": val(wx["raw"]["credit_gap"]),
    }
    return {"state": STATES[code] if code >= 0 else "unknown", "score": val(wx["score"]), "as_of": wx["dates"][i],
            "signals": {s: val(wx["signals"][s]) for s in SIGNALS}, "details": details}


def decide(weather, state, allocations, today_iso):
    """Turns the weather and the learned state into today's changes. Returns a dict (see today())."""
    import allocator
    modes = {p: mode(p) for p in PARTS}
    notes = list(state.get("notes") or [])
    s = weather["state"]
    usable = s in STATES and weather["as_of"] and weather["as_of"] >= (
        date.fromisoformat(_expected_last(today_iso)) - timedelta(days=4)).isoformat()
    if s in STATES and not usable:
        notes.append(f"the latest market data is from {weather['as_of']}, too old to use; the normal rules apply")
    elif s not in STATES:
        notes.append("the weather is unknown (not enough market history yet); the normal rules apply")
    dial = (state.get("dial") or {}).get(s, 1.0) if usable else 1.0
    would_scales = {k: (dial if k in BC.WEATHER_SLEEVES else 1.0) for k in allocations}
    tilt_now = ((state.get("tilt") or {}).get(s) or {}) if usable else {}
    tilt_now = {k: float(tilt_now.get(k, 1.0)) for k in allocations}
    base = allocator.base_allocations()
    would_alloc = apply_tilt(allocations, tilt_now, base)
    would_params = {k: {"label": v.get("label", "normal"), "params": v.get("params") or {}}
                    for k, v in (state.get("tune") or {}).items() if k in allocations}
    return {
        "as_of": weather["as_of"], "weather": weather, "modes": modes, "usable": bool(usable),
        "dial": dial, "dial_table": state.get("dial") or {}, "dial_stats": state.get("dial_stats") or {},
        "dial_learned": bool(state.get("dial_days")),
        "history": state.get("history") or {},
        "scales": would_scales if (modes["WEATHER"] == "on" and usable) else None,
        "would_scales": would_scales,
        "tilt": tilt_now,
        "allocations": would_alloc if modes["TILT"] == "on" else dict(allocations),
        "would_allocations": would_alloc, "before_tilt": dict(allocations),
        "params": ({k: v["params"] for k, v in would_params.items()} if modes["TUNE"] == "on" else None),
        "would_params": would_params,
        "notes": notes,
    }


def today(store, today_iso, allocations, save=False, price_dir=None, folder=None):
    """The brain's decision for today, or None if every part is switched off.
    store: the daily price store (its newest days are added to the long history). allocations: this month's
    shares from allocator.py. The returned dict has "scales" (stock strategy sizes), "allocations" (after the
    tilt) and "params" (strategy settings); each is only filled in when that part is "on"."""
    if all(mode(p) == "off" for p in PARTS):
        return None
    notes = []
    if store is not None:
        update_from_store(store, folder, notes)
    wx = weather_series(load_history(folder, before=today_iso))
    state = current_state(today_iso, wx, save=save, price_dir=price_dir)
    out = decide(weather_today(wx), state, allocations, today_iso)
    out["notes"] = notes + out["notes"]
    if not (state.get("history") or {}).get("years", 0) >= 10:
        out["notes"].append("only a few years of market history are saved. Run: python tracker/run_brain.py fetch")
    return out


# ---------------------------------------------------------------- words and pages

def _pct(x, d=0, signed=False):
    if x is None:
        return "-"
    return f"{x * 100:+.{d}f}%" if signed else f"{x * 100:.{d}f}%"


def signal_words(w):
    d = w.get("details") or {}
    parts = []
    if d.get("trend_gap") is not None:
        parts.append(f"SPY {_pct(abs(d['trend_gap']), 1)} {'above' if d['trend_gap'] > 0 else 'below'} its 200-day average")
    if d.get("year_return") is not None:
        parts.append(f"1-year move {_pct(d['year_return'], 0, True)}")
    if d.get("vol") is not None:
        parts.append(f"volatility {_pct(d['vol'])}")
    if d.get("sectors_total"):
        parts.append(f"{d['sectors_up']:.0f} of {d['sectors_total']:.0f} sectors above their average")
    if d.get("credit_gap") is not None:
        parts.append("junk bonds " + ("keeping up" if d["credit_gap"] > 0 else "falling behind"))
    return ", ".join(parts)


def _tag(m):
    return {"on": "ON", "watch": "WATCH ONLY", "off": "OFF"}[m]


def summary_lines(b):
    if not b:
        return []
    w = b["weather"]
    lines = ["MARKET BRAIN"]
    if w["state"] == "unknown":
        lines.append("  Weather: unknown (not enough market history yet)")
    else:
        lines.append(f"  Weather: {STATE_LABEL[w['state']]} (score {w['score']:+.2f}, as of {w['as_of']})")
        lines.append(f"  Signals: {signal_words(w)}")
    h = b.get("history") or {}
    m = b["modes"]
    if m["WEATHER"] != "off":
        table = ", ".join(f"{s} {b['dial_table'].get(s, 1.0):.2f}x" for s in STATES)
        names = ", ".join(SLEEVE_LABEL.get(k, k) for k in BC.WEATHER_SLEEVES if k in b["would_scales"])
        if b.get("dial_learned") and h:
            learned = f" (learned from {h.get('first', '?')[:4]}-{h.get('last', '?')[:4]}: {table})"
        else:
            learned = " (not learned yet: needs 3+ years of market history)"
        verb = "puts" if m["WEATHER"] == "on" else "would put"
        lines.append(f"  Weather dial [{_tag(m['WEATHER'])}]: {verb} {names or 'no stock strategies'} at "
                     f"{b['dial']:.2f}x normal size{learned}")
    if m["TILT"] != "off":
        tilt = ", ".join(f"{SLEEVE_LABEL.get(k, k)} {v:.2f}x" for k, v in b["tilt"].items())
        verb = "" if m["TILT"] == "on" else "would give "
        lines.append(f"  Strategy tilt [{_tag(m['TILT'])}]: {verb}{tilt or 'none'}")
    if m["TUNE"] != "off":
        tune = "; ".join(f"{SLEEVE_LABEL.get(k, k)} {v['label']}" for k, v in b["would_params"].items())
        verb = "" if m["TUNE"] == "on" else "would use "
        lines.append(f"  Settings [{_tag(m['TUNE'])}]: {verb}{tune or 'waiting for strategy history'}")
    if "watch" in m.values():
        lines.append("  WATCH ONLY parts are shown but do not change trades (switch them in brain_config.py).")
    for n in b["notes"][:5]:
        lines.append(f"  ! {n}")
    return lines


def summary_html(b):
    if not b:
        return '<p class="empty">The market brain is switched off.</p>'
    esc = html.escape
    lines = summary_lines(b)[1:]
    items = "".join(f"<li>{esc(x.strip())}</li>" for x in lines)
    rows = ""
    stats = b.get("dial_stats") or {}
    if stats:
        rows = "".join(
            "<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in [
                STATE_LABEL[s], stats[s]["days"], _pct(stats[s]["share"]), _pct(stats[s]["ret_year"], 1, True),
                _pct(stats[s]["vol_year"]), "-" if stats[s]["sharpe"] is None else f"{stats[s]['sharpe']:.2f}",
                f"{b['dial_table'].get(s, 1.0):.2f}x"]) + "</tr>" for s in STATES if s in stats)
        rows = ('<div class="wrap"><table><thead><tr><th>Weather</th><th>Days</th><th>Share</th><th>Market return/yr after</th>'
                '<th>Volatility</th><th>Sharpe</th><th>Dial</th></tr></thead><tbody>' + rows + "</tbody></table></div>")
    return f"<ul>{items}</ul>{rows}"


def page(b, title="Market brain"):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{background:#0f1115;color:#e5e7eb;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px 16px 48px}}
h1{{font-size:22px;margin:0 0 10px}}.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #262a33;white-space:nowrap}}th{{color:#9ca3af;font-weight:500}}
li{{margin:3px 0}}.empty{{color:#9ca3af}}
</style></head><body><h1>{html.escape(title)}</h1>{summary_html(b)}</body></html>"""
