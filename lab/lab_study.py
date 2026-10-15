"""
lab_study.py
The measuring part of the lab.
  * event study:  for each signal, what the stock did 1, 5, 10, 20 and 60 trading days after the tracker could
                  first trade it (the next morning's open), compared with IWM (small-company index fund), after costs
  * form scan:    the same test for every SEC form type, with a stricter bar because so many are tested
  * replay:       the tracker's exact rules (scores, daily limits, sizing, stops, targets, 14-day hold) run day by day
"""

import math
from datetime import timedelta

import numpy as np

from lab_core import (CLIP_RETURN, HORIZONS, MAIN_HORIZON, MIN_EVENTS, NEW_IDEA_MIN_EVENTS, NEW_IDEA_MIN_T,
                      SHORT_BORROW_PER_YEAR, START_EQUITY, log)
import lab_sec as S

BEARISH_TAGS = ("GOING_CONCERN", "DILUTION", "INSIDER_SELLING", "LATE_FILING", "NEGATIVE_8K")
BULLISH_TAGS = ("INSIDER_BUYING", "POSITIVE_8K", "ACTIVIST_13D")
TAG_NAMES = {
    "GOING_CONCERN": "Going concern warning",
    "DILUTION": "Dilution filing (S-1, S-3, 424B5)",
    "INSIDER_SELLING": "2+ insiders selling",
    "LATE_FILING": "Late filing (NT 10-K / NT 10-Q)",
    "NEGATIVE_8K": "Bad-news 8-K",
    "INSIDER_BUYING": "Insider buying (cluster or CEO/CFO)",
    "POSITIVE_8K": "Good-news 8-K",
    "ACTIVIST_13D": "13D filed (not traded now)",
    "EVENT_8K": "Any 8-K (the +10 points)",
}
# trading cost for one side of a trade, by how many dollars of the stock trade on an average day
COST_STEPS = ((50e6, 0.0005), (10e6, 0.0010), (2e6, 0.0025), (0.5e6, 0.0050), (0.0, 0.0100))


def side_cost(dollar_volume):
    dv = np.nan_to_num(np.asarray(dollar_volume, dtype=np.float64), nan=0.0)
    out = np.full(dv.shape, COST_STEPS[-1][1])
    for floor, cost in reversed(COST_STEPS[:-1]):
        out = np.where(dv >= floor, cost, out)
    return out


def prior_mean(X, n, min_count):
    """Average of the n days BEFORE each day (so nothing from the trading day itself leaks in)."""
    nd = X.shape[0]
    ok = np.isfinite(X)
    cs = np.zeros((nd + 1, X.shape[1]), dtype=np.float64)
    cs[1:] = np.cumsum(np.where(ok, X, 0.0), axis=0)
    cc = np.zeros((nd + 1, X.shape[1]), dtype=np.int32)
    cc[1:] = np.cumsum(ok, axis=0)
    hi = np.arange(nd)
    lo = np.maximum(hi - n, 0)
    total = cs[hi] - cs[lo]
    count = cc[hi] - cc[lo]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(count >= min_count, total / np.maximum(count, 1), np.nan)
    return out.astype(np.float32)


class Market:
    def __init__(self, panel):
        self.P = panel
        H, L, C, V = panel.H, panel.L, panel.C, panel.V
        nd, nt = C.shape
        self.nd, self.nt = nd, nt
        valid = np.isfinite(C) & (C > 0)
        seen = np.where(valid, np.arange(nd, dtype=np.int32)[:, None], -1).astype(np.int32)
        np.maximum.accumulate(seen, axis=0, out=seen)
        cols = np.arange(nt)
        self.CF = np.where(seen >= 0, C[np.maximum(seen, 0), cols], np.nan).astype(np.float32)   # last close so far
        any_valid = valid.any(axis=0)
        self.last_valid = np.where(any_valid, nd - 1 - np.argmax(valid[::-1], axis=0), -1)
        prev_c = np.vstack([np.full((1, nt), np.nan, dtype=np.float32), C[:-1]])
        with np.errstate(invalid="ignore"):
            tr = np.fmax(H - L, np.fmax(np.abs(H - prev_c), np.abs(L - prev_c)))
        self.ATR = prior_mean(tr, 14, 5)
        self.AVGV = prior_mean(V, 20, 10)
        self.iwm = panel.col.get("IWM")
        self.spy = panel.col.get("SPY")
        self.half = nd // 2
        self._resolved = {}

    def resolve(self, tickers, cik, day):
        """(column, entry day index, symbol) for a company's filing on `day`: the trade opens at the next open."""
        e = self.P.index_after(day)
        if e < 1 or e >= self.nd:
            return None
        key = (cik, e)
        if key in self._resolved:
            return self._resolved[key]
        found = None
        for s in tickers.candidates(cik, day):
            for v in S.symbol_variants(s):
                j = self.P.col.get(v)
                if j is not None and self.P.O[e, j] > 0:
                    found = (j, e, v)
                    break
            if found:
                break
        self._resolved[key] = found
        return found

    def resolve_symbol(self, symbol, day):
        e = self.P.index_on_or_after(day)
        if e < 1 or e >= self.nd or not symbol:
            return None
        for v in S.symbol_variants(symbol):
            j = self.P.col.get(v)
            if j is not None and self.P.O[e, j] > 0:
                return j, e, v
        return None

    def measure(self, j, e):
        """Returns for many events at once. j, e are arrays of column and entry-day index."""
        P = self.P
        j = np.asarray(j, dtype=np.int64)
        e = np.asarray(e, dtype=np.int64)
        entry = P.O[e, j].astype(np.float64)
        out = {"entry": entry}
        b = self.iwm
        for h in HORIZONS:
            x = e + h - 1
            ok = x < self.nd
            xc = np.minimum(x, self.nd - 1)
            with np.errstate(divide="ignore", invalid="ignore"):
                r = self.CF[xc, j] / entry - 1.0
                rb = (self.CF[xc, b] / P.O[e, b] - 1.0) if b is not None else 0.0
            ex = (r - rb).astype(np.float64)
            ex[~ok] = np.nan
            out[h] = ex
        with np.errstate(divide="ignore", invalid="ignore"):
            out["gap"] = (entry / P.C[e - 1, j] - 1.0).astype(np.float64)
        avgv = self.AVGV[e, j].astype(np.float64)
        out["dollar_volume"] = avgv * entry
        out["stopped"] = self.last_valid[j] < np.minimum(e + MAIN_HORIZON - 1, self.nd - 1)
        return out

    def beta(self, j, i, days=120):
        lo = max(1, i - days)
        a = self.CF[lo - 1:i, j].astype(np.float64)
        b = self.CF[lo - 1:i, self.iwm].astype(np.float64) if self.iwm is not None else None
        if b is None or len(a) < 22:
            return None
        with np.errstate(divide="ignore", invalid="ignore"):
            ra, rb = a[1:] / a[:-1] - 1, b[1:] / b[:-1] - 1
        m = np.isfinite(ra) & np.isfinite(rb)
        if m.sum() < 20 or np.var(rb[m]) <= 0:
            return None
        return float(np.cov(ra[m], rb[m])[0, 1] / np.var(rb[m], ddof=1))


# ---------------------------------------------------------------- statistics

def summarize(signed, e, cost, half):
    """signed: each event's return in the trade's direction (vs IWM). Averages count a single move as at most
    +/-100%. The t-stat treats events on the same day as one cluster, so a busy day can't fake a strong result."""
    signed = np.asarray(signed, dtype=np.float64)
    m = np.isfinite(signed)
    n = int(m.sum())
    if n == 0:
        return {"n": 0}
    x = np.clip(signed[m], -CLIP_RETURN, CLIP_RETURN)
    after = x - np.asarray(cost, dtype=np.float64)[m]
    ee = np.asarray(e)[m]
    mean = float(after.mean())
    uniq, inv = np.unique(ee, return_inverse=True)
    clusters = np.bincount(inv, weights=after - mean)
    days = len(uniq)
    se = math.sqrt(float((clusters ** 2).sum())) / n * math.sqrt(days / (days - 1)) if days > 1 else float("nan")
    t = mean / se if se and se > 0 else float("nan")
    first, second = after[ee < half], after[ee >= half]
    return {
        "n": n, "days": days,
        "avg": float(x.mean()), "avg_after_cost": mean, "median_after_cost": float(np.median(after)),
        "win_rate": float((after > 0).mean()), "t": float(t),
        "first_half": float(first.mean()) if len(first) else float("nan"), "n_first": int(len(first)),
        "second_half": float(second.mean()) if len(second) else float("nan"), "n_second": int(len(second)),
    }


def verdict_existing(s):
    if s.get("n", 0) < MIN_EVENTS:
        return "TOO FEW"
    a, t = s["avg_after_cost"], s["t"]
    if a > 0 and t >= 2 and s["first_half"] > 0 and s["second_half"] > 0:
        return "WORKS"
    if a > 0:
        return "WEAK"
    if t <= -2:
        return "LOSES MONEY"
    return "NO EDGE"


def verdict_new(s):
    if s.get("n", 0) < NEW_IDEA_MIN_EVENTS:
        return "TOO FEW"
    a, t = s["avg_after_cost"], s["t"]
    if a > 0 and t >= NEW_IDEA_MIN_T and s["first_half"] > 0 and s["second_half"] > 0:
        return "PROMISING"
    if a > 0 and t >= 2:
        return "MAYBE"
    return "NO"


def round_trip_cost(dollar_volume, short, horizon):
    cost = 2 * side_cost(dollar_volume)
    if short:
        cost = cost + SHORT_BORROW_PER_YEAR * horizon / 252
    return cost


def study_group(meas, idx, short, half):
    """All horizons for one group of events (idx = positions in the measured arrays)."""
    sign = -1.0 if short else 1.0
    out = {}
    for h in HORIZONS:
        cost = round_trip_cost(meas["dollar_volume"][idx], short, h)
        out[h] = summarize(sign * meas[h][idx], meas["e"][idx], cost, half)
    out["gap"] = float(np.nanmean(sign * meas["gap"][idx])) if len(idx) else float("nan")
    out["stopped_trading"] = int(meas["stopped"][idx].sum()) if len(idx) else 0
    return out


# ---------------------------------------------------------------- the scanner's signals

IDEAS = [
    ("sell_planned", "2+ insiders selling, all on pre-planned (10b5-1) schedules", True,
     lambda s: s["insider"] and s["insider"]["sell_cluster"] and s["insider"]["sellers_planned"] == s["insider"]["num_sellers"]),
    ("sell_unplanned", "2+ insiders selling, none on a pre-planned schedule", True,
     lambda s: s["insider"] and s["insider"]["sell_cluster"] and s["insider"]["sellers_unplanned"] == s["insider"]["num_sellers"]),
    ("buy_big", "Insider buying of $250,000 or more", False,
     lambda s: "INSIDER_BUYING" in s["tags"] and s["insider"]["dollars"] >= 250_000),
    ("buy_stake", "Insider buying that grew a buyer's stake by 10% or more", False,
     lambda s: "INSIDER_BUYING" in s["tags"] and s["insider"]["stake_up"] >= 0.10),
    ("gc_spac", "Going concern, SPACs (blank-check shells) only", True,
     lambda s: s["gc"] and s["spac"]),
    ("gc_not_spac", "Going concern, SPACs left out", True,
     lambda s: s["gc"] and not s["spac"]),
    ("gc_10k", "Going concern in an annual report (10-K)", True,
     lambda s: s["gc"] and s["gc_form"] == "10-K" and not s["spac"]),
    ("dil_424b5", "Dilution: 424B5 (shares actually being sold)", True,
     lambda s: "424B5" in s["forms"]),
    ("dil_reg_only", "Dilution: S-1 or S-3 only (no sale yet)", True,
     lambda s: ("S-1" in s["forms"] or "S-3" in s["forms"]) and "424B5" not in s["forms"]),
    ("gc_and_dil", "Going concern and dilution on the same day", True,
     lambda s: s["gc"] and "DILUTION" in s["tags"]),
    ("exec_leaving", "8-K: CEO or CFO leaving", True,
     lambda s: "executive departure" in s["bad"]),
    ("buyback", "8-K: new share buyback", False,
     lambda s: "share buyback" in s["good"]),
    ("guidance", "8-K: raised guidance", False,
     lambda s: "guidance raised" in s["good"]),
]


def prepare_signals(M, tickers, signals):
    """Adds the ticker, entry day, the market points and the tracker's direction to each rebuilt signal."""
    import analytics as A
    ready = []
    by_date = {}
    for s in signals:
        if s["date"] is None:
            continue
        hit = M.resolve(tickers, s["cik"], s["date"])
        if not hit:
            continue
        s["j"], s["e"], s["sym"] = hit
        by_date.setdefault(s["date"], []).append(s)
    P = M.P
    for day, group in by_date.items():
        group.sort(key=lambda s: (-s["score"], s["company"]))
        i = P.index_on_or_after(day)
        same_day = i < M.nd and P.days[i] == day
        for s in group[:10]:            # the scanner looks up market data for its top 10 only
            if not same_day or s["bullish"]:
                continue
            o, c = P.O[i, s["j"]], P.C[i, s["j"]]
            if o > 0 and c > 0:
                change = (c - o) / o * 100
                if change <= -8:
                    s["score"] += 10
                    s["reasons"] = s["reasons"] + [f"Stock fell {abs(change):.1f}% on the day (+10)"]
    for s in signals:
        if "e" not in s:
            continue
        direction, tags, _ = A.classify_signal("; ".join(s["forms"]), " | ".join(s["reasons"]), s.get("company", ""))
        s["direction"], s["tags"] = direction, tags
        ready.append(s)
    return ready


def study_signals(M, ready):
    import config as C
    if not ready:
        return {}
    meas = M.measure([s["j"] for s in ready], [s["e"] for s in ready])
    meas["e"] = np.array([s["e"] for s in ready])
    price_ok = meas["entry"] >= C.MIN_PRICE
    volume_ok = np.nan_to_num(meas["dollar_volume"], nan=0.0) >= C.MIN_AVG_DOLLAR_VOLUME
    tradeable = price_ok & volume_ok
    for k, s in enumerate(ready):
        s["tradeable"] = bool(tradeable[k])
    half = M.half

    def group(test, short, need_read=False):
        idx = np.array([k for k, s in enumerate(ready)
                        if tradeable[k] and test(s) and not (need_read and s["eightk_unread"])], dtype=np.int64)
        return study_group(meas, idx, short, half), len(idx)

    result = {"tags": [], "traded": [], "ideas": [], "tradeable": int(tradeable.sum()), "total": len(ready)}
    for tag in BEARISH_TAGS + BULLISH_TAGS + ("EVENT_8K",):
        short = tag in BEARISH_TAGS
        g, n = group(lambda s, tag=tag: tag in s["tags"], short)
        result["tags"].append({"key": tag, "name": TAG_NAMES[tag], "side": "short" if short else "long", "stats": g,
                               "verdict": verdict_existing(g[MAIN_HORIZON])})
    traded = [
        ("all_shorts", "Every short the tracker would take (score 35+)", True, lambda s: s["direction"] == "SHORT" and s["score"] >= C.MIN_SCORE_TO_TRADE),
        ("all_longs", "Every long the tracker would take (score 35+)", False, lambda s: s["direction"] == "LONG" and s["score"] >= C.MIN_SCORE_TO_TRADE_LONG),
        ("short_35", "Shorts scoring 35-44", True, lambda s: s["direction"] == "SHORT" and 35 <= s["score"] < 45),
        ("short_45", "Shorts scoring 45-59", True, lambda s: s["direction"] == "SHORT" and 45 <= s["score"] < 60),
        ("short_60", "Shorts scoring 60+", True, lambda s: s["direction"] == "SHORT" and s["score"] >= 60),
        ("long_35", "Longs scoring 35-44", False, lambda s: s["direction"] == "LONG" and 35 <= s["score"] < 45),
        ("long_45", "Longs scoring 45+", False, lambda s: s["direction"] == "LONG" and s["score"] >= 45),
    ]
    dv = np.nan_to_num(meas["dollar_volume"], nan=0.0)
    for key, name, short, test in traded:
        g, n = group(test, short, need_read=True)
        result["traded"].append({"key": key, "name": name, "side": "short" if short else "long", "stats": g,
                                 "verdict": verdict_existing(g[MAIN_HORIZON])})
    for key, name, short, test in traded[:2]:
        for size_key, size_name, size_test in (("small", "trading under $5M a day (too small for big funds)", lambda k: dv[k] < 5e6),
                                               ("large", "trading $5M+ a day", lambda k: dv[k] >= 5e6)):
            idx = np.array([k for k, s in enumerate(ready) if tradeable[k] and test(s) and not s["eightk_unread"]
                            and size_test(k)], dtype=np.int64)
            g = study_group(meas, idx, short, half)
            result["traded"].append({"key": f"{key}_{size_key}", "name": f"{name.split(' (')[0]}, {size_name}",
                                     "side": "short" if short else "long", "stats": g,
                                     "verdict": verdict_existing(g[MAIN_HORIZON])})
    for key, name, short, test in IDEAS:
        try:
            g, n = group(lambda s, test=test: bool(test(s)), short)
        except (KeyError, TypeError):
            continue
        result["ideas"].append({"key": key, "name": name, "side": "short" if short else "long", "stats": g,
                                "verdict": verdict_new(g[MAIN_HORIZON])})
    return result


# ---------------------------------------------------------------- every form type

def study_forms(M, tickers, form_events):
    import config as C
    rows = []
    for form, cik, day in form_events:
        d = S.parse_day(day) if isinstance(day, str) else day
        if d is None:
            continue
        hit = M.resolve(tickers, cik, d)
        if hit:
            rows.append((form, hit[0], hit[1]))
    if not rows:
        return []
    names, codes = np.unique(np.array([r[0] for r in rows], dtype=object), return_inverse=True)
    meas = M.measure([r[1] for r in rows], [r[2] for r in rows])
    meas["e"] = np.array([r[2] for r in rows])
    ok = (meas["entry"] >= C.MIN_PRICE) & (np.nan_to_num(meas["dollar_volume"], nan=0.0) >= C.MIN_AVG_DOLLAR_VOLUME)
    out = []
    for code, form in enumerate(names):
        idx = np.where(ok & (codes == code))[0]
        if len(idx) < NEW_IDEA_MIN_EVENTS:
            continue
        raw = summarize(meas[MAIN_HORIZON][idx], meas["e"][idx], np.zeros(len(idx)), M.half)
        short = raw.get("avg", 0) < 0
        g = study_group(meas, idx, short, M.half)
        out.append({"form": form, "side": "short" if short else "long", "stats": g,
                    "verdict": verdict_new(g[MAIN_HORIZON])})
    out.sort(key=lambda r: -abs(r["stats"][MAIN_HORIZON].get("t", 0) if np.isfinite(r["stats"][MAIN_HORIZON].get("t", 0)) else 0))
    log(f"Study: {len(out)} form types had {NEW_IDEA_MIN_EVENTS}+ tradeable events.")
    return out


# ---------------------------------------------------------------- the real signals so far

def study_live(M, live):
    import analytics as A
    import config as C
    ready = []
    pending = 0
    for s in live:
        hit = M.resolve_symbol(s["ticker"], s["day"])
        if not hit:
            if s["day"] > M.P.days[-1]:
                pending += 1
            continue
        direction, tags, _ = A.classify_signal(s["forms"], s["reasons"])
        s.update({"j": hit[0], "e": hit[1], "direction": direction, "tags": tags})
        ready.append(s)
    if not ready:
        return {"signals": len(live), "measured": 0, "pending": pending, "groups": []}
    meas = M.measure([s["j"] for s in ready], [s["e"] for s in ready])
    meas["e"] = np.array([s["e"] for s in ready])
    half = int(np.median(meas["e"]))
    trading_days = int(meas["e"].max() - meas["e"].min() + 1)
    groups = []
    for name, short, test in (
            ("Shorts the tracker would take (score 35+)", True, lambda s: s["direction"] == "SHORT" and s["score"] >= C.MIN_SCORE_TO_TRADE),
            ("Longs the tracker would take (score 35+)", False, lambda s: s["direction"] == "LONG" and s["score"] >= C.MIN_SCORE_TO_TRADE_LONG)):
        idx = np.array([k for k, s in enumerate(ready) if test(s)], dtype=np.int64)
        g = study_group(meas, idx, short, half)
        verdict = "TOO EARLY" if trading_days < 60 else verdict_existing(g[MAIN_HORIZON])
        groups.append({"name": name, "side": "short" if short else "long", "stats": g, "verdict": verdict})
    return {"signals": len(live), "measured": len(ready), "pending": pending, "groups": groups,
            "trading_days": trading_days,
            "first_day": min(s["day"] for s in ready).isoformat(), "last_day": max(s["day"] for s in ready).isoformat()}


# ---------------------------------------------------------------- replay of the tracker's rules

def replay(M, ready):
    """Runs the tracker's rules day by day on the rebuilt signals. The price at the open stands in for the
    price the tracker sees at about 9:40 AM. Shorts assume the shares could be borrowed (often not true for the
    smallest companies), so short results are a best case."""
    import analytics as A
    import config as C
    P = M.P
    by_day = {}
    for s in ready:
        if s["direction"] in ("LONG", "SHORT"):
            by_day.setdefault(s["e"], []).append(s)
    if not by_day:
        return None
    for group in by_day.values():
        group.sort(key=lambda s: (-s["score"], s["company"]))
    start = max(min(by_day), 20)              # the tracker always has weeks of price history to judge a stock
    realized, borrow, hedge_pnl, hedge_notional = 0.0, 0.0, 0.0, 0.0
    open_t, closed, curve = [], [], []
    min_score = min(C.MIN_SCORE_TO_TRADE, C.MIN_SCORE_TO_TRADE_LONG)
    caps = {"LONG": C.MAX_NEW_LONGS_PER_RUN, "SHORT": C.MAX_NEW_SHORTS_PER_RUN}
    equity = START_EQUITY
    for i in range(start, M.nd):
        day = P.days[i]
        if i > start and M.iwm is not None:
            a, b = M.CF[i - 1, M.iwm], M.CF[i, M.iwm]
            if a > 0 and b > 0:
                hedge_pnl += hedge_notional * (float(b) / float(a) - 1)
        still = []
        for t in open_t:
            px = float(P.O[i, t["j"]])
            if not px > 0:
                if i > M.last_valid[t["j"]]:
                    px, reason = float(M.CF[i, t["j"]]), "stopped trading"
                else:
                    still.append(t)
                    continue
            else:
                reason = A.exit_reason(t["rule"], px, day)
                if reason is None:
                    still.append(t)
                    continue
            fill = px * (1 + t["cost"]) if t["direction"] == "SHORT" else px * (1 - t["cost"])
            pnl = t["sign"] * (fill - t["entry"]) * t["qty"]
            realized += pnl
            closed.append({"symbol": t["symbol"], "company": t["company"], "direction": t["direction"],
                           "tags": "; ".join(t["tags"]), "score": t["score"], "entry_date": P.days[t["i"]].isoformat(),
                           "exit_date": day.isoformat(), "entry": round(t["entry"], 4), "exit": round(fill, 4),
                           "qty": t["qty"], "pnl": round(pnl, 2), "return": round(t["sign"] * (fill / t["entry"] - 1), 4),
                           "reason": reason, "days": i - t["i"]})
        open_t = still
        held = {t["symbol"] for t in open_t}
        new = {"LONG": 0, "SHORT": 0}
        added, analyzed = 0, 0
        slots = max(0, C.MAX_OPEN_POSITIONS - len(open_t))
        for s in by_day.get(i, []):
            if s["score"] < min_score:
                continue
            if analyzed >= C.MAX_CANDIDATES_TO_ANALYZE:
                break
            direction = s["direction"]
            if direction == "SHORT" and s["score"] < C.MIN_SCORE_TO_TRADE:
                continue
            if direction == "LONG" and s["score"] < C.MIN_SCORE_TO_TRADE_LONG:
                continue
            if s["sym"] in held:
                continue
            analyzed += 1
            j = s["j"]
            price = float(P.O[i, j])
            if not price > 0 or price < C.MIN_PRICE:
                continue
            avg = float(M.AVGV[i, j])
            avg = avg if math.isfinite(avg) and avg > 0 else None
            if avg is not None and avg * price < C.MIN_AVG_DOLLAR_VOLUME:
                continue
            atr = float(M.ATR[i, j])
            sized = A.size_position(direction, price, atr if math.isfinite(atr) else None, equity, avg)
            if sized["qty"] < 1:
                continue
            if new["LONG"] + new["SHORT"] >= C.MAX_NEW_TRADES_PER_RUN or added >= slots or new[direction] >= caps[direction]:
                continue
            cost = float(side_cost(avg * price if avg else 0.0))
            beta = M.beta(j, i)
            beta = A.clamp(beta if beta is not None else C.DEFAULT_BETA, 0.0, 3.0)
            open_t.append({
                "j": j, "i": i, "symbol": s["sym"], "company": s["company"], "direction": direction,
                "sign": -1 if direction == "SHORT" else 1, "qty": int(sized["qty"]), "score": s["score"],
                "entry": price * (1 - cost) if direction == "SHORT" else price * (1 + cost), "cost": cost,
                "beta": beta, "tags": s["tags"],
                "rule": {"direction": direction, "stop": sized["stop"], "target": sized["target"],
                         "exit_by": (day + timedelta(days=C.HOLD_CALENDAR_DAYS)).isoformat()},
            })
            held.add(s["sym"])
            new[direction] += 1
            added += 1
        unrealized, hedge_notional = 0.0, 0.0
        for t in open_t:
            mark = float(M.CF[i, t["j"]])
            if not mark > 0:
                mark = t["entry"]
            unrealized += t["sign"] * (mark - t["entry"]) * t["qty"]
            hedge_notional -= t["sign"] * mark * t["qty"] * t["beta"]
            if t["direction"] == "SHORT":
                borrow += mark * t["qty"] * SHORT_BORROW_PER_YEAR / 252
        equity = START_EQUITY + realized + unrealized - borrow
        curve.append((day, equity, equity + hedge_pnl, len(open_t)))
    return {"curve": curve, "trades": closed, "still_open": len(open_t)}


def performance(values, start_value=START_EQUITY):
    v = np.array([start_value] + list(values), dtype=np.float64)
    r = v[1:] / v[:-1] - 1
    years = max(len(r) / 252, 1e-9)
    peak = np.maximum.accumulate(v)
    out = {"total": float(v[-1] / v[0] - 1), "max_drawdown": float((v / peak - 1).min()), "days": len(r)}
    out["yearly"] = float((v[-1] / v[0]) ** (1 / years) - 1) if years >= 0.25 and v[-1] > 0 else float("nan")
    sd = float(r.std(ddof=1)) if len(r) > 2 else 0.0
    out["volatility"] = sd * math.sqrt(252)
    out["sharpe"] = float(r.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")
    return out


def summarize_replay(M, rep):
    if not rep or not rep["curve"]:
        return None
    days = [c[0] for c in rep["curve"]]
    un = performance([c[1] for c in rep["curve"]])
    hedged = performance([c[2] for c in rep["curve"]])
    bench = {}
    i0 = M.P.index_on_or_after(days[0])
    for name, col in (("IWM", M.iwm), ("SPY", M.spy)):
        if col is not None:
            a, b = float(M.P.O[i0, col]), float(M.CF[M.nd - 1, col])
            bench[name] = b / a - 1 if a > 0 else float("nan")
    trades = rep["trades"]
    by = {}
    for t in trades:
        for key in (t["direction"], "exit: " + t["reason"]):
            g = by.setdefault(key, [])
            g.append(t["return"])
    groups = {k: {"n": len(v), "avg": float(np.mean(v)), "win_rate": float(np.mean([x > 0 for x in v]))} for k, v in by.items()}
    pnl = [t["pnl"] for t in trades]
    return {"first_day": days[0].isoformat(), "last_day": days[-1].isoformat(), "unhedged": un, "hedged": hedged,
            "benchmarks": bench, "trades": len(trades), "still_open": rep["still_open"],
            "win_rate": float(np.mean([p > 0 for p in pnl])) if pnl else float("nan"),
            "avg_return": float(np.mean([t["return"] for t in trades])) if trades else float("nan"),
            "groups": groups,
            "curve": [(c[0].isoformat(), round(c[1], 2), round(c[2], 2)) for c in rep["curve"]]}
