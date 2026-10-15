"""
experiments.py
Tests changes to the SEC-filing tracker's rules on the Signal Lab's 2 years of data BEFORE they are used for real.
GitHub runs it by hand (the "Signal Lab experiments" workflow, using the lab's saved data). It only reads saved
data and writes lab/results/experiments.json and experiments.csv. It never places orders.

Each test replays the tracker day by day with today's rules (shorts only; no shorts from insider selling alone; no
SPAC going-concern shorts; 55% budget) and changes one group of settings:
  * how long a short is held (14, 30, 45 or 60 calendar days)
  * the stop-loss and the profit target (in ATRs, the stock's usual daily move), or none
  * how many positions, and how big each one is
  * a stricter trading-volume floor, as a stand-in for "easy to borrow"
  * the IWM hedge: capped at the shorts' dollars (today), full beta, or none

  python lab/experiments.py
"""

import csv
import json
import math
import os
import time
from datetime import timedelta

import numpy as np

from lab_core import PRICE_YEARS, RESULTS_DIR, SHORT_BORROW_PER_YEAR, START_EQUITY, log, today_et
import lab_events as E
import lab_prices as LP
import lab_sec as S
import lab_study as ST

import analytics as A      # the tracker's own rules (lab_core puts the tracker folder on the path)
import config as C

BASE = {
    "name": "today's rules", "hold": C.HOLD_CALENDAR_DAYS, "stop": C.STOP_ATR_MULT, "target": C.TARGET_ATR_MULT,
    "positions": C.MAX_OPEN_POSITIONS, "max_pos": C.MAX_POSITION_PCT, "risk": C.RISK_PER_TRADE_PCT,
    "budget": 0.55, "new_per_day": C.MAX_NEW_SHORTS_PER_RUN, "candidates": C.MAX_CANDIDATES_TO_ANALYZE,
    "min_dollar_volume": C.MIN_AVG_DOLLAR_VOLUME, "hedge": "dollar",
}
HOLDS = (14, 30, 45, 60)
EXITS = (("stop 2, target 3 (today)", 2.0, 3.0), ("stop 3, target 5", 3.0, 5.0), ("stop 3, no target", 3.0, None),
         ("no stop, target 3", None, 3.0), ("no stop, no target", None, None))
BOOKS = (("20 x 5%", 20, 0.05, 0.005), ("30 x 3.5%", 30, 0.035, 0.0035), ("40 x 2.5%", 40, 0.025, 0.0025))
TOP_FOR_CHECKS = 6


# ---------------------------------------------------------------- data

def load():
    """(Market, signals) with today's tracker rules applied to every rebuilt signal."""
    today = today_et()
    default_start = today - timedelta(days=int(PRICE_YEARS * 365.25))
    saved = LP.saved_days()
    start = min([default_start] + saved[:1]) - timedelta(days=10)
    panel = LP.load_panel()
    if panel is None or len(panel.days) < 60:
        raise SystemExit("Not enough saved price days. Run the Signal Lab first.")
    M = ST.Market(panel)
    tickers = S.TickerMap(S.read_insider(start - timedelta(days=400), today))
    first, last = panel.days[0] - timedelta(days=7), panel.days[-1]
    signals = E.build_signals(first, last, {"warnings": []})
    ready = ST.prepare_signals(M, tickers, signals)
    for s in ready:
        direction, tags, _ = A.classify_signal("; ".join(s["forms"]), " | ".join(s["reasons"]), s.get("company", ""))
        s["direction"], s["tags"] = direction, tags
    log(f"Experiments: {len(panel.days)} days, {len(ready):,} signals, "
        f"{sum(1 for s in ready if s['direction'] == 'SHORT'):,} shorts under today's rules.")
    return M, ready


# ---------------------------------------------------------------- one replay

def _finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def run(M, ready, v):
    """Replays the tracker's shorts with the settings in v. Returns the results (no curve)."""
    P = M.P
    by_day = {}
    for s in ready:
        if s["direction"] == "SHORT" and s["score"] >= C.MIN_SCORE_TO_TRADE:
            by_day.setdefault(s["e"], []).append(s)
    if not by_day:
        return None
    for group in by_day.values():
        group.sort(key=lambda s: (-s["score"], s["company"]))
    start = max(min(by_day), 20)
    realized = borrow = hedge_pnl = hedge_notional = 0.0
    equity = float(START_EQUITY)
    open_t, closed, curve, gross_pct, counts = [], [], [], [], []
    full_days = 0
    for i in range(start, M.nd):
        day = P.days[i]
        if i > start and M.iwm is not None:
            a, b = M.CF[i - 1, M.iwm], M.CF[i, M.iwm]
            if a > 0 and b > 0:
                hedge_pnl += hedge_notional * (float(b) / float(a) - 1)
        still = []
        for t in open_t:                                   # exits, judged at the open (about 9:40 AM)
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
            fill = px * (1 + t["cost"])                    # buying the shares back
            pnl = (t["entry"] - fill) * t["qty"]
            realized += pnl
            closed.append({"ret": 1 - fill / t["entry"] if t["entry"] > 0 else 0.0, "pnl": pnl, "reason": reason,
                           "days": i - t["i"]})
        open_t = still
        held = {t["sym"] for t in open_t}
        gross = sum(_mark(M, t, i - 1) * t["qty"] for t in open_t)
        new, analyzed = 0, 0
        if len(open_t) >= v["positions"]:
            full_days += 1
        for s in by_day.get(i, []):
            if analyzed >= v["candidates"] or new >= v["new_per_day"] or len(open_t) >= v["positions"]:
                break
            if s["sym"] in held:
                continue
            analyzed += 1
            j = s["j"]
            price = float(P.O[i, j])
            if not price > 0 or price < C.MIN_PRICE:
                continue
            avg = float(M.AVGV[i, j])
            avg = avg if math.isfinite(avg) and avg > 0 else None
            if avg is not None and avg * price < v["min_dollar_volume"]:
                continue
            atr = float(M.ATR[i, j])
            atr = atr if math.isfinite(atr) and atr > 0 else price * C.DEFAULT_ATR_PCT
            sizing_dist = (v["stop"] or 2.0) * atr                 # with no stop, size as if it were 2 ATRs away
            caps = [equity * v["risk"] / sizing_dist, equity * v["max_pos"] / price]
            if avg:
                caps.append(avg * C.MAX_PCT_OF_DAILY_VOLUME)
            qty = int(math.floor(min(caps)))
            if qty < 1 or gross + qty * price > v["budget"] * equity:
                continue
            cost = float(ST.side_cost(avg * price if avg else 0.0))
            beta = M.beta(j, i)
            beta = A.clamp(beta if beta is not None else C.DEFAULT_BETA, 0.0, 3.0)
            open_t.append({
                "i": i, "j": j, "sym": s["sym"], "qty": qty, "entry": price * (1 - cost), "cost": cost, "beta": beta,
                "rule": {"direction": "SHORT",
                         "stop": price + v["stop"] * atr if v["stop"] else float("inf"),
                         "target": price - v["target"] * atr if v["target"] else float("-inf"),
                         "exit_by": (day + timedelta(days=v["hold"])).isoformat()},
            })
            held.add(s["sym"])
            new += 1
            gross += qty * price
        unrealized = net = net_beta = gross = 0.0
        for t in open_t:
            mark = _mark(M, t, i)
            unrealized += (t["entry"] - mark) * t["qty"]
            net -= mark * t["qty"]
            net_beta -= mark * t["qty"] * t["beta"]
            gross += mark * t["qty"]
            borrow += mark * t["qty"] * SHORT_BORROW_PER_YEAR / 252
        if v["hedge"] == "beta":
            hedge_notional = -net_beta
        elif v["hedge"] == "dollar":
            hedge_notional = max(-abs(net), min(abs(net), -net_beta))
        else:
            hedge_notional = 0.0
        equity = START_EQUITY + realized + unrealized - borrow
        curve.append((equity, equity + hedge_pnl))
        gross_pct.append(gross / equity if equity > 0 else 0.0)
        counts.append(len(open_t))
    return summarize(v, curve, closed, gross_pct, counts, full_days)


def _mark(M, t, i):
    m = float(M.CF[max(i, 0), t["j"]])
    return m if m > 0 else t["entry"]


def summarize(v, curve, closed, gross_pct, counts, full_days):
    if not curve:
        return None
    un = ST.performance([c[0] for c in curve])
    hedged = ST.performance([c[1] for c in curve])
    half = len(curve) // 2
    first = ST.performance([c[1] for c in curve[:half]])
    second = ST.performance([c[1] for c in curve[half:]], start_value=curve[half - 1][1] if half else START_EQUITY)
    rets = [t["ret"] for t in closed]
    reasons = {}
    for t in closed:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        **{k: v[k] for k in BASE},
        "yearly": hedged["yearly"], "sharpe": hedged["sharpe"], "max_drawdown": hedged["max_drawdown"],
        "volatility": hedged["volatility"], "total": hedged["total"],
        "yearly_unhedged": un["yearly"], "sharpe_unhedged": un["sharpe"], "max_drawdown_unhedged": un["max_drawdown"],
        "first_half_yearly": first["yearly"], "second_half_yearly": second["yearly"],
        "trades": len(closed), "win_rate": float(np.mean([r > 0 for r in rets])) if rets else float("nan"),
        "avg_trade": float(np.mean(rets)) if rets else float("nan"),
        "avg_days_held": float(np.mean([t["days"] for t in closed])) if closed else float("nan"),
        "avg_gross": float(np.mean(gross_pct)), "max_gross": float(np.max(gross_pct)),
        "avg_positions": float(np.mean(counts)), "days_at_position_limit": full_days / max(1, len(curve)),
        "exits": reasons, "days": len(curve),
    }


# ---------------------------------------------------------------- the tests

def variants():
    out = [dict(BASE)]
    for hold in HOLDS:
        for exit_name, stop, target in EXITS:
            for book_name, positions, max_pos, risk in BOOKS:
                v = dict(BASE, hold=hold, stop=stop, target=target, positions=positions, max_pos=max_pos, risk=risk)
                v["name"] = f"hold {hold}d, {exit_name}, {book_name}"
                out.append(v)
    return out


def more_money():
    """Today's exits with a bigger budget and more positions: what more money in the tracker would do (and risk)."""
    out = []
    for budget in (0.55, 0.70, 0.85):
        for book_name, positions, max_pos, risk in (("20 x 5%", 20, 0.05, 0.005), ("30 x 5%", 30, 0.05, 0.005)):
            for per_day in (8, 12):
                v = dict(BASE, budget=budget, positions=positions, max_pos=max_pos, risk=risk, new_per_day=per_day)
                v["name"] = f"budget {budget:.0%}, {book_name}, {per_day} new a day"
                out.append(v)
    return out


def checks(best):
    """The same settings with a stricter volume floor (a stand-in for easy-to-borrow) and other hedges."""
    out = []
    for v in best:
        for label, change in (("volume over $5M a day", {"min_dollar_volume": 5_000_000}),
                              ("full beta hedge", {"hedge": "beta"}), ("no hedge", {"hedge": "none"})):
            w = dict({k: v[k] for k in BASE}, **change)
            w["name"] = f"{v['name']} | {label}"
            out.append(w)
    return out


def rank_key(r):
    """Best first: Sharpe after the hedge, then yearly return. Results that lost money in either half rank last."""
    steady = (r["first_half_yearly"] or -1) > 0 and (r["second_half_yearly"] or -1) > 0
    return (steady, _num(r["sharpe"]), _num(r["yearly"]))


def _num(x):
    return x if isinstance(x, (int, float)) and math.isfinite(x) else -1e9


def main():
    started = time.time()
    M, ready = load()
    results = []
    for v in variants():
        r = run(M, ready, v)
        if r:
            results.append(r)
            log(f"  {r['name']}: {r['yearly'] * 100:.1f}% a year, Sharpe {r['sharpe']:.2f}, worst drop "
                f"{r['max_drawdown'] * 100:.1f}%, {r['trades']} trades, average {r['avg_gross'] * 100:.0f}% used")
    grid = list(results[1:])
    for v in more_money():
        r = run(M, ready, v)
        if r:
            results.append(r)
            log(f"  {r['name']}: {r['yearly'] * 100:.1f}% a year, Sharpe {r['sharpe']:.2f}, worst drop "
                f"{r['max_drawdown'] * 100:.1f}%")
    best = sorted(grid, key=rank_key, reverse=True)[:TOP_FOR_CHECKS]
    for v in checks(best):
        r = run(M, ready, v)
        if r:
            results.append(r)
            log(f"  {r['name']}: {r['yearly'] * 100:.1f}% a year, Sharpe {r['sharpe']:.2f}")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = {"made": today_et().isoformat(), "minutes": round((time.time() - started) / 60, 1),
           "baseline": results[0], "best": [r["name"] for r in best], "results": results}
    with open(os.path.join(RESULTS_DIR, "experiments.json"), "w") as f:
        json.dump(out, f, indent=1, default=float)
    fields = [k for k in results[0] if k != "exits"] + ["exits"]
    with open(os.path.join(RESULTS_DIR, "experiments.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({**r, "exits": json.dumps(r["exits"])})
    log(f"Experiments finished in {out['minutes']} minutes. Best: {best[0]['name'] if best else '-'}")


if __name__ == "__main__":
    main()
