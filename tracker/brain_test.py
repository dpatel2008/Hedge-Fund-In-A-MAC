"""
brain_test.py
Tests the Market Brain on history. At every step it only learns from the days before (walk-forward),
the same way it works live, so the test cannot peek at the future.

  python tracker/run_brain.py test

Test 1 (every year IBKR gave): SPY with the weather dial vs buying and holding SPY vs the old 200-day rule.
Test 2 (the years your strategies have prices for): your strategy mix with each brain part added one at a time.
Then it says which parts helped. Every decision is traded at the NEXT day's close, like the other backtests.
"""

import html
import json
import math
import os
from bisect import bisect_right

import numpy as np

import brain_config as BC
import config as C
import market_brain as M
import sleeve_config as SC

# Rough yearly average 3-month Treasury bill rates, in percent. Used as the return on cash in the tests.
TBILL = {1990: 7.5, 1991: 5.4, 1992: 3.4, 1993: 3.0, 1994: 4.3, 1995: 5.5, 1996: 5.0, 1997: 5.1, 1998: 4.8,
         1999: 4.7, 2000: 5.8, 2001: 3.4, 2002: 1.6, 2003: 1.0, 2004: 1.4, 2005: 3.2, 2006: 4.7, 2007: 4.4,
         2008: 1.4, 2009: 0.15, 2010: 0.14, 2011: 0.05, 2012: 0.09, 2013: 0.06, 2014: 0.03, 2015: 0.05,
         2016: 0.3, 2017: 0.9, 2018: 1.9, 2019: 2.1, 2020: 0.4, 2021: 0.05, 2022: 2.0, 2023: 5.1, 2024: 5.0,
         2025: 4.1, 2026: 3.7}

CRASHES = [("Dot-com crash", "2000-03-24", "2002-10-09"), ("2008 financial crisis", "2007-10-09", "2009-03-09"),
           ("2018 Q4 selloff", "2018-09-20", "2018-12-24"), ("2020 COVID crash", "2020-02-19", "2020-03-23"),
           ("2022 bear market", "2022-01-03", "2022-10-12")]


def cash_rate(day_iso):
    y = int(day_iso[:4])
    v = TBILL.get(y, TBILL[min(TBILL)] if y < min(TBILL) else TBILL[max(TBILL)])
    return v / 100 / 252


def stats(rets):
    """Yearly growth, volatility, Sharpe ratio and worst drop of a list of daily returns."""
    a = np.asarray(rets, dtype=float)
    if len(a) < 20:
        return None
    nav = np.cumprod(1 + a)
    years = len(a) / 252.0
    cagr = float(nav[-1] ** (1 / years) - 1) if nav[-1] > 0 else -1.0
    vol = float(np.std(a, ddof=1) * math.sqrt(252))
    sharpe = float(np.mean(a) / np.std(a, ddof=1) * math.sqrt(252)) if vol > 0 else None
    peak = np.maximum.accumulate(np.concatenate([[1.0], nav]))
    dd = float(np.min(np.concatenate([[1.0], nav]) / peak - 1))
    return {"days": len(a), "years": years, "total": float(nav[-1] - 1), "cagr": cagr, "vol": vol,
            "sharpe": sharpe, "max_dd": dd}


def verdict(new, old):
    if not new or not old or new.get("sharpe") is None or old.get("sharpe") is None:
        return "NOT ENOUGH DATA"
    if new["years"] < 1.0:
        return "NOT ENOUGH DATA"
    d_sh = new["sharpe"] - old["sharpe"]
    d_dd = new["max_dd"] - old["max_dd"]
    if d_sh >= BC.HELPS_SHARPE and d_dd >= -BC.HELPS_MAX_DD_WORSE:
        return "HELPS"
    if d_sh <= -BC.HELPS_SHARPE:
        return "HURTS"
    return "NO CLEAR HELP"


def _old_rule(p):
    """The old regime rule (risk.py) for every day: 1.0, or less when SPY is under its 200-day average."""
    sma = M._sma(p, SC.REGIME_SMA_DAYS)
    vol = M._rolling_vol(p, 20)
    out = np.ones(len(p))
    below = ~np.isnan(sma) & (p <= sma)
    out[below] = SC.REGIME_BELOW_SMA_SCALE
    out[below & (vol >= SC.REGIME_STRESSED_VOL)] = SC.REGIME_STRESSED_SCALE
    return out


def _dials_by_month(wx):
    """The dial table the brain would have had in each month, learned only from earlier days."""
    r1 = M.next_day_returns(wx["price"])
    first = {}
    for i, d in enumerate(wx["dates"]):
        first.setdefault(d[:7], i)
    return {m: M.learn_dial(wx["codes"], r1, i) for m, i in first.items()}


# ---------------------------------------------------------------- test 1: the weather dial on SPY

def test_market(wx, cost_bps):
    p, dates, codes = wx["price"], wx["dates"], wx["codes"]
    n = len(p)
    r1 = M.next_day_returns(p)
    dials = _dials_by_month(wx)
    old = _old_rule(p)
    w_dial = np.full(n, np.nan)
    for t in range(n):
        learned = dials.get(dates[t][:7])
        if learned and codes[t] >= 0:
            w_dial[t] = learned["dial"][M.STATES[codes[t]]]
    # the weight decided at day t's close is traded at day t+1's close and earns day t+2's move
    start = None
    for j in range(3, n):
        if np.isfinite(w_dial[j - 2]) and np.isfinite(w_dial[j - 3]):
            start = j
            break
    if start is None:
        return None
    rows = {"Buy and hold SPY": np.ones(n), "Old rule (200-day average)": old, "Weather dial (Part 1)": w_dial}
    series = {}
    for name, w in rows.items():
        out = []
        for j in range(start, n):
            ret = r1[j - 1]
            cash = cash_rate(dates[j])
            wj, wp = w[j - 2], w[j - 3]
            out.append(wj * ret + (1 - wj) * cash - abs(wj - wp) * cost_bps / 1e4)
        series[name] = out
    span = dates[start:]
    table = {name: dict(stats(s), invested=float(np.mean(rows[name][start - 2:n - 2]))) for name, s in series.items()}
    crashes = []
    for label, a, b in CRASHES:
        i0, i1 = bisect_right(span, a), bisect_right(span, b)
        if i0 == 0 or i1 <= i0 or a < span[0]:
            continue
        crashes.append({"name": label, "from": a, "to": b,
                        "returns": {name: float(np.prod(1 + np.asarray(s[i0:i1])) - 1) for name, s in series.items()}})
    learned_all = M.learn_dial(codes, r1, n - 1)
    return {"from": span[0], "to": span[-1], "table": table, "crashes": crashes,
            "verdict": verdict(table["Weather dial (Part 1)"], table["Old rule (200-day average)"]),
            "weather": learned_all, "dials": dials, "series_dates": span}


# ---------------------------------------------------------------- test 2: your strategies

def test_strategies(wx, price_dir, cost_bps, log=print):
    import allocator
    import backtest
    data = backtest.load_prices(price_dir)
    names = M.enabled_sleeves()
    if "SPY" not in data or not names:
        return None
    base = allocator.base_allocations()
    log("  replaying your strategies (this takes a minute)...")
    normal = {k: M.sleeve_returns(k, data, None, cost_bps) for k in names}
    cands = {}
    for k in names:
        cands[k] = {"normal": normal[k]}
        for c in M.candidate_list(k)[1:]:
            cands[k][M.params_label(c)] = M.sleeve_returns(k, data, c, cost_bps)
    cal = sorted(set.intersection(*[set(v) for v in normal.values()]))
    if len(cal) < 60:
        return None

    wx_index = {d: i for i, d in enumerate(wx["dates"])}

    def code_at(d):
        i = wx_index.get(d)
        if i is None:
            i = bisect_right(wx["dates"], d) - 1
        return int(wx["codes"][i]) if i >= 0 else -1

    old = _old_rule(wx["price"])

    def old_at(d):
        i = wx_index.get(d)
        if i is None:
            i = bisect_right(wx["dates"], d) - 1
        return float(old[i]) if i >= 0 else 1.0

    dials = _dials_by_month(wx)
    prev_state = M.prev_state_map(wx)
    month_first = {}
    for d in cal:
        month_first.setdefault(d[:7], d)
    tilts, tilt_from = {}, None
    for m, d0 in month_first.items():
        t = M.learn_tilt(normal, prev_state, base, d0)
        tilts[m] = t["tilt"]
        if tilt_from is None and len(t["sharpe"]) >= 2:
            tilt_from = d0
    picks, current, tune_from, pick_log = [], {k: "normal" for k in names}, None, []
    for m, d0 in month_first.items():
        if not M.tune_month(m):
            continue
        chosen = {}
        for k in names:
            lab, scores = M.pick_settings(cands[k], d0, current[k])
            chosen[k] = lab
            if scores and tune_from is None:
                tune_from = d0
        current = dict(chosen)
        picks.append((d0, chosen))
        pick_log.append({"date": d0, "picks": chosen})
    pick_dates = [d for d, _ in picks]

    def pick_at(d):
        i = bisect_right(pick_dates, d) - 1        # a pick made on d0 is used from the day after d0
        while i >= 0 and pick_dates[i] >= d:
            i -= 1
        return picks[i][1] if i >= 0 else {k: "normal" for k in names}

    def weights(kind, x):
        c = code_at(x)
        s = M.STATES[c] if c >= 0 else None
        if kind == "fixed":
            return dict(base)
        if kind == "today":
            sc = old_at(x)
            return {k: base[k] * (sc if k in SC.REGIME_SCALED_SLEEVES else 1.0) for k in names}
        learned = dials.get(x[:7])
        dial = learned["dial"][s] if (learned and s) else 1.0
        alloc = dict(base)
        if kind in ("tilt", "tune") and s:
            alloc = M.apply_tilt(base, tilts.get(x[:7], {}).get(s, {}), base)
        return {k: alloc[k] * (dial if k in BC.WEATHER_SLEEVES else 1.0) for k in names}

    kinds = [("fixed", "Fixed shares, no market rule"), ("today", "Today's rules (old 200-day rule)"),
             ("dial", "+ Weather dial (Part 1)"), ("tilt", "+ Strategy tilt (Part 2)"),
             ("tune", "+ Settings tuning (Part 3)")]
    series = {}
    for kind, label in kinds:
        out, prev_w, prev_pick = [], None, None
        for j in range(2, len(cal)):
            d, x = cal[j], cal[j - 2]
            w = weights(kind, x)
            pick = pick_at(cal[j - 1]) if kind == "tune" else None
            r = 0.0
            for k in names:
                src = cands[k].get(pick[k], normal[k]) if pick else normal[k]
                r += w[k] * src.get(d, normal[k].get(d, 0.0))
            r += (1 - sum(w.values())) * cash_rate(d)
            if prev_w is not None:
                r -= sum(abs(w[k] - prev_w[k]) for k in names) * cost_bps / 1e4
            if pick and prev_pick:
                r -= sum(w[k] for k in names if pick[k] != prev_pick[k]) * cost_bps / 1e4
            out.append(r)
            prev_w, prev_pick = w, pick
        series[label] = out
    span = cal[2:]

    def window(label, start):
        i = bisect_right(span, start) if start else 0
        return stats(series[label][i:])

    table = {label: stats(s) for label, s in series.items()}
    labels = [label for _, label in kinds]
    checks = {
        "WEATHER": {"vs": labels[1], "from": span[0], "new": table[labels[2]], "old": table[labels[1]]},
        "TILT": {"vs": labels[2], "from": tilt_from, "new": window(labels[3], tilt_from) if tilt_from else None,
                 "old": window(labels[2], tilt_from) if tilt_from else None},
        "TUNE": {"vs": labels[3], "from": tune_from, "new": window(labels[4], tune_from) if tune_from else None,
                 "old": window(labels[3], tune_from) if tune_from else None},
    }
    for c in checks.values():
        c["verdict"] = verdict(c["new"], c["old"])
    return {"from": span[0], "to": span[-1], "sleeves": names, "table": table, "order": labels, "checks": checks,
            "picks": pick_log[-6:]}


# ---------------------------------------------------------------- everything together

def run(folder=None, price_dir=None, cost_bps=None, log=print):
    cost_bps = BC.TEST_COST_BPS if cost_bps is None else cost_bps
    hist = M.load_history(folder)
    wx = M.weather_series(hist)
    if not wx:
        raise SystemExit("No market history yet. Run: python tracker/run_brain.py fetch")
    log(f"  market history: {wx['dates'][0]} to {wx['dates'][-1]} ({len(wx['dates'])} days)")
    t1 = test_market(wx, cost_bps)
    t2 = test_strategies(wx, price_dir or os.path.join(C.DATA_DIR, "prices"), cost_bps, log)
    rec = {}
    v1 = t1["verdict"] if t1 else "NOT ENOUGH DATA"
    v1b = t2["checks"]["WEATHER"]["verdict"] if t2 else "NOT ENOUGH DATA"
    if v1 == "HELPS" and v1b != "HURTS":
        rec["WEATHER"] = ("on", f"it helped on SPY over the long history ({v1}) and on your strategies ({v1b})")
    else:
        rec["WEATHER"] = ("watch", f"long-history test: {v1}; your strategies: {v1b}")
    for part in ("TILT", "TUNE"):
        v = t2["checks"][part]["verdict"] if t2 else "NOT ENOUGH DATA"
        rec[part] = ("on" if v == "HELPS" else "watch", f"your strategies: {v}")
    first = {s: d for s, d in wx["first"].items()}
    return {"history": {"from": wx["dates"][0], "to": wx["dates"][-1], "first": first}, "test1": t1, "test2": t2,
            "recommend": rec, "cost_bps": cost_bps}


def _p(x, d=1, signed=False):
    if x is None:
        return "-"
    return f"{x * 100:+.{d}f}%" if signed else f"{x * 100:.{d}f}%"


def _sh(x):
    return "-" if x is None else f"{x:.2f}"


def format_text(out):
    h = out["history"]
    first = h["first"]
    sectors = [first[s] for s in BC.SECTOR_ETFS if s in first]
    starts = [f"SPY from {first.get(BC.MARKET, '-')}"]
    if sectors:
        starts.append(f"sector funds from {max(sectors)}")
    if BC.CREDIT[0] in first:
        starts.append(f"junk bonds ({BC.CREDIT[0]}) from {first[BC.CREDIT[0]]}")
    lines = ["MARKET BRAIN TEST (learning only from the past at every step)",
             f"Market history: {h['from']} to {h['to']}: " + ", ".join(starts) + ".", ""]
    t1 = out["test1"]
    if t1 and t1.get("weather"):
        lines.append("WHAT THE MARKET DID AFTER EACH KIND OF WEATHER (all history)")
        lines.append(f"{'weather':<9}{'days':>7}{'share':>8}{'return/yr':>11}{'vol/yr':>9}{'Sharpe':>8}{'dial now':>10}")
        st, dial = t1["weather"]["stats"], t1["weather"]["dial"]
        for s in M.STATES:
            x = st[s]
            lines.append(f"{s:<9}{x['days']:>7}{_p(x['share'], 0):>8}{_p(x['ret_year'], 1, True):>11}"
                         f"{_p(x['vol_year'], 0):>9}{_sh(x['sharpe']):>8}{dial[s]:>9.2f}x")
        lines.append("")
    if t1:
        lines.append(f"TEST 1: SPY, {t1['from']} to {t1['to']}")
        lines.append(f"{'':<30}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'worst drop':>12}{'invested':>10}")
        for name, x in t1["table"].items():
            lines.append(f"{name:<30}{_p(x['cagr']):>8}{_p(x['vol'], 0):>8}{_sh(x['sharpe']):>8}{_p(x['max_dd']):>12}"
                         f"{_p(x['invested'], 0):>10}")
        if t1["crashes"]:
            lines.append("  In the big drops:")
            for c in t1["crashes"]:
                lines.append(f"    {c['name']:<24}" + "  ".join(f"{k.split(' (')[0]} {_p(v, 0, True)}" for k, v in c["returns"].items()))
        lines.append(f"  Verdict for the weather dial: {t1['verdict']}")
        lines.append("")
    else:
        lines += ["TEST 1: not enough market history (needs about 4 years). Run: python tracker/run_brain.py fetch", ""]
    t2 = out["test2"]
    if t2:
        lines.append(f"TEST 2: YOUR STRATEGIES ({', '.join(t2['sleeves'])}), {t2['from']} to {t2['to']}")
        lines.append(f"{'':<36}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'worst drop':>12}")
        for name in t2["order"]:
            x = t2["table"][name]
            if x:
                lines.append(f"{name:<36}{_p(x['cagr']):>8}{_p(x['vol'], 0):>8}{_sh(x['sharpe']):>8}{_p(x['max_dd']):>12}")
        for part, c in t2["checks"].items():
            since = f" (judged from {c['from']}, when it had enough history)" if part != "WEATHER" and c["from"] else ""
            lines.append(f"  {part}: {c['verdict']}{since}")
        if t2["picks"]:
            last = t2["picks"][-1]
            lines.append(f"  Latest settings picks ({last['date']}): " + ", ".join(f"{k} {v}" for k, v in last["picks"].items()))
        lines.append("")
    else:
        lines += ["TEST 2: no strategy price history. Run: python tracker/run_sleeves.py prefetch --years 8", ""]
    lines.append("WHAT TO SET IN brain_config.py")
    for part, (value, why) in out["recommend"].items():
        lines.append(f'  {part} = "{value}"   ({why})')
    lines += ["", f"Costs {out['cost_bps']:g} bps per dollar traded. Cash earns rough historical T-bill rates (the sleeve "
              "backtest uses a flat 4%, so its numbers differ). SPY gets a rough "
              f"{BC.SPY_DIVIDEND_YIELD * 100:.1f}% dividend added back.",
              "Limits: test 2 uses today's big companies (survivorship bias) and only a few years; a part that helped in the "
              "past can still fail. Treat this as evidence, not a promise."]
    return "\n".join(lines)


def write_report(out, path):
    text = html.escape(format_text(out))
    with open(path, "w") as f:
        f.write("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Market brain test</title></head>"
                "<body style='font:13px/1.4 ui-monospace,Menlo,monospace;background:#0f1115;color:#e5e7eb;padding:16px'>"
                f"<h2 style='font-family:-apple-system,sans-serif'>Market brain test</h2><pre style='overflow-x:auto'>{text}</pre></body></html>")
    return path


def save_json(out, path):
    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items() if k not in ("dials", "series_dates")}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return o
    with open(path, "w") as f:
        json.dump(clean(out), f, indent=1)
    return path
