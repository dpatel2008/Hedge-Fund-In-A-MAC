"""
analytics.py
Pure math and trading rules. No broker, no files, no internet.
"""

import math
import re
from datetime import datetime

import numpy as np

import config as C

BEARISH = ("DILUTION", "GOING_CONCERN", "INSIDER_SELLING", "LATE_FILING", "NEGATIVE_8K")
BULLISH = ("INSIDER_BUYING", "POSITIVE_8K")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def first_positive(*values):
    for v in values:
        if is_num(v) and v > 0:
            return float(v)
    return None


def clamp(x, low, high):
    return max(low, min(high, x))


def parse_date(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def closes_by_date(bars):
    return {b["date"]: b["close"] for b in bars}


# ---------------------------------------------------------------- signals

def signal_tags(form_types, reasons):
    forms = [f.strip() for f in (form_types or "").split(";") if f.strip()]
    reasons_l = (reasons or "").lower()
    tags = []
    if any(f in ("S-1", "S-3", "424B5") for f in forms):
        tags.append("DILUTION")
    if "going concern" in reasons_l:
        tags.append("GOING_CONCERN")
    if "insiders sold" in reasons_l:
        tags.append("INSIDER_SELLING")
    if "insiders bought" in reasons_l or "executive bought" in reasons_l:
        tags.append("INSIDER_BUYING")
    if "positive 8-k" in reasons_l:
        tags.append("POSITIVE_8K")
    if "negative 8-k" in reasons_l:
        tags.append("NEGATIVE_8K")
    if any(f in ("NT 10-Q", "NT 10-K") for f in forms):
        tags.append("LATE_FILING")
    if any(f in ("SC 13D", "SCHEDULE 13D") for f in forms):
        tags.append("ACTIVIST_13D")
    if "8-K" in forms:
        tags.append("EVENT_8K")
    return tags


SPAC_NAME = re.compile(r"\b(?:acquisition|merger|spac|blank check)\s+(?:corp|corporation|co|company|inc|ltd|limited|"
                       r"holdings|holding|i{1,3}|iv|v|vi{0,3})\b|\bacquisition corp", re.IGNORECASE)   # same as the Signal Lab


def is_spac(company):
    """True for blank-check shells (SPACs), judged by the company name the way the Signal Lab does."""
    return bool(SPAC_NAME.search(company or ""))


def classify_signal(form_types, reasons, company=""):
    """Returns (direction, tags, why_skipped). Direction is SHORT, LONG or SKIP."""
    tags = signal_tags(form_types, reasons)
    bearish = [t for t in tags if t in BEARISH]
    bullish = [t for t in tags if t in BULLISH]
    if bearish and bullish:
        return "SKIP", tags, "conflicting signals (bearish and bullish at the same time)"
    if bearish and "ACTIVIST_13D" in tags:
        return "SKIP", tags, "conflicting signals (bearish filing plus a 13D)"
    if bearish:
        working = list(bearish)
        if getattr(C, "SKIP_SPAC_GOING_CONCERN", False) and "GOING_CONCERN" in working and is_spac(company):
            working.remove("GOING_CONCERN")
            if not working:
                return "SKIP", tags, "SPAC going-concern warning (these lost money in the Signal Lab)"
        alone = set(getattr(C, "SHORT_ONLY_WITH_OTHER_SIGNALS", ()))
        if alone and all(t in alone for t in working):
            return "SKIP", tags, "insider selling alone (lost money in the Signal Lab); needs another bearish signal"
        return "SHORT", tags, ""
    if bullish:
        if C.TRADE_LONGS:
            return "LONG", tags, ""
        return "SKIP", tags, "long trades are turned off"
    if "ACTIVIST_13D" in tags:
        if C.TRADE_13D_LONGS:
            return "LONG", tags, ""
        return "SKIP", tags, "13D-only signals are not traded (the list may show the filer, not the target)"
    return "SKIP", tags, "no directional signal"


# ---------------------------------------------------------------- price stats

def atr(bars, n=14):
    """Average true range. bars are dicts with high, low, close, oldest first."""
    if len(bars) < 2:
        return None
    ranges = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    ranges = ranges[-n:]
    return sum(ranges) / len(ranges) if ranges else None


def avg_volume(bars, n=20):
    volumes = [b["volume"] for b in bars[-n:] if is_num(b["volume"])]
    return sum(volumes) / len(volumes) if volumes else None


def beta_vs(stock_closes, bench_closes, min_returns=None):
    """closes are {date: close}. Returns (beta, number_of_returns)."""
    min_returns = min_returns or C.MIN_RETURNS_FOR_BETA
    dates = sorted(set(stock_closes) & set(bench_closes))
    if len(dates) < min_returns + 1:
        return None, max(0, len(dates) - 1)
    s = np.array([stock_closes[d] for d in dates], dtype=float)
    b = np.array([bench_closes[d] for d in dates], dtype=float)
    s_ret = s[1:] / s[:-1] - 1
    b_ret = b[1:] / b[:-1] - 1
    variance = np.var(b_ret, ddof=1)
    if variance <= 0:
        return None, len(s_ret)
    covariance = np.cov(s_ret, b_ret, ddof=1)[0, 1]
    return float(covariance / variance), len(s_ret)


# ---------------------------------------------------------------- sizing and hedging

def size_position(direction, price, atr_value, equity, avg_shares=None):
    if not (is_num(atr_value) and atr_value > 0):
        atr_value = price * C.DEFAULT_ATR_PCT
    stop_dist = C.STOP_ATR_MULT * atr_value
    target_dist = C.TARGET_ATR_MULT * atr_value
    risk_dollars = equity * C.RISK_PER_TRADE_PCT

    caps = [risk_dollars / stop_dist, equity * C.MAX_POSITION_PCT / price]
    if is_num(avg_shares) and avg_shares > 0:
        caps.append(avg_shares * C.MAX_PCT_OF_DAILY_VOLUME)
    qty = int(math.floor(min(caps)))

    sign = -1 if direction == "SHORT" else 1
    return {
        "qty": qty,
        "stop": price - sign * stop_dist,
        "target": price + sign * target_dist,
        "stop_dist": stop_dist,
        "target_dist": target_dist,
        "risk_dollars": qty * stop_dist,
        "atr": atr_value,
    }


def hedge_target(exposures, hedge_price, max_to_net=None):
    """exposures: dicts with signed_notional (long +, short -) and beta. max_to_net: the hedge is never bigger than
    this many times the net dollars (None = no limit)."""
    net = sum(e["signed_notional"] for e in exposures)
    gross = sum(abs(e["signed_notional"]) for e in exposures)
    net_beta = sum(e["signed_notional"] * e["beta"] for e in exposures)
    hedge_dollars = -net_beta
    if max_to_net is not None:
        limit = max_to_net * abs(net)
        hedge_dollars = max(-limit, min(limit, hedge_dollars))
    shares = int(round(hedge_dollars / hedge_price)) if hedge_price else 0
    return {
        "net_notional": net,
        "gross_notional": gross,
        "net_beta_dollars": net_beta,
        "hedge_dollars": hedge_dollars,
        "hedge_shares": shares,
    }


def m2k_contracts(hedge_dollars, iwm_price):
    """Rough Micro Russell 2000 futures equivalent. One contract is about 5 x index, and the index is about 10 x IWM."""
    if not iwm_price:
        return 0.0
    return hedge_dollars / (50.0 * iwm_price)


# ---------------------------------------------------------------- exits and trade records

def exit_reason(trade, price, today):
    if trade["direction"] == "SHORT":
        if price >= trade["stop"]:
            return "stop loss"
        if price <= trade["target"]:
            return "profit target"
    else:
        if price <= trade["stop"]:
            return "stop loss"
        if price >= trade["target"]:
            return "profit target"
    exit_by = parse_date(trade.get("exit_by", ""))
    if exit_by and today >= exit_by:
        return "time exit"
    return None


def close_record(trade, exit_price, exit_date, reason, qty):
    sign = -1 if trade["direction"] == "SHORT" else 1
    pnl = sign * (exit_price - trade["entry_price"]) * qty
    cost = trade["entry_price"] * qty
    entry = parse_date(trade["entry_date"])
    return {
        "trade_id": trade["trade_id"],
        "symbol": trade["symbol"],
        "company": trade["company"],
        "direction": trade["direction"],
        "tags": list(trade["tags"]),
        "score": trade["score"],
        "qty": qty,
        "entry_date": trade["entry_date"],
        "entry_price": trade["entry_price"],
        "exit_date": exit_date.isoformat(),
        "exit_price": exit_price,
        "exit_reason": reason,
        "pnl": pnl,
        "return_pct": pnl / cost if cost else 0.0,
        "hold_days": (exit_date - entry).days if entry else 0,
    }


# ---------------------------------------------------------------- options

def option_style(score):
    """High-conviction signals get a plain long call or put. Everything else gets a defined-risk spread."""
    if C.OPTION_SINGLE_ENABLED and is_num(score) and score >= C.OPTION_SINGLE_MIN_SCORE:
        return "single"
    return "spread"


def spread_legs(direction, chain):
    """Returns (long_leg, short_leg, right) for the spread that fits the direction, or None.
    SHORT ideas use a bear put spread, LONG ideas use a bull call spread."""
    if not chain:
        return None
    if direction == "SHORT":
        long_leg, short_leg, right = chain.get("atm_put"), chain.get("otm_put"), "P"
    else:
        long_leg, short_leg, right = chain.get("atm_call"), chain.get("otm_call"), "C"
    if long_leg and short_leg and is_num(long_leg.get("mid")) and is_num(short_leg.get("mid")):
        return long_leg, short_leg, right
    return None


def option_legs(direction, chain, style="spread"):
    """Returns (long_leg, short_leg, right). For a single option the short leg is None."""
    if style != "single":
        return spread_legs(direction, chain)
    if not chain:
        return None
    if direction == "SHORT":
        leg, right = chain.get("atm_put"), "P"
    else:
        leg, right = chain.get("atm_call"), "C"
    if leg and is_num(leg.get("mid")):
        return leg, None, right
    return None


def option_ideas(direction, price, chain, style="spread"):
    """chain has atm_call, atm_put, otm_call, otm_put quotes (or None) plus expiry and dte."""
    out = {}
    if not chain:
        return out
    call, put = chain.get("atm_call"), chain.get("atm_put")
    out["opt_expiry"] = chain.get("expiry", "")
    out["opt_dte"] = chain.get("dte", "")
    if call:
        out["opt_atm_strike"] = call.get("strike")
    if call and put and is_num(call.get("mid")) and is_num(put.get("mid")) and price > 0:
        out["implied_move_pct"] = (call["mid"] + put["mid"]) / price
    ivs = [q["iv"] for q in (call, put) if q and is_num(q.get("iv"))]
    if ivs:
        out["opt_atm_iv"] = sum(ivs) / len(ivs)

    legs = option_legs(direction, chain, style)
    if not legs:
        return out
    long_leg, short_leg, right = legs
    if short_leg is None:
        name = "Long put" if right == "P" else "Long call"
        debit = long_leg["mid"]
        if debit > 0:
            out["spread_type"] = name
            out["spread_legs"] = f"Buy {long_leg['strike']:g}{right} exp {chain.get('expiry', '')}"
            out["spread_debit"] = debit
            out["spread_breakeven"] = (
                long_leg["strike"] - debit if right == "P" else long_leg["strike"] + debit
            )
            for greek in ("delta", "gamma", "vega", "theta"):
                value = long_leg.get(greek)
                if is_num(value):
                    out["spread_" + greek] = value
        return out

    name = "Bear put spread" if right == "P" else "Bull call spread"
    debit = long_leg["mid"] - short_leg["mid"]
    width = abs(long_leg["strike"] - short_leg["strike"])
    if debit > 0 and width > debit:
        out["spread_type"] = name
        out["spread_legs"] = (
            f"Buy {long_leg['strike']:g}{right} / Sell {short_leg['strike']:g}{right} "
            f"exp {chain.get('expiry', '')}"
        )
        out["spread_debit"] = debit
        out["spread_max_profit"] = width - debit
        out["spread_breakeven"] = (
            long_leg["strike"] - debit if right == "P" else long_leg["strike"] + debit
        )
        for greek in ("delta", "gamma", "vega", "theta"):
            a, b = long_leg.get(greek), short_leg.get(greek)
            if is_num(a) and is_num(b):
                out["spread_" + greek] = a - b
    return out


def _wide_leg(leg):
    bid, ask, mid = leg.get("bid"), leg.get("ask"), leg.get("mid")
    if is_num(bid) and is_num(ask) and is_num(mid) and bid > 0 and ask > 0 and mid > 0:
        return (ask - bid) / mid > C.OPTION_MAX_LEG_GAP_PCT
    return False


def spread_problem(ideas, long_leg, short_leg):
    """Returns None when the option is fine to trade, otherwise a short reason it was skipped.
    short_leg is None for a plain long call or put."""
    debit, max_profit = ideas.get("spread_debit"), ideas.get("spread_max_profit")
    if short_leg is None:
        if not is_num(debit):
            return "no usable option prices"
        if debit < C.OPTION_SINGLE_MIN_PRICE:
            return f"option costs under ${C.OPTION_SINGLE_MIN_PRICE:.2f} a share"
        if _wide_leg(long_leg):
            return "option prices are too wide (bid and ask far apart)"
        return None
    if not (is_num(debit) and is_num(max_profit)):
        return "no usable spread prices"
    if debit < C.OPTION_MIN_DEBIT:
        return f"spread costs under ${C.OPTION_MIN_DEBIT:.2f} a share"
    if max_profit / debit < C.OPTION_MIN_REWARD_RISK:
        return "best case is smaller than the cost"
    for leg in (long_leg, short_leg):
        if _wide_leg(leg):
            return "option prices are too wide (bid and ask far apart)"
    return None


def size_spread(equity, debit, risk_pct=None):
    """How many to buy so the most we can lose is the option budget (a share of the account)."""
    per_contract = debit * 100.0
    if not (is_num(per_contract) and per_contract > 0):
        return 0
    risk_pct = C.OPTION_RISK_PCT if risk_pct is None else risk_pct
    count = int(math.floor(equity * risk_pct / per_contract))
    return max(0, min(count, C.OPTION_MAX_CONTRACTS))


def option_exit_reason(opt, mid, today, underlying_open):
    """opt is an open option record. mid is its current value per share, or None."""
    expiry = parse_date(opt.get("expiry", ""))
    if expiry and (expiry - today).days <= C.OPTION_CLOSE_DTE:
        return "near expiry"
    if not underlying_open:
        return "stock trade closed"
    if is_num(mid):
        if opt.get("style") == "single":
            if mid >= opt["entry_debit"] * (1 + C.OPTION_SINGLE_TAKE_PROFIT_PCT):
                return "profit target"
            if mid <= opt["entry_debit"] * (1 - C.OPTION_SINGLE_STOP_LOSS_PCT):
                return "stop loss"
            return None
        if mid - opt["entry_debit"] >= C.OPTION_TAKE_PROFIT_PCT * opt["max_profit"]:
            return "profit target"
        if mid <= opt["entry_debit"] * (1 - C.OPTION_STOP_LOSS_PCT):
            return "stop loss"
    return None


def close_option_record(opt, exit_price, exit_date, reason, qty):
    mult = float(opt.get("multiplier") or 100)
    pnl = (exit_price - opt["entry_debit"]) * mult * qty
    cost = opt["entry_debit"] * mult * qty
    entry = parse_date(opt["entry_date"])
    word = "put" if opt["right"] == "P" else "call"
    single = opt.get("style") == "single"
    return {
        "trade_id": opt["trade_id"],
        "symbol": f"{opt['symbol']} long {word}" if single else f"{opt['symbol']} {word} spread",
        "company": opt["company"],
        "direction": f"{opt['direction']} OPTION" if single else f"{opt['direction']} SPREAD",
        "tags": list(opt["tags"]) + ["OPTION_SINGLE" if single else "OPTION_SPREAD"],
        "score": opt["score"],
        "qty": qty,
        "entry_date": opt["entry_date"],
        "entry_price": opt["entry_debit"],
        "exit_date": exit_date.isoformat(),
        "exit_price": exit_price,
        "exit_reason": reason,
        "pnl": pnl,
        "return_pct": pnl / cost if cost else 0.0,
        "hold_days": (exit_date - entry).days if entry else 0,
    }


# ---------------------------------------------------------------- risk

def portfolio_risk(positions, histories):
    """positions: list of (symbol, signed_notional). histories: {symbol: {date: close}}."""
    items = [(s, v) for s, v in positions if v != 0 and len(histories.get(s, {})) >= 10]
    if not items:
        return None
    weights = np.array([v for _, v in items], dtype=float)

    dates = set(histories[items[0][0]])
    for symbol, _ in items[1:]:
        dates &= set(histories[symbol])
    dates = sorted(dates)

    if len(dates) >= 21:
        closes = np.array([[histories[s][d] for s, _ in items] for d in dates], dtype=float)
        returns = closes[1:] / closes[:-1] - 1
        if len(items) == 1:
            variance = float(np.var(returns[:, 0], ddof=1) * weights[0] ** 2)
        else:
            cov = np.cov(returns, rowvar=False, ddof=1)
            variance = float(weights @ cov @ weights)
        method = "historical covariance"
    else:
        vols = []
        for symbol, _ in items:
            series = np.array([histories[symbol][d] for d in sorted(histories[symbol])], dtype=float)
            vols.append(float(np.std(series[1:] / series[:-1] - 1, ddof=1)))
        vols = np.array(vols)
        cov = 0.5 * np.outer(vols, vols)
        np.fill_diagonal(cov, vols ** 2)
        variance = float(weights @ cov @ weights)
        method = "assumed 0.5 correlation"

    vol = math.sqrt(max(variance, 0.0))
    return {
        "daily_vol_dollars": vol,
        "var95": 1.645 * vol,
        "var99": 2.326 * vol,
        "cvar95": 2.063 * vol,
        "method": method,
        "n_positions": len(items),
    }


def stress_tests(exposures, hedge_exposure=None):
    everything = list(exposures) + ([hedge_exposure] if hedge_exposure else [])
    beta_dollars = sum(e["signed_notional"] * e["beta"] for e in everything)
    short_gross = sum(-e["signed_notional"] for e in exposures if e["signed_notional"] < 0)
    long_gross = sum(e["signed_notional"] for e in exposures if e["signed_notional"] > 0)
    return [
        {"scenario": "Market down 10% (beta-adjusted)", "pnl": -0.10 * beta_dollars},
        {"scenario": "Market up 10% (beta-adjusted)", "pnl": 0.10 * beta_dollars},
        {"scenario": "Every short squeezes up 30%", "pnl": -0.30 * short_gross},
        {"scenario": "Every long falls 30%", "pnl": -0.30 * long_gross},
    ]


# ---------------------------------------------------------------- performance

def equity_stats(rows):
    """rows: dicts with numeric net_liq, oldest first."""
    values = [r["net_liq"] for r in rows]
    if len(values) < 2:
        return None
    arr = np.array(values, dtype=float)
    returns = arr[1:] / arr[:-1] - 1
    peak = np.maximum.accumulate(arr)
    max_dd = float(np.min(arr / peak - 1))
    std = float(np.std(returns, ddof=1)) if len(returns) >= 2 else 0.0
    return {
        "days": len(values),
        "total_return": float(arr[-1] / arr[0] - 1),
        "ann_vol": std * math.sqrt(252) if len(returns) >= 2 else None,
        "sharpe": float(np.mean(returns) / std * math.sqrt(252)) if std > 0 and len(returns) >= 5 else None,
        "max_drawdown": max_dd,
    }


def alpha_beta(rows):
    """rows: dicts with numeric net_liq and spy_price. Returns beta, annual alpha and r-squared vs the benchmark."""
    usable = [r for r in rows if is_num(r.get("net_liq")) and is_num(r.get("spy_price"))]
    n = len(usable) - 1
    if n < C.MIN_RETURNS_FOR_BETA:
        return {"n": max(n, 0), "beta": None, "alpha_annual": None, "r2": None}
    p = np.array([r["net_liq"] for r in usable], dtype=float)
    b = np.array([r["spy_price"] for r in usable], dtype=float)
    p_ret = p[1:] / p[:-1] - 1
    b_ret = b[1:] / b[:-1] - 1
    variance = np.var(b_ret, ddof=1)
    if variance <= 0:
        return {"n": n, "beta": None, "alpha_annual": None, "r2": None}
    beta = float(np.cov(p_ret, b_ret, ddof=1)[0, 1] / variance)
    alpha_daily = float(np.mean(p_ret) - beta * np.mean(b_ret))
    corr = float(np.corrcoef(p_ret, b_ret)[0, 1])
    return {"n": n, "beta": beta, "alpha_annual": alpha_daily * 252, "r2": corr ** 2}


def trade_stats(trades):
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "hit_rate": None, "avg_win": None, "avg_loss": None,
                "profit_factor": None, "total_pnl": 0.0, "avg_hold_days": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "n": n,
        "hit_rate": len(wins) / n,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "total_pnl": sum(pnls),
        "avg_hold_days": sum(t["hold_days"] for t in trades) / n,
    }


def stats_by_tag(trades):
    tags = sorted({tag for t in trades for tag in t["tags"]})
    return {tag: trade_stats([t for t in trades if tag in t["tags"]]) for tag in tags}
