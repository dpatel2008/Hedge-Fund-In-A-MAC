"""
allocator.py
Two jobs:
  1. Decides how much of the account each strategy sleeve gets this month (risk-based, automatic).
  2. Builds the "capital by strategy" table: how much money each strategy is using right now,
     including the SEC-filing tracker, the IWM hedge, the T-bill sleeve and money nothing is using.
     "Using" means long value plus short value (a $10,000 short uses $10,000 of the account's room).

How the monthly decision works (all limits are in sleeve_config.py):
  * Every sleeve starts from its normal share (SLEEVE_ALLOCATION).
  * It replays each strategy on the saved price history (the same rules the live sleeve uses).
  * If a strategy has been swinging more than its usual amount lately, it gets less money, and if it has
    been calmer than usual it gets more (volatility management).
  * A strategy that lost money over the last year gets less. One with a good year gets a little more.
  * A strategy in a deep slump (down a lot from its high) gets cut in half until it recovers.
  * No sleeve goes below half or above 1.5 times its normal share. Money only moves between sleeves:
    a sleeve can get extra only out of money another sleeve gave up, so together they never get more
    than their normal total. Money no sleeve uses goes to the T-bill sleeve.
Nothing here places an order.
"""

import html
import json
import math
import os
from datetime import datetime

import numpy as np

import config as C
import sleeve_config as SC
import sleeve_strategies as S

STATE_FILE = "allocation_state.json"
CASH_SYMBOLS = (SC.CASH_SYMBOL, SC.CASH_FALLBACK_SYMBOL)
LABELS = {
    "tracker": "SEC-filing tracker", "hedge": "IWM hedge", "trend": "Trend", "sector": "Sector rotation",
    "momentum": "Momentum", "meanrev": "Mean reversion", "pairs": "Pairs", "cash": "T-bills (idle cash)",
    "plain": "Cash (includes money from short sales)",
}


def _path(name):
    return os.path.join(C.DATA_DIR, name)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 1. the monthly allocation

def base_allocations():
    return {n: SC.SLEEVE_ALLOCATION.get(n, 0.0) for n in S.SLEEVE_ORDER if SC.SLEEVE_ENABLED.get(n)}


def sleeve_metrics(nav):
    """nav: daily values of one strategy, oldest first. Returns numbers that describe its recent risk."""
    a = np.asarray([x for x in nav if x], dtype=float)
    if len(a) < SC.ALLOC_MIN_DAYS + 1:
        return None
    r = a[1:] / a[:-1] - 1
    short = r[-SC.ALLOC_SHORT_VOL_DAYS:]
    vol_short = float(np.std(short, ddof=1) * math.sqrt(252)) if len(short) > 2 else 0.0
    vol_long = float(np.std(r, ddof=1) * math.sqrt(252)) if len(r) > 2 else 0.0
    year = a[-min(len(a), 253):]
    year_r = year[1:] / year[:-1] - 1
    ret_year = float(year[-1] / year[0] - 1)
    sd = float(np.std(year_r, ddof=1)) if len(year_r) > 2 else 0.0
    sharpe = float(np.mean(year_r) / sd * math.sqrt(252)) if sd > 0 else 0.0
    drawdown = float(year[-1] / np.max(year) - 1)
    return {"days": int(len(a)), "vol_short": vol_short, "vol_long": vol_long, "ret_year": ret_year,
            "sharpe": sharpe, "drawdown": drawdown}


def decide(metrics, base):
    """metrics: {sleeve: dict or None}. base: {sleeve: normal share}. Returns {sleeve: decision}."""
    out = {}
    for name, b in base.items():
        m = metrics.get(name)
        mult, reasons = 1.0, []
        if m is None:
            reasons.append("not enough history yet, normal share")
        else:
            if m["vol_short"] > 0 and m["vol_long"] > 0:
                ratio = m["vol_long"] / m["vol_short"]
                ratio = min(max(ratio, SC.ALLOC_VOL_MULT_MIN), SC.ALLOC_VOL_MULT_MAX)
                if ratio < 0.95:
                    reasons.append(f"swinging more than usual ({m['vol_short'] * 100:.0f}% vs {m['vol_long'] * 100:.0f}% a year)")
                elif ratio > 1.05:
                    reasons.append(f"calmer than usual ({m['vol_short'] * 100:.0f}% vs {m['vol_long'] * 100:.0f}% a year)")
                mult *= ratio
            if m["ret_year"] < 0:
                mult *= SC.ALLOC_LOSER_MULT
                reasons.append(f"lost {abs(m['ret_year']) * 100:.1f}% over the last year")
            elif m["sharpe"] >= SC.ALLOC_WINNER_SHARPE:
                mult *= SC.ALLOC_WINNER_MULT
                reasons.append(f"good year (+{m['ret_year'] * 100:.1f}%, Sharpe {m['sharpe']:.2f})")
            if m["drawdown"] <= -SC.ALLOC_DRAWDOWN_LIMIT:
                mult *= 0.5
                reasons.append(f"in a slump ({m['drawdown'] * 100:.1f}% below its high), cut in half")
        mult = min(max(mult, SC.ALLOC_MIN_MULT), SC.ALLOC_MAX_MULT)
        out[name] = {"base": b, "mult": mult, "alloc": b * mult, "reasons": reasons or ["normal"], "metrics": m}
    # money only moves between sleeves: the extra given to strong/calm sleeves can never be more than
    # what was taken from weak/jumpy ones, so the sleeves together never get more than their normal total
    extra = sum(max(0.0, d["alloc"] - d["base"]) for d in out.values())
    freed = sum(max(0.0, d["base"] - d["alloc"]) for d in out.values())
    if extra > freed + 1e-12:
        keep = freed / extra if extra > 0 else 0.0
        for d in out.values():
            if d["alloc"] > d["base"]:
                d["alloc"] = d["base"] + (d["alloc"] - d["base"]) * keep
                d["mult"] = d["alloc"] / d["base"] if d["base"] else 1.0
                if keep < 1.0:
                    d["reasons"].append("extra limited: no other sleeve gave up money" if keep == 0
                                        else "extra limited to the money the other sleeves gave up")
    return out


def settings_key():
    """Everything that changes the decision. If any of it changes, the month is decided again."""
    return {k: getattr(SC, k) for k in dir(SC) if k.startswith("ALLOC_")}


def compute(today_iso, price_dir=None, cost_bps=5.0):
    """Replays each enabled sleeve on the saved prices and decides this month's shares."""
    import backtest
    base = base_allocations()
    metrics = {}
    complete = True
    data = backtest.load_prices(price_dir or _path("prices"))
    if "SPY" not in data:
        complete = False
    else:
        calendar = data["SPY"]["dates"]
        start = calendar[-SC.ALLOC_LOOKBACK_DAYS] if len(calendar) > SC.ALLOC_LOOKBACK_DAYS else None
        for name in base:
            try:
                res = backtest.simulate(name, data, cost_bps, start=start)
                metrics[name] = sleeve_metrics(res["nav"])
            except Exception as e:  # a strategy that cannot be replayed keeps its normal share
                metrics[name] = None
                complete = False
                print(f"  allocation: could not replay {name}: {e}")
    decisions = decide(metrics, base)
    return {"month": today_iso[:7], "date": today_iso, "base": base, "settings": settings_key(),
            "complete": complete, "sleeves": decisions}


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
        json.dump(state, f, indent=2)
    os.replace(tmp, _path(STATE_FILE))


def current(today_iso, save=False, price_dir=None):
    """This month's allocation. Recomputed once a month (or when the sleeve settings change).
    Returns (state, {sleeve: share of the account})."""
    base = base_allocations()
    if not SC.DYNAMIC_ALLOCATION:
        state = {"month": today_iso[:7], "date": today_iso, "base": base,
                 "sleeves": {n: {"base": b, "mult": 1.0, "alloc": b, "reasons": ["automatic shifting is off"],
                                 "metrics": None} for n, b in base.items()}}
        return state, {n: b for n, b in base.items()}
    state = load_state()
    fresh = (state and state.get("month") == today_iso[:7] and state.get("base") == base
             and state.get("settings") == settings_key() and state.get("complete"))
    if not fresh:
        state = compute(today_iso, price_dir)
        if save and state["complete"]:            # an incomplete decision is used today but tried again next run
            save_state(state)
    return state, {n: d["alloc"] for n, d in state["sleeves"].items()}


# ---------------------------------------------------------------- 2. capital by strategy

def capital_rows(equity, positions, ledger_positions, allocations):
    """equity: account value. positions: every position in the account (stocks AND options).
    ledger_positions: {sleeve: {symbol: {"qty", "avg_cost"}}} from the sleeve ledger.
    allocations: {sleeve: share} for this month. Returns a list of row dicts plus a totals dict."""
    by_key = {}
    for p in positions:
        by_key.setdefault(p["symbol"], []).append(p)
    owner = {}
    for sleeve, held in ledger_positions.items():
        for symbol in held:
            owner[symbol] = sleeve

    def stock_price(symbol, fallback):
        for p in by_key.get(symbol, []):
            if p.get("sec_type", "STK") == "STK" and _num(p.get("market_price")):
                return _num(p["market_price"])
        return fallback

    def blank(key, budget_pct):
        return {"key": key, "name": LABELS[key], "budget_pct": budget_pct,
                "budget": (budget_pct or 0.0) * equity if budget_pct is not None else None,
                "long": 0.0, "short": 0.0, "options": 0.0, "positions": 0, "pnl": 0.0}

    rows = {"tracker": blank("tracker", SC.TRACKER_MAX_GROSS_PCT), "hedge": blank("hedge", None)}
    for name in S.SLEEVE_ORDER:
        rows[name] = blank(name, allocations.get(name, 0.0) if SC.SLEEVES_ENABLED and SC.SLEEVE_ENABLED.get(name) else 0.0)
    rows["cash"] = blank("cash", None)

    # sleeve positions, from the ledger (a symbol can be shared between a sleeve and the tracker)
    sleeve_qty, sleeve_pnl = {}, {}
    for sleeve, held in ledger_positions.items():
        key = sleeve if sleeve in rows else "cash"
        for symbol, pos in held.items():
            q = _num(pos.get("qty")) or 0.0
            cost = _num(pos.get("avg_cost")) or 0.0
            price = stock_price(symbol, cost)
            value = q * price
            row = rows[key]
            if value >= 0:
                row["long"] += value
            else:
                row["short"] += -value
            row["positions"] += 1
            row["pnl"] += (price - cost) * q
            sleeve_qty[symbol] = sleeve_qty.get(symbol, 0.0) + q
            sleeve_pnl[symbol] = sleeve_pnl.get(symbol, 0.0) + (price - cost) * q

    option_net = {}       # underlying symbol -> net option value (a spread's short leg offsets its long leg)
    for p in positions:
        value = _num(p.get("market_value")) or 0.0
        symbol, kind = p["symbol"], p.get("sec_type", "STK")
        if kind == "STK":
            qty = _num(p.get("qty")) or 0.0
            extra = qty - sleeve_qty.get(symbol, 0.0)      # shares that are not owned by a sleeve
            if abs(extra) < 1:
                continue
            if symbol in sleeve_qty and (extra * qty <= 0 or abs(extra) > abs(qty)):
                continue                                    # the ledger and the account disagree: not tracker money
            price = _num(p.get("market_price")) or (value / qty if qty else 0.0)
            key = "hedge" if symbol == C.HEDGE_SYMBOL else "tracker"
            row = rows[key]
            part = extra * price
            if part >= 0:
                row["long"] += part
            else:
                row["short"] += -part
            row["positions"] += 1
            unreal = _num(p.get("unrealized"))
            if unreal is not None:
                row["pnl"] += unreal - sleeve_pnl.get(symbol, 0.0)
        else:
            option_net[symbol] = option_net.get(symbol, 0.0) + value
            rows["tracker"]["pnl"] += _num(p.get("unrealized")) or 0.0
    for symbol, net in option_net.items():
        rows["tracker"]["options"] += abs(net)
        rows["tracker"]["positions"] += 1

    out = []
    for key in ["tracker", "hedge"] + list(S.SLEEVE_ORDER) + ["cash"]:
        row = rows[key]
        row["gross"] = row["long"] + row["short"] + row["options"]
        row["gross_pct"] = row["gross"] / equity if equity else 0.0
        if key == "hedge" and row["gross"] == 0:
            continue
        out.append(row)
    invested = sum(r["gross"] for r in out if r["key"] not in ("cash", "hedge"))   # what the total limit counts
    # the account's cash: what is left after paying for everything bought, plus the money short sales brought in
    unused = equity - sum(r["long"] + r["options"] for r in out) + sum(r["short"] for r in out)
    out.append({"key": "plain", "name": LABELS["plain"], "budget_pct": SC.CASH_BUFFER_PCT if SC.CASH_SLEEVE_ENABLED else None,
                "budget": SC.CASH_BUFFER_PCT * equity if SC.CASH_SLEEVE_ENABLED else None, "long": unused,
                "short": 0.0, "options": 0.0, "positions": 0, "pnl": 0.0, "gross": unused,
                "gross_pct": unused / equity if equity else 0.0})
    totals = {"equity": equity, "invested": invested, "invested_pct": invested / equity if equity else 0.0,
              "cap_pct": SC.MAX_TOTAL_GROSS_PCT}
    return out, totals


def _money(x):
    if x is None:
        return "-"
    return f"{'-' if x < 0 else ''}${abs(x):,.0f}"


def _pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def _budget_words(r):
    if r["key"] == "plain":
        return f"keep at least {_pct(r['budget_pct'])}" if r.get("budget_pct") is not None else "no target"
    if r["key"] == "cash":
        return "gets the money nobody else is using"
    if r["key"] == "hedge":
        return "sized to offset the tracker's shorts (never bigger than them)"
    if r["key"] not in ("tracker",) and not r.get("budget_pct"):
        return "switched off"
    return f"budget {_pct(r['budget_pct'])} ({_money(r['budget'])})"


def capital_text(rows, totals, state=None):
    lines = ["CAPITAL BY STRATEGY",
             f"  Account value {_money(totals['equity'])}. Invested (long + short, not counting the IWM hedge) "
             f"{_money(totals['invested'])} = {_pct(totals['invested_pct'])} (limit {_pct(totals['cap_pct'])})."]
    for r in rows:
        budget = _budget_words(r)
        detail = ""
        if r["key"] not in ("plain",):
            detail = f", long {_money(r['long'])}, short {_money(r['short'])}"
            if r["options"]:
                detail += f", options {_money(r['options'])}"
            detail += f", {r['positions']} position(s), open P&L {_money(r['pnl'])}"
        lines.append(f"  - {r['name']}: using {_money(r['gross'])} = {_pct(r['gross_pct'])} | {budget}{detail}")
    if state and state.get("sleeves"):
        off = "" if SC.SLEEVES_ENABLED else " - sleeves are switched OFF, so these are not in use yet"
        lines.append(f"  This month's strategy shares (decided {state.get('date')}){off}:")
        for name, d in state["sleeves"].items():
            lines.append(f"    {LABELS.get(name, name)}: {d['alloc'] * 100:.1f}% (normal {d['base'] * 100:.0f}%) - "
                         + "; ".join(d["reasons"]))
    return "\n".join(lines)


def capital_html(rows, totals, state=None):
    esc = html.escape
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in [
            r["name"], _budget_words(r), _money(r["gross"]), _pct(r["gross_pct"]),
            _money(r["long"]) if r["key"] != "plain" else "-", _money(r["short"]) if r["key"] != "plain" else "-",
            _money(r["options"]) if r["options"] else "-", r["positions"] or "-",
            _money(r["pnl"]) if r["key"] != "plain" else "-",
        ]) + "</tr>" for r in rows)
    table = ('<div class="wrap"><table><thead><tr><th>Strategy</th><th>Budget</th><th>Using now</th>'
             '<th>% of account</th><th>Long</th><th>Short</th><th>Options</th><th>Positions</th><th>Open P&amp;L</th>'
             f'</tr></thead><tbody>{body}</tbody></table></div>')
    alloc = ""
    if state and state.get("sleeves"):
        items = "".join(
            f"<li><b>{esc(LABELS.get(n, n))}</b>: {d['alloc'] * 100:.1f}% (normal {d['base'] * 100:.0f}%) - "
            f"{esc('; '.join(d['reasons']))}</li>" for n, d in state["sleeves"].items())
        off = "" if SC.SLEEVES_ENABLED else " - sleeves are switched OFF, so these are not in use yet"
        alloc = f"<h3>This month's strategy shares (decided {esc(str(state.get('date')))}){off}</h3><ul>{items}</ul>"
    head = (f"<p>Account value {_money(totals['equity'])}. Invested (long + short, not counting the IWM hedge) "
            f"{_money(totals['invested'])} = "
            f"{_pct(totals['invested_pct'])} of the account (limit {_pct(totals['cap_pct'])}).</p>")
    return head + table + alloc


def capital_page(rows, totals, state=None):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Capital by strategy</title><style>
body{{background:#0f1115;color:#e5e7eb;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px 16px 48px}}
h1{{font-size:22px;margin:0 0 4px}}h3{{font-size:15px;margin:22px 0 6px}}.sub{{color:#9ca3af;margin-bottom:12px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #262a33;white-space:nowrap}}th{{color:#9ca3af;font-weight:500}}
li{{margin:3px 0}}
</style></head><body><h1>Capital by strategy</h1>
<div class="sub">{html.escape(datetime.now().strftime('%Y-%m-%d %H:%M'))} | paper account | one IBKR account, split into strategies</div>
{capital_html(rows, totals, state)}
</body></html>"""


def report(adapter, today_iso, save=False, compute=True):
    """Reads the account and builds the capital table. Returns (rows, totals, state).
    compute=False skips the monthly replay and uses the saved decision (or the normal shares)."""
    import sleeve_engine
    equity = adapter.equity()
    if not equity:
        raise SystemExit("Could not read the account value from IBKR.")
    positions = adapter.all_positions() if hasattr(adapter, "all_positions") else adapter.positions()
    ledger = sleeve_engine.Ledger()
    if compute:
        state, allocations = current(today_iso, save=save)
    else:
        state = load_state()
        if not (state and state.get("month") == today_iso[:7] and state.get("base") == base_allocations()
                and state.get("settings") == settings_key()):
            state = None
        allocations = {n: d["alloc"] for n, d in state["sleeves"].items()} if state else base_allocations()
    rows, totals = capital_rows(equity, positions, ledger.positions, allocations)
    return rows, totals, state
