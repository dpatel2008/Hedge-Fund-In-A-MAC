"""
lab_statarb.py
Statistical arbitrage tests for the Signal Lab. It only measures; it never places orders.

Both methods use every US common stock that costs $5 or more and trades at least $10 million a day:
  1. "Pairs", the research paper's method (Gatev, Goetzmann and Rouwenhorst, 2006):
       * every month, look back 12 months and find the 20 pairs of stocks whose prices moved most closely together
       * for the next 6 months, when a pair drifts apart by more than 2 times its normal gap, buy the one that fell
         behind and short the one that ran ahead, $1 each
       * close both when their prices cross back, or when the 6 months are up
  2. "Stock vs its sector", the version big funds use today (Avellaneda and Lee, 2010):
       * each stock is matched with the sector fund it moves with most (XLK for tech, XLE for oil, ...)
       * every day, measure how far each stock has drifted from its sector over the last 60 days, in units of its
         usual drift (the "s-score")
       * buy a stock that fell unusually far behind its sector and short the sector fund against it; short a stock
         that ran unusually far ahead and buy the sector fund against it; close when the gap has mostly closed
  Plus the video's examples on their own, with the paper's rules: Coca-Cola vs Pepsi and Home Depot vs Lowe's.

Every trade opens and closes at the next morning's open after the signal (the first time a daily system can act),
pays trading costs on every leg (0.05% to 1% each way, by how much the stock trades) and 3% a year to borrow the
shares that are shorted. Prices are not adjusted for dividends.
"""

import math
import re
import warnings

import numpy as np

from lab_core import SHORT_BORROW_PER_YEAR, log, read_csv
from lab_prices import TYPES_FILE
from lab_study import side_cost

FORMATION_DAYS = 252          # 12 months to find the pairs
TRADING_DAYS = 126            # then 6 months to trade them
STEP_DAYS = 21                # a new set of pairs every month (the sets overlap, as in the paper)
TOP_PAIRS = 20
OPEN_GAP = 2.0                # open when the gap is more than 2 times its normal size
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 10e6
MAX_STOCKS = 1500             # the most heavily traded ones, if more qualify
COPY_CORRELATION = 0.95       # two share classes of one company (or near-copies) are not a real pair

SECTOR_FUNDS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
WINDOW = 60                   # days used to measure each stock's drift from its sector
S_OPEN = 1.25                 # open when the s-score is beyond +/-1.25
S_CLOSE_LONG = -0.50          # close a buy when the s-score is back above -0.50
S_CLOSE_SHORT = 0.75          # close a short when the s-score is back below +0.75
MAX_AR = 0.967                # the gap must close within about 30 trading days to count as mean-reverting
MAX_BETA = 2.0

MIN_ROUND_TRIPS = 30
MIN_DAYS = 60
EXAMPLES = (("KO", "PEP", "Coca-Cola vs Pepsi (the video's example)"),
            ("HD", "LOW", "Home Depot vs Lowe's (the video's example)"))
_PLAIN = re.compile(r"^[A-Z]{1,5}$")


# ---------------------------------------------------------------- which columns are common stocks

def load_types():
    """{ticker: type} from the price service's list (CS = common stock), or None if it has not been downloaded."""
    rows = read_csv(TYPES_FILE)
    out = {r["ticker"]: r.get("type") or "" for r in rows if r.get("ticker")}
    return out or None


def common_stock_mask(tickers, types):
    """True for common stocks. A ticker missing from the list (it stopped trading before the list was made) counts
    if it is a plain 1-5 letter symbol, so companies that went bust or were bought out stay in the test."""
    keep = np.zeros(len(tickers), dtype=bool)
    for j, t in enumerate(tickers):
        kind = types.get(t)
        keep[j] = kind == "CS" or (kind is None and bool(_PLAIN.match(t or "")))
    return keep


def liquid(M, a, b, mask):
    """Columns that traded every day from row a to row b-1, cost $5+ at the end and traded $10M+ a day (median)."""
    C = M.P.C[a:b]
    with np.errstate(invalid="ignore"):
        full = mask & np.isfinite(C).all(axis=0) & (C > 0).all(axis=0)
    cols = np.where(full)[0]
    if len(cols) == 0:
        return cols
    sub_c = C[:, cols].astype(np.float64)
    sub_v = np.nan_to_num(M.P.V[a:b, cols].astype(np.float64), nan=0.0)
    dv = np.median(sub_c * sub_v, axis=0)
    ok = (sub_c[-1] >= MIN_PRICE) & (dv >= MIN_DOLLAR_VOLUME)
    cols, dv = cols[ok], dv[ok]
    if len(cols) > MAX_STOCKS:
        keep = np.argsort(-dv)[:MAX_STOCKS]
        cols = cols[keep]
    return np.sort(cols)


def _cost(M, d, j):
    price = M.P.O[d, j]
    if not (price > 0):
        price = M.CF[d, j]
    dv = M.AVGV[d, j] * price
    return float(side_cost(dv if np.isfinite(dv) else 0.0))


def _price(arr, d, j, fallback):
    p = float(arr[d, j])
    return p if math.isfinite(p) and p > 0 else fallback


# ---------------------------------------------------------------- 1. the paper's pairs

def find_pairs(M, s1, mask, top=TOP_PAIRS):
    """The `top` closest pairs over the 12 months before row s1: [(col_a, col_b, normal_gap, distance)]."""
    a = s1 - FORMATION_DAYS
    cols = liquid(M, a, s1, mask)
    n = len(cols)
    if n < 2:
        return [], n
    C = M.P.C[a:s1, cols].astype(np.float64)
    P = C / C[0]
    R = np.diff(np.log(C), axis=0)
    sd = R.std(axis=0)
    live = sd > 0
    Z = np.zeros_like(R)
    Z[:, live] = (R[:, live] - R[:, live].mean(axis=0)) / sd[live]
    corr = Z.T @ Z / len(R)
    G = P.T @ P
    sq = np.diag(G)
    ssd = sq[:, None] + sq[None, :] - 2.0 * G
    bad = np.tril(np.ones((n, n), dtype=bool)) | (corr > COPY_CORRELATION) | ~live[:, None] | ~live[None, :]
    ssd[bad] = np.inf
    k = min(top, int(np.isfinite(ssd).sum()))
    if k == 0:
        return [], n
    flat = np.argpartition(ssd.ravel(), k - 1)[:k]
    flat = flat[np.argsort(ssd.ravel()[flat])]
    out = []
    for f in flat:
        i, j = divmod(int(f), n)
        gap = float(np.std(P[:, i] - P[:, j]))
        if gap > 0:
            out.append((int(cols[i]), int(cols[j]), gap, float(ssd[i, j])))
    return out, n


def trade_pair(M, a_col, b_col, gap, s1, end, label=""):
    """Trades one pair from row s1 to row end-1 with the paper's rules. Returns (pnl, costs, trades): pnl and costs
    are per-day arrays for $1 long + $1 short, trades is a list of finished round trips."""
    CF, O = M.CF, M.P.O
    n = end - s1
    pnl = np.zeros(n)
    costs = np.zeros(n)
    trades = []
    base_a, base_b = float(CF[s1 - 1, a_col]), float(CF[s1 - 1, b_col])
    if not (base_a > 0 and base_b > 0):
        return pnl, costs, trades
    pos = None              # {"long", "short", "nl", "ns", "start", "sign", "pnl", "cost"}
    action = None           # "open+1" / "open-1" / "close" decided at the previous close
    for k in range(n):
        d = s1 + k
        if action == "close" and pos is not None:
            pl, ps = _price(O, d, pos["long"], float(CF[d - 1, pos["long"]])), _price(O, d, pos["short"], float(CF[d - 1, pos["short"]]))
            day = pos["nl"] * (pl - CF[d - 1, pos["long"]]) - pos["ns"] * (ps - CF[d - 1, pos["short"]])
            fee = _cost(M, d, pos["long"]) * pos["nl"] * pl + _cost(M, d, pos["short"]) * pos["ns"] * ps
            pnl[k] += day
            costs[k] += fee
            pos["pnl"] += day
            pos["cost"] += fee
            trades.append(_finish(M, pos, d, "the gap closed" if pos.get("why") is None else pos["why"], label))
            pos = None
        elif action and action.startswith("open") and pos is None:
            sign = 1 if action == "open+1" else -1          # +1: buy a, short b
            long_c, short_c = (a_col, b_col) if sign > 0 else (b_col, a_col)
            ol, os_ = float(O[d, long_c]), float(O[d, short_c])
            if ol > 0 and os_ > 0 and math.isfinite(ol) and math.isfinite(os_):
                pos = {"long": long_c, "short": short_c, "nl": 1.0 / ol, "ns": 1.0 / os_, "start": d, "sign": sign,
                       "pnl": 0.0, "cost": 0.0, "why": None}
                fee = _cost(M, d, long_c) + _cost(M, d, short_c)
                day = pos["nl"] * (CF[d, long_c] - ol) - pos["ns"] * (CF[d, short_c] - os_)
                pnl[k] += day
                costs[k] += fee
                pos["pnl"] += day
                pos["cost"] += fee
        elif pos is not None:
            day = pos["nl"] * (CF[d, pos["long"]] - CF[d - 1, pos["long"]]) - pos["ns"] * (CF[d, pos["short"]] - CF[d - 1, pos["short"]])
            fee = SHORT_BORROW_PER_YEAR / 252 * pos["ns"] * CF[d - 1, pos["short"]]
            pnl[k] += day
            costs[k] += fee
            pos["pnl"] += day
            pos["cost"] += fee
        action = None
        if k == n - 1:
            if pos is not None:                              # the 6 months are up (or the data ends): close at the close
                fee = _cost(M, d, pos["long"]) * pos["nl"] * CF[d, pos["long"]] + \
                      _cost(M, d, pos["short"]) * pos["ns"] * CF[d, pos["short"]]
                costs[k] += fee
                pos["cost"] += fee
                trades.append(_finish(M, pos, d, "time ran out", label))
            break
        spread = CF[d, a_col] / base_a - CF[d, b_col] / base_b
        if pos is None:
            if k < n - 2 and abs(spread) > OPEN_GAP * gap:      # no new trade on the period's last day
                action = "open-1" if spread > 0 else "open+1"   # a ran ahead: short a, buy b
        else:
            ahead = pos["short"] == a_col                    # we are short a, so a was ahead
            stopped = M.last_valid[pos["long"]] <= d or M.last_valid[pos["short"]] <= d
            if stopped:
                pos["why"] = "a stock stopped trading"
                action = "close"
            elif (ahead and spread <= 0) or (not ahead and spread >= 0):
                action = "close"
    return pnl, costs, trades


def _finish(M, pos, d, why, label):
    days = M.P.days
    return {"method": "pairs", "label": label, "long": M.P.tickers[pos["long"]], "short": M.P.tickers[pos["short"]],
            "opened": days[pos["start"]].isoformat(), "closed": days[d].isoformat(), "days": int(d - pos["start"] + 1),
            "return_before_costs": round(pos["pnl"], 6), "costs": round(pos["cost"], 6),
            "return_after_costs": round(pos["pnl"] - pos["cost"], 6), "why": why}


def run_pairs(M, mask, forced=None, label=""):
    """The paper's method from row FORMATION_DAYS to the end. forced=(col_a, col_b) trades only that pair (the
    video's examples). Returns (start row, gross daily returns, net daily returns, trades, latest pairs, universe)."""
    nd = M.nd
    start = FORMATION_DAYS
    if nd - start < MIN_DAYS:
        return None
    gross = np.zeros(nd - start)
    net = np.zeros(nd - start)
    active = np.zeros(nd - start)
    trades, latest, sizes = [], [], []
    for s1 in range(start, nd, STEP_DAYS):
        end = min(s1 + TRADING_DAYS, nd)
        if forced:
            a, b = forced
            a_ok = np.isfinite(M.P.C[s1 - FORMATION_DAYS:s1, a]).all() and np.isfinite(M.P.C[s1 - FORMATION_DAYS:s1, b]).all()
            if not a_ok:
                continue
            Pa = M.P.C[s1 - FORMATION_DAYS:s1, a].astype(np.float64)
            Pb = M.P.C[s1 - FORMATION_DAYS:s1, b].astype(np.float64)
            pairs = [(a, b, float(np.std(Pa / Pa[0] - Pb / Pb[0])), 0.0)]
        else:
            pairs, size = find_pairs(M, s1, mask)
            sizes.append(size)
            if not pairs:
                continue
            latest = pairs
        book_gross = np.zeros(end - s1)
        book_cost = np.zeros(end - s1)
        for a, b, gap, _ in pairs:
            p, c, t = trade_pair(M, a, b, gap, s1, end, label)
            book_gross += p
            book_cost += c
            trades += t
        count = len(pairs)                                   # money set aside: $1 per pair, traded or not
        gross[s1 - start:end - start] += book_gross / count
        net[s1 - start:end - start] += (book_gross - book_cost) / count
        active[s1 - start:end - start] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        gross = np.where(active > 0, gross / np.maximum(active, 1), 0.0)
        net = np.where(active > 0, net / np.maximum(active, 1), 0.0)
    pairs_now = [{"a": M.P.tickers[a], "b": M.P.tickers[b], "normal_gap": round(g, 4)} for a, b, g, _ in latest]
    return start, gross, net, trades, pairs_now, (int(np.median(sizes)) if sizes else 0)


# ---------------------------------------------------------------- 2. stock vs its sector

def s_scores(M, d, cols, fund_of):
    """Each stock's s-score at the close of row d (NaN when its gap does not mean-revert fast enough), and its beta
    to its sector fund."""
    CF = M.CF
    rows = slice(d - WINDOW, d + 1)
    X = np.diff(np.log(CF[rows, cols].astype(np.float64)), axis=0)
    F = np.diff(np.log(CF[rows, fund_of].astype(np.float64)), axis=0)
    ok = np.isfinite(X).all(axis=0) & np.isfinite(F).all(axis=0)
    X = np.nan_to_num(X)
    F = np.nan_to_num(F)
    fm, xm = F.mean(axis=0), X.mean(axis=0)
    var_f = ((F - fm) ** 2).mean(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = np.where(var_f > 0, ((F - fm) * (X - xm)).mean(axis=0) / var_f, 0.0)
        alpha = xm - beta * fm
        resid = X - alpha - beta * F
        cum = np.cumsum(resid, axis=0)
        x0, x1 = cum[:-1], cum[1:]
        m0, m1 = x0.mean(axis=0), x1.mean(axis=0)
        v0 = ((x0 - m0) ** 2).mean(axis=0)
        b = np.where(v0 > 0, ((x0 - m0) * (x1 - m1)).mean(axis=0) / v0, np.nan)
        a = m1 - b * m0
        eps = x1 - a - b * x0
        var_eps = eps.var(axis=0)
        good = ok & (b > 0) & (b < MAX_AR) & (var_eps > 0)
        mean_level = np.where(good, a / (1 - b), np.nan)
        sigma_eq = np.where(good, np.sqrt(var_eps / (1 - b * b)), np.nan)
        center = np.nanmean(mean_level) if np.isfinite(mean_level).any() else 0.0
        s = np.where(good, -(mean_level - center) / sigma_eq, np.nan)
    return s, np.clip(beta, 0.0, MAX_BETA)


def assign_funds(M, a, b, cols, fund_cols):
    """For each stock, the sector fund whose daily moves matched its own most closely from row a to row b-1."""
    X = np.diff(np.log(M.CF[a:b, cols].astype(np.float64)), axis=0)
    F = np.diff(np.log(M.CF[a:b, fund_cols].astype(np.float64)), axis=0)
    X = np.nan_to_num(X)
    F = np.nan_to_num(F)
    with np.errstate(invalid="ignore", divide="ignore"):
        Xz = (X - X.mean(axis=0)) / np.where(X.std(axis=0) > 0, X.std(axis=0), 1.0)
        Fz = (F - F.mean(axis=0)) / np.where(F.std(axis=0) > 0, F.std(axis=0), 1.0)
    corr = Xz.T @ Fz / len(X)
    return fund_cols[np.argmax(corr, axis=1)]


def run_sector(M, mask):
    """The sector version from row FORMATION_DAYS to the end. Returns (start, gross, net, trades, info) or None."""
    P, CF, O, nd = M.P, M.CF, M.P.O, M.nd
    funds = [P.col[f] for f in SECTOR_FUNDS if f in P.col]
    if len(funds) < 6:
        return None
    fund_cols = np.array(funds)
    start = FORMATION_DAYS
    if nd - start < MIN_DAYS:
        return None
    stock_mask = mask.copy()
    stock_mask[fund_cols] = False
    n = nd - start
    gross, cost_arr, used = np.zeros(n), np.zeros(n), np.zeros(n)
    positions, trades = {}, []
    plan = {}                       # col -> ("open", sign, fund, beta) or ("close",)
    cols, fund_of = np.array([], dtype=int), np.array([], dtype=int)
    for k in range(n):
        d = start + k
        if k % STEP_DAYS == 0:
            cols = liquid(M, d - FORMATION_DAYS, d, stock_mask)
            fund_of = assign_funds(M, d - FORMATION_DAYS, d, cols, fund_cols) if len(cols) else np.array([], dtype=int)
        # 1. this morning's trades and the day's results
        for j, p in list(positions.items()):
            if plan.get(j, ("",))[0] == "close":
                sl = _price(O, d, j, float(CF[d - 1, j]))
                hf = _price(O, d, p["fund"], float(CF[d - 1, p["fund"]]))
                day = p["sign"] * (p["ns"] * (sl - CF[d - 1, j]) - p["nf"] * (hf - CF[d - 1, p["fund"]]))
                fee = _cost(M, d, j) * p["ns"] * sl + _cost(M, d, p["fund"]) * p["nf"] * hf
                gross[k] += day
                cost_arr[k] += fee
                p["pnl"] += day
                p["cost"] += fee
                used[k] += 1
                trades.append(_finish_sector(M, j, p, d))
                del positions[j]
            else:
                day = p["sign"] * (p["ns"] * (CF[d, j] - CF[d - 1, j]) - p["nf"] * (CF[d, p["fund"]] - CF[d - 1, p["fund"]]))
                short_value = p["ns"] * CF[d - 1, j] if p["sign"] < 0 else p["nf"] * CF[d - 1, p["fund"]]
                fee = SHORT_BORROW_PER_YEAR / 252 * short_value
                gross[k] += day
                cost_arr[k] += fee
                p["pnl"] += day
                p["cost"] += fee
                used[k] += 1
        for j, step in plan.items():
            if step[0] != "open" or j in positions:
                continue
            _, sign, fund, beta = step
            so, fo = float(O[d, j]), float(O[d, fund])
            if not (so > 0 and fo > 0 and math.isfinite(so) and math.isfinite(fo)):
                continue
            p = {"sign": sign, "fund": fund, "beta": beta, "ns": 1.0 / so, "nf": beta / fo, "start": d,
                 "pnl": 0.0, "cost": 0.0}
            day = sign * (p["ns"] * (CF[d, j] - so) - p["nf"] * (CF[d, fund] - fo))
            fee = _cost(M, d, j) + _cost(M, d, fund) * beta
            gross[k] += day
            cost_arr[k] += fee
            p["pnl"] += day
            p["cost"] += fee
            used[k] += 1
            positions[j] = p
        plan = {}
        if d >= nd - 1:
            for j, p in positions.items():                   # the data ends: close at today's close
                fee = _cost(M, d, j) * p["ns"] * CF[d, j] + _cost(M, d, p["fund"]) * p["nf"] * CF[d, p["fund"]]
                cost_arr[k] += fee
                p["cost"] += fee
                trades.append(_finish_sector(M, j, p, d, "data ended"))
            break
        # 2. tonight's decisions for tomorrow's open
        watch = np.array(sorted(set(cols.tolist()) | set(positions)), dtype=int)
        if len(watch) == 0:
            continue
        fund_map = dict(zip(cols.tolist(), fund_of.tolist()))
        for j, p in positions.items():
            fund_map[j] = p["fund"]
        f_cols = np.array([fund_map[j] for j in watch], dtype=int)
        s, beta = s_scores(M, d, watch, f_cols)
        tradable = set(cols.tolist())
        for idx, j in enumerate(watch.tolist()):
            score = s[idx]
            if j in positions:
                p = positions[j]
                if M.last_valid[j] <= d:
                    plan[j] = ("close",)
                elif math.isfinite(score) and ((p["sign"] > 0 and score > S_CLOSE_LONG) or (p["sign"] < 0 and score < S_CLOSE_SHORT)):
                    plan[j] = ("close",)
                continue
            if j not in tradable or not math.isfinite(score):
                continue
            if score < -S_OPEN:
                plan[j] = ("open", 1, int(f_cols[idx]), float(beta[idx]))
            elif score > S_OPEN:
                plan[j] = ("open", -1, int(f_cols[idx]), float(beta[idx]))
    with np.errstate(invalid="ignore", divide="ignore"):         # return on the money in use that day ($1 per trade)
        g = np.where(used > 0, gross / np.maximum(used, 1), 0.0)
        net = np.where(used > 0, (gross - cost_arr) / np.maximum(used, 1), 0.0)
    average = float(used[used > 0].mean()) if (used > 0).any() else 0.0
    return start, g, net, trades, {"average_positions": round(average, 1)}


def _finish_sector(M, j, p, d, why="the gap closed"):
    days = M.P.days
    return {"method": "sector", "label": "", "long": M.P.tickers[j] if p["sign"] > 0 else M.P.tickers[p["fund"]],
            "short": M.P.tickers[p["fund"]] if p["sign"] > 0 else M.P.tickers[j],
            "opened": days[p["start"]].isoformat(), "closed": days[d].isoformat(), "days": int(d - p["start"] + 1),
            "return_before_costs": round(p["pnl"], 6), "costs": round(p["cost"], 6),
            "return_after_costs": round(p["pnl"] - p["cost"], 6), "why": why}


# ---------------------------------------------------------------- scoring

def perf(M, start, gross, net, trades):
    n = len(net)
    out = {"days": n, "first_day": M.P.days[start].isoformat(), "last_day": M.P.days[start + n - 1].isoformat(),
           "round_trips": len(trades)}
    for name, r in (("before", gross), ("after", net)):
        mean = float(np.mean(r)) if n else float("nan")
        sd = float(np.std(r, ddof=1)) if n > 2 else 0.0
        out[f"yearly_{name}"] = mean * 252
        out[f"sharpe_{name}"] = mean / sd * math.sqrt(252) if sd > 0 else float("nan")
        if name == "after":
            out["t"] = mean / sd * math.sqrt(n) if sd > 0 else float("nan")
            half = n // 2
            out["first_half"] = float(np.mean(r[:half])) * 252 if half else float("nan")
            out["second_half"] = float(np.mean(r[half:])) * 252 if n - half else float("nan")
            v = np.cumprod(1 + r)
            out["worst_drop"] = float((v / np.maximum.accumulate(np.concatenate([[1.0], v]))[1:] - 1).min()) if n else float("nan")
    if trades:
        after = [t["return_after_costs"] for t in trades]
        out["win_rate"] = float(np.mean([x > 0 for x in after]))
        out["avg_trade"] = float(np.mean(after))
        out["avg_days"] = float(np.mean([t["days"] for t in trades]))
    else:
        out["win_rate"] = out["avg_trade"] = out["avg_days"] = float("nan")
    spy = M.spy
    if spy is not None and n > 20:
        s = M.CF[start - 1:start + n, spy].astype(np.float64)
        rs = s[1:] / s[:-1] - 1
        m = np.isfinite(rs)
        if m.sum() > 20 and np.var(rs[m]) > 0:
            out["beta_to_spy"] = float(np.cov(net[m], rs[m])[0, 1] / np.var(rs[m], ddof=1))
    out["verdict"] = verdict(out)
    return out


def verdict(s):
    if s["round_trips"] < MIN_ROUND_TRIPS or s["days"] < MIN_DAYS:
        return "TOO FEW"
    a, t = s["yearly_after"], s["t"]
    if a > 0 and t >= 2 and s["first_half"] > 0 and s["second_half"] > 0:
        return "WORKS"
    if a > 0:
        return "WEAK"
    if t <= -2:
        return "LOSES MONEY"
    return "NO EDGE"


# ---------------------------------------------------------------- the whole test

def study_statarb(M):
    """Returns (results dict, trades list) for the report. Raises nothing for missing data: it says what is missing."""
    out = {"methods": [], "notes": []}
    types = load_types()
    if types is None:
        out["notes"].append("The list of which symbols are common stocks (not funds) has not been downloaded yet, so "
                            "this test waits. The next run gets it.")
        return out, []
    if M.nd < FORMATION_DAYS + MIN_DAYS:
        out["notes"].append(f"Needs at least {FORMATION_DAYS + MIN_DAYS} trading days of prices; "
                            f"{M.nd} are saved so far.")
        return out, []
    mask = common_stock_mask(M.P.tickers, types)
    all_trades = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        log("Stat arb: the paper's pairs method...")
        res = run_pairs(M, mask)
        if res:
            start, gross, net, trades, pairs_now, size = res
            row = perf(M, start, gross, net, trades)
            row.update({"key": "pairs", "name": "Pairs (the research paper's method)",
                        "what": f"the {TOP_PAIRS} closest pairs out of about {size:,} stocks, refreshed monthly"})
            out["methods"].append(row)
            out["pairs_now"] = pairs_now
            out["universe"] = size
            all_trades += trades
        log("Stat arb: stock vs its sector...")
        res = run_sector(M, mask)
        if res:
            start, gross, net, trades, info = res
            row = perf(M, start, gross, net, trades)
            row.update({"key": "sector", "name": "Stock vs its sector (the version funds use today)",
                        "what": f"every liquid stock against its sector fund; about {info['average_positions']:.0f} "
                                f"trades open at a time"})
            out["methods"].append(row)
            all_trades += trades
        else:
            out["notes"].append("The sector funds (XLK, XLE, ...) are missing from the saved prices, so the sector "
                                "version was skipped.")
        for a, b, name in EXAMPLES:
            ca, cb = M.P.col.get(a), M.P.col.get(b)
            if ca is None or cb is None:
                continue
            res = run_pairs(M, mask, forced=(ca, cb), label=f"{a}/{b}")
            if res:
                start, gross, net, trades, _, _ = res
                row = perf(M, start, gross, net, trades)
                row.update({"key": f"{a}/{b}", "name": name, "what": "one pair, the paper's rules"})
                out["methods"].append(row)
                all_trades += trades
    for r in out["methods"]:
        log(f"  {r['name']}: {r['round_trips']} trades, {r['yearly_before'] * 100:+.1f}% a year before costs, "
            f"{r['yearly_after'] * 100:+.1f}% after, Sharpe {r['sharpe_after']:.2f} -> {r['verdict']}")
    return out, all_trades
