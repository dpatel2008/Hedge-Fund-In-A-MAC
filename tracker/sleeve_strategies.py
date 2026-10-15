"""
sleeve_strategies.py
The trading rules for each strategy sleeve. Pure math: no broker, no files, no internet.

Every run_* function takes:
    data  : {symbol: {"dates": [YYYY-MM-DD, ...], "close": [price, ...]}}, oldest first, last COMPLETED day last
    held  : {symbol: {"qty": signed shares, "avg_cost": price, "entry_date": YYYY-MM-DD}} for that sleeve
    today : YYYY-MM-DD
    p     : settings (sleeve_config)
and returns {"weights": {symbol: weight}, "notes": [text], "info": [dict]}.
A weight is a share of the sleeve's money. Negative means short. A symbol that is held but missing from
"weights" is closed.
"""

import numpy as np

import sleeve_config as SC

# ---------------------------------------------------------------- the stock and ETF lists

TREND_UNIVERSE = ["SPY", "QQQ", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]

SECTOR_UNIVERSE = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC"]

PAIRS_LIST = [
    ("KO", "PEP"), ("XOM", "CVX"), ("V", "MA"), ("HD", "LOW"), ("JPM", "BAC"), ("GS", "MS"),
    ("PG", "CL"), ("T", "VZ"), ("MRK", "PFE"), ("COP", "EOG"), ("LMT", "NOC"), ("SPGI", "MCO"),
    ("AMT", "CCI"), ("UPS", "FDX"),
]

MEANREV_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "AVGO", "ADBE", "CRM", "ORCL", "CSCO", "TXN",
    "QCOM", "AMD", "COST", "WMT", "LLY", "ABBV", "TMO", "UNH", "JNJ", "MCD", "NKE", "SBUX", "HON",
    "CAT", "DE", "LIN", "NEE", "BKNG",
]

MOMENTUM_UNIVERSE = [
    "INTU", "NOW", "PANW", "ADI", "LRCX", "KLAC", "AMAT", "MU", "SNPS", "CDNS", "FTNT", "ANET", "ADSK",
    "IBM", "ACN", "BRK-B", "BLK", "SCHW", "AXP", "C", "WFC", "USB", "PNC", "TFC", "CB", "PGR",
    "AON", "ICE", "CME", "AMGN", "GILD", "BMY", "ISRG", "SYK", "MDT", "ABT", "DHR", "VRTX", "REGN",
    "ZTS", "BDX", "CVS", "CI", "ELV", "HCA", "TJX", "LULU", "ORLY", "AZO", "ROST", "MAR", "HLT", "CMG",
    "YUM", "KMB", "GIS", "MDLZ", "MO", "PM", "GE", "RTX", "BA", "UNP", "ETN", "ITW", "EMR", "GD", "WM",
    "CSX", "NSC", "SLB", "PSX", "MPC", "VLO", "OXY", "DUK", "SO", "NFLX", "DIS", "CMCSA", "TMUS", "SHW",
    "APD", "ECL", "FCX", "NEM", "PLD", "EQIX", "PSA", "O",
]

UNIVERSE = {
    "trend": TREND_UNIVERSE,
    "sector": SECTOR_UNIVERSE,
    "momentum": MOMENTUM_UNIVERSE,
    "meanrev": MEANREV_UNIVERSE,
    "pairs": sorted({s for pair in PAIRS_LIST for s in pair}),
}

FREQ = {"trend": "daily", "sector": "monthly", "momentum": "monthly", "meanrev": "daily", "pairs": "daily"}

SLEEVE_ORDER = ["trend", "sector", "momentum", "meanrev", "pairs"]

PARTNER = {}
for _a, _b in PAIRS_LIST:
    PARTNER[_a] = _b
    PARTNER[_b] = _a


def all_symbols(extra=()):
    out = set(extra)
    for name, symbols in UNIVERSE.items():
        out.update(symbols)
    return sorted(out)


def universes_overlap():
    """Returns the symbols that appear in more than one sleeve (should be empty)."""
    seen, bad = set(), set()
    for symbols in UNIVERSE.values():
        for s in symbols:
            if s in seen:
                bad.add(s)
            seen.add(s)
    return sorted(bad)


# ---------------------------------------------------------------- small indicators

def _close(data, symbol):
    d = data.get(symbol)
    if not d:
        return None
    close = d.get("close")
    if close is None or len(close) == 0:
        return None
    return np.asarray(close, dtype=float)


def sma(c, n):
    if c is None or len(c) < n:
        return None
    return float(np.mean(c[-n:]))


def realized_vol(c, n=60):
    """Yearly volatility from the last n daily returns."""
    if c is None or len(c) < n + 1:
        return None
    r = np.diff(np.log(c[-(n + 1):]))
    v = float(np.std(r, ddof=1) * np.sqrt(252))
    return v if np.isfinite(v) and v > 0 else None


def rsi(c, n=2):
    """Wilder's RSI of the closing prices."""
    if c is None or len(c) < n + 2:
        return None
    c = np.asarray(c[-120:], dtype=float)
    d = np.diff(c)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    avg_g = float(np.mean(gains[:n]))
    avg_l = float(np.mean(losses[:n]))
    for i in range(n, len(d)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return float(100.0 - 100.0 / (1.0 + avg_g / avg_l))


def month_changed(last_iso, today_iso):
    """True if today is in a later calendar month than the last rebalance (or there was none)."""
    if not last_iso:
        return True
    return str(last_iso)[:7] < str(today_iso)[:7]


def bars_since(data, symbol, since_iso):
    d = data.get(symbol)
    if not d or not since_iso:
        return 0
    return sum(1 for day in d["dates"] if day > since_iso)


def _cap_and_scale(weights, cap):
    out = {s: min(w, cap) for s, w in weights.items()}
    return out


# ---------------------------------------------------------------- trend following (daily)

def run_trend(data, held, today, p=None):
    p = dict(SC.TREND, **(p or {}))
    inv, above, info, notes = {}, {}, [], []
    for s in TREND_UNIVERSE:
        c = _close(data, s)
        if c is None or len(c) < p["sma_days"] + 1:
            notes.append(f"{s}: not enough price history yet")
            continue
        v = realized_vol(c, p["vol_days"])
        if v is None:
            continue
        avg = sma(c, p["sma_days"])
        price = float(c[-1])
        holding = s in held and held[s].get("qty", 0) > 0
        if holding:
            is_up = price > avg * (1 - p["band"])      # stay in until it falls 1% under the average
        else:
            is_up = price > avg * (1 + p["band"])      # get in only when it is 1% over the average
        inv[s] = 1.0 / max(v, 0.03)
        above[s] = is_up
        info.append({"symbol": s, "price": price, "sma": avg, "vol": v, "trend": "UP" if is_up else "DOWN"})
    total = sum(inv.values())
    weights = {}
    if total > 0:
        for s in inv:
            if above[s]:
                weights[s] = min(inv[s] / total, p["max_weight"])
    for row in info:
        row["weight"] = weights.get(row["symbol"], 0.0)
    up = sum(1 for s in above if above[s])
    notes.append(f"{up} of {len(above)} ETFs are in an uptrend; the rest stay in cash")
    return {"weights": weights, "notes": notes, "info": info}


# ---------------------------------------------------------------- sector rotation (monthly)

def run_sector(data, held, today, p=None):
    p = dict(SC.SECTOR, **(p or {}))
    scored, info, notes = [], [], []
    for s in SECTOR_UNIVERSE:
        c = _close(data, s)
        if c is None or len(c) < p["lookback_days"] + 1:
            notes.append(f"{s}: not enough price history yet")
            continue
        score = float(c[-1] / c[-(p["lookback_days"] + 1)] - 1)
        scored.append((score, s))
        info.append({"symbol": s, "price": float(c[-1]), "return_6m": score})
    scored.sort(reverse=True)
    picks = [s for score, s in scored[: p["top_n"]] if score > 0]
    weights = {s: 1.0 / p["top_n"] for s in picks}
    for row in info:
        row["weight"] = weights.get(row["symbol"], 0.0)
    notes.append("strongest sectors with a positive 6-month return: " + (", ".join(picks) if picks else "none, staying in cash"))
    return {"weights": weights, "notes": notes, "info": info}


# ---------------------------------------------------------------- momentum (monthly)

def run_momentum(data, held, today, p=None):
    p = dict(SC.MOMENTUM, **(p or {}))
    need = p["lookback_days"] + 1
    scored, info, notes = [], [], []
    for s in MOMENTUM_UNIVERSE:
        c = _close(data, s)
        if c is None or len(c) < need:
            continue
        past = c[-need]
        recent = c[-(p["skip_days"] + 1)]
        if past <= 0:
            continue
        score = float(recent / past - 1)
        avg = sma(c, p["sma_days"])
        v = realized_vol(c, p["vol_days"])
        if avg is None or v is None:
            continue
        scored.append({"symbol": s, "score": score, "up": float(c[-1]) > avg, "vol": v, "price": float(c[-1])})
    scored.sort(key=lambda r: r["score"], reverse=True)
    longs = [r for r in scored if r["up"]][: p["top_n"]]
    weights = {}
    total = sum(1.0 / r["vol"] for r in longs)
    for r in longs:
        weights[r["symbol"]] = min((1.0 / r["vol"]) / total, p["max_weight"])
    if p["short_n"] > 0:
        shorts = [r for r in reversed(scored) if not r["up"]][: p["short_n"]]
        stotal = sum(1.0 / r["vol"] for r in shorts)
        for r in shorts:
            weights[r["symbol"]] = -p["short_gross"] * (1.0 / r["vol"]) / stotal
    for r in scored[:15]:
        info.append({"symbol": r["symbol"], "price": r["price"], "return_12_1": r["score"],
                     "above_200d": r["up"], "weight": weights.get(r["symbol"], 0.0)})
    notes.append(f"ranked {len(scored)} stocks; holding the top {len(longs)} that are above their 200-day average")
    return {"weights": weights, "notes": notes, "info": info, "scored": len(scored)}


# ---------------------------------------------------------------- mean reversion (daily)

def run_meanrev(data, held, today, p=None):
    p = dict(SC.MEANREV, **(p or {}))
    keep, info, notes = {}, [], []
    slots = p["max_positions"]
    for s, pos in held.items():
        if pos.get("qty", 0) <= 0:
            continue                                   # this sleeve is long-only; anything else is closed
        c = _close(data, s)
        if c is None:
            keep[s] = True                              # no data today: keep it rather than guess
            continue
        price = float(c[-1])
        reason = None
        avg5 = sma(c, p["exit_sma_days"])
        r = rsi(c, p["rsi_days"])
        if avg5 is not None and price > avg5:
            reason = "bounced above its 5-day average"
        elif r is not None and r > p["exit_rsi"]:
            reason = "RSI recovered"
        elif bars_since(data, s, pos.get("entry_date")) >= p["max_hold_days"]:
            reason = "held the maximum number of days"
        elif pos.get("avg_cost") and price < pos["avg_cost"] * (1 - p["stop_pct"]):
            reason = "stop loss"
        if reason:
            notes.append(f"sell {s}: {reason}")
        else:
            keep[s] = True
    candidates = []
    for s in MEANREV_UNIVERSE:
        if s in keep or s in held:
            continue
        c = _close(data, s)
        if c is None or len(c) < p["sma_days"] + 1:
            continue
        r = rsi(c, p["rsi_days"])
        avg200 = sma(c, p["sma_days"])
        if r is None or avg200 is None:
            continue
        if float(c[-1]) > avg200 and r < p["entry_rsi"]:
            candidates.append((r, s, float(c[-1])))
        if r < 25:
            info.append({"symbol": s, "price": float(c[-1]), "rsi2": r})
    candidates.sort()
    free = max(0, slots - len(keep))
    for r, s, price in candidates[:free]:
        keep[s] = True
        notes.append(f"buy {s}: sharp drop (RSI {r:.1f}) inside an uptrend")
    weights = {s: 1.0 / slots for s in keep}
    if not notes:
        notes.append("no sharp drops to buy and nothing to sell")
    return {"weights": weights, "notes": notes, "info": info}


# ---------------------------------------------------------------- pairs trading (daily)

def pair_stats(ca, cb, p):
    """Returns (z, beta, corr, phi) for the spread between two price series, or None."""
    n = p["lookback_days"]
    if ca is None or cb is None or len(ca) < n + 1 or len(cb) < n + 1:
        return None
    la = np.log(ca[-n:])
    lb = np.log(cb[-n:])
    beta = float(np.polyfit(lb, la, 1)[0])
    spread = la - beta * lb
    tail = spread[-p["z_days"]:]
    sd = float(np.std(tail, ddof=1))
    if sd <= 0 or not np.isfinite(sd):
        return None
    z = float((spread[-1] - np.mean(tail)) / sd)
    ra = np.diff(la)
    rb = np.diff(lb)
    corr = float(np.corrcoef(ra, rb)[0, 1])
    x, y = spread[:-1], spread[1:]
    vx = float(np.var(x))
    phi = float(np.cov(x, y, ddof=0)[0, 1] / vx) if vx > 0 else 1.0
    return z, beta, corr, phi


def _close_pair(data, a, b):
    """Closing prices of two symbols on the dates both have, so the two series line up day by day."""
    da, db = data.get(a), data.get(b)
    if not da or not db:
        return None, None
    mb = dict(zip(db["dates"], db["close"]))
    common = [(float(c), float(mb[d])) for d, c in zip(da["dates"], da["close"]) if d in mb]
    if not common:
        return None, None
    return np.array([x for x, _ in common]), np.array([y for _, y in common])


def run_pairs(data, held, today, p=None):
    p = dict(SC.PAIRS, **(p or {}))
    leg = 1.0 / (2 * p["max_pairs"])
    weights, info, notes = {}, [], []
    open_pairs = []
    entries = []
    handled = set()
    for a, b in PAIRS_LIST:
        ca, cb = _close_pair(data, a, b)
        stats = pair_stats(ca, cb, p)
        in_pair = (a in held and b in held and held[a]["qty"] * held[b]["qty"] < 0)
        row = {"pair": f"{a}/{b}", "z": None, "corr": None, "phi": None, "status": "no data"}
        if in_pair:
            handled.update((a, b))
            side = 1 if held[a]["qty"] > 0 else -1
            if stats is None:
                open_pairs.append((a, b, side))
                row["status"] = "open (no data today, holding)"
            else:
                z = stats[0]
                row.update({"z": z, "corr": stats[2], "phi": stats[3]})
                age = bars_since(data, a, held[a].get("entry_date"))
                reason = None
                if abs(z) < p["exit_z"]:
                    reason = "the gap closed"
                elif side * z > 0:
                    reason = "the gap flipped sides"
                elif abs(z) > p["stop_z"]:
                    reason = "stop: the gap kept widening"
                elif age >= p["max_hold_days"]:
                    reason = "held the maximum number of days"
                if reason:
                    row["status"] = f"closing: {reason}"
                    notes.append(f"close {a}/{b}: {reason}")
                else:
                    open_pairs.append((a, b, side))
                    row["status"] = "open"
        elif stats is not None:
            z, beta, corr, phi = stats
            row.update({"z": z, "corr": corr, "phi": phi})
            ok = corr >= p["min_corr"] and phi <= p["max_phi"]
            if not ok:
                row["status"] = "skipped: not moving together or not mean-reverting"
            elif p["entry_z"] <= abs(z) <= p["max_entry_z"]:
                row["status"] = "ENTRY candidate"
                entries.append((abs(z), a, b, -1 if z > 0 else 1, z))
            else:
                row["status"] = "waiting"
        info.append(row)
    free = max(0, p["max_pairs"] - len(open_pairs))
    entries.sort(reverse=True)
    for _, a, b, side, z in entries[:free]:
        open_pairs.append((a, b, side))
        notes.append(f"open {a}/{b}: gap z={z:+.1f}, {'long ' + a + ' short ' + b if side > 0 else 'short ' + a + ' long ' + b}")
    for a, b, side in open_pairs:
        weights[a] = side * leg
        weights[b] = -side * leg
    for s, pos in held.items():
        if s not in weights and s not in handled and s in PARTNER:
            notes.append(f"close {s}: its partner leg is not open")
    if not notes:
        notes.append("no pairs to open or close")
    return {"weights": weights, "notes": notes, "info": info}


RUNNERS = {
    "trend": run_trend, "sector": run_sector, "momentum": run_momentum,
    "meanrev": run_meanrev, "pairs": run_pairs,
}
