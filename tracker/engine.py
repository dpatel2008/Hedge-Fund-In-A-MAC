"""
engine.py
The tracker's brain. Builds the trade plan, places paper trades (stock and option
spreads, long and short), manages exits, rebalances the hedge, and writes the reports. Works with any broker object that
has the same methods as IBBroker.
"""

import csv
import io
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import analytics as A
import config as C
import dashboard
import risk

ET = ZoneInfo("America/New_York")
SIGNAL_TRIES = 3              # download attempts for the signal list before giving up for the day
SIGNAL_RETRY_SECONDS = 10
STALE_SIGNAL_DAYS = 4         # warn in the email when the newest signal is older than this
PLAN_TIME_LIMIT_SECONDS = 600 # checking signals stops after 10 minutes, so the strategy sleeves always get their turn


class RetryLater(Exception):
    """IBKR's data was not ready (for example the positions had not loaded). Nothing was changed, so the next
    scheduled try can safely run again."""

PLAN_FIELDS = [
    "date", "symbol", "company", "score", "direction", "tags", "forms", "reasons", "filed",
    "decision", "decision_reason", "flags", "price", "avg_dollar_volume", "atr_pct", "beta",
    "shortable_level", "shortable_shares", "put_call_volume", "qty", "notional", "pct_of_equity",
    "stop", "target", "stop_dist", "target_dist", "exit_by", "risk_dollars",
    "opt_expiry", "opt_dte", "opt_atm_strike", "opt_atm_iv", "implied_move_pct",
    "opt_style", "spread_type", "spread_legs", "spread_debit", "spread_max_profit", "spread_breakeven",
    "spread_delta", "spread_gamma", "spread_vega", "spread_theta",
    "spread_qty", "spread_cost", "spread_status", "filing_url",
]

CLOSED_FIELDS = [
    "trade_id", "symbol", "company", "direction", "tags", "score", "qty", "entry_date",
    "entry_price", "exit_date", "exit_price", "exit_reason", "pnl", "return_pct", "hold_days",
]


# ---------------------------------------------------------------- small helpers

def today_et():
    return datetime.now(ET).date()


def market_is_open(now=None):
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    try:
        import market_days
        if not market_days.is_trading_day(now.date()):
            return False
        close = market_days.close_minutes(now.date())
    except ImportError:
        close = 16 * 60
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < close


def data_path(name):
    return os.path.join(C.DATA_DIR, name)


def read_csv(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def write_text(path, text):
    """Writes the whole file at once: a crash or power cut leaves either the old file or the new one, never half."""
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_csv(path, rows, fieldnames):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    write_text(path, buf.getvalue())


def _load_json_list(name):
    path = data_path(name)
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except ValueError:
        try:
            shutil.copy(path, path + ".corrupt")
        except OSError:
            pass
        raise SystemExit(f"The tracker's memory file {path} is damaged, so nothing was traded. A copy was saved as "
                         f"{name}.corrupt. Ask for help before deleting it.")
    if not isinstance(data, list):
        raise SystemExit(f"The tracker's memory file {path} has the wrong shape, so nothing was traded. Ask for help.")
    return data


def load_open_trades():
    return _load_json_list("open_trades.json")


def save_open_trades(trades):
    write_text(data_path("open_trades.json"), json.dumps(trades, indent=2))


def load_open_options():
    return _load_json_list("open_options.json")


def save_open_options(options):
    write_text(data_path("open_options.json"), json.dumps(options, indent=2))


def _float(x, default=0.0):
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def load_pending():
    """Orders sent but not yet written down (see execute_plan). {} if none."""
    try:
        with open(data_path("pending_orders.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pending(pending):
    path = data_path("pending_orders.json")
    if pending:
        write_text(path, json.dumps(pending, indent=2))
    elif os.path.exists(path):
        os.remove(path)


def load_closed_trades():
    """Every closed trade so far. A row with a damaged number is read as 0 there, never dropped."""
    trades = []
    for row in read_csv(data_path("closed_trades.csv")):
        trades.append({
            "trade_id": row.get("trade_id") or "", "symbol": row.get("symbol") or "", "company": row.get("company") or "",
            "direction": row.get("direction") or "",
            "tags": [t for t in (row.get("tags") or "").split("; ") if t],
            "score": row.get("score") or "", "qty": int(_float(row.get("qty"))),
            "entry_date": row.get("entry_date") or "", "entry_price": _float(row.get("entry_price")),
            "exit_date": row.get("exit_date") or "", "exit_price": _float(row.get("exit_price")),
            "exit_reason": row.get("exit_reason") or "", "pnl": _float(row.get("pnl")),
            "return_pct": _float(row.get("return_pct")), "hold_days": int(_float(row.get("hold_days"))),
        })
    return trades


def save_closed_trades(trades):
    rows = [dict(t, tags="; ".join(t["tags"])) for t in trades]
    write_csv(data_path("closed_trades.csv"), rows, CLOSED_FIELDS)


def load_signals(local_path=None, tries=None, wait=None):
    """The ranked signal list from GitHub (or a local file). Tries a few times. Raises if it cannot be read."""
    if local_path:
        with open(local_path, newline="") as f:
            return list(csv.DictReader(f))
    tries = SIGNAL_TRIES if tries is None else tries
    wait = SIGNAL_RETRY_SECONDS if wait is None else wait
    problem = None
    for attempt in range(tries):
        try:
            resp = requests.get(C.SIGNALS_URL, timeout=30)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            if not reader.fieldnames or not {"ticker", "score"} <= set(reader.fieldnames):
                raise ValueError("the file does not have the expected columns")
            return list(reader)
        except Exception as e:                       # network blip, GitHub busy, or a broken file
            problem = e
            if attempt + 1 < tries:
                time.sleep(wait)
    raise RuntimeError(f"could not download the signal list ({type(problem).__name__}: {problem})")


def newest_signal_date(signals):
    best = None
    for sig in signals:
        d = A.parse_date(sig.get("date_filed", ""))
        if d and (best is None or d > best):
            best = d
    return best


def _score(sig):
    try:
        return int(float(sig.get("score") or 0))
    except (TypeError, ValueError):
        return 0


class Session:
    """Caches quotes and price history so each symbol is only requested once per run. Also adds up how long IBKR
    takes to answer each kind of request, for the progress lines."""

    def __init__(self, broker):
        self.broker = broker
        self._quotes = {}
        self._bars = {}
        self.seconds = {"prices": 0.0, "history": 0.0, "options": 0.0}

    def quote(self, symbol):
        if symbol not in self._quotes:
            started = time.time()
            self._quotes[symbol] = self.broker.snapshot(symbol)
            self.seconds["prices"] += time.time() - started
        return self._quotes[symbol]

    def price(self, symbol):
        quote = self.quote(symbol)
        return quote.get("price") if quote else None

    def bars(self, symbol):
        if symbol not in self._bars:
            started = time.time()
            self._bars[symbol] = self.broker.history(symbol, C.HISTORY_DAYS) or []
            self.seconds["history"] += time.time() - started
        return self._bars[symbol]


# ---------------------------------------------------------------- the trade plan

def build_plan(session, signals, equity, open_symbols, today, open_spread_count=0, opened_today=None, avoid=None):
    """opened_today: {"LONG": n, "SHORT": n} trades already opened earlier today (a second try the same day counts
    them, so the daily limits are never doubled). avoid: {symbol: reason} names that must not be traded."""
    broker = session.broker
    plan = []
    analyzed = 0
    opened_today = opened_today or {}
    avoid = avoid or {}
    new_by_side = {"LONG": int(opened_today.get("LONG", 0)), "SHORT": int(opened_today.get("SHORT", 0))}
    new_trades = new_by_side["LONG"] + new_by_side["SHORT"]
    side_caps = {"LONG": C.MAX_NEW_LONGS_PER_RUN, "SHORT": C.MAX_NEW_SHORTS_PER_RUN}
    spreads_planned = 0
    min_score = min(C.MIN_SCORE_TO_TRADE, C.MIN_SCORE_TO_TRADE_LONG)
    slots = max(0, C.MAX_OPEN_POSITIONS - len(open_symbols))
    added = 0                                         # new positions in this run (they use up open slots)
    bench_closes = None
    started = time.time()

    for sig in signals:
        score = _score(sig)
        if score < min_score:
            continue
        symbol = (sig.get("ticker") or "").strip().upper()
        row = {
            "date": today.isoformat(), "symbol": symbol, "company": sig.get("company", ""),
            "score": score, "forms": sig.get("form_types", ""), "reasons": sig.get("reasons", ""),
            "filed": sig.get("date_filed", ""), "filing_url": sig.get("sample_filing_url", ""),
            "direction": "", "tags": "", "decision": "SKIP", "decision_reason": "", "flags": "",
        }

        def skip(reason):
            row["decision"] = "SKIP"
            row["decision_reason"] = reason
            plan.append(row)

        if not symbol:
            skip("no ticker symbol")
            continue
        if analyzed >= C.MAX_CANDIDATES_TO_ANALYZE:
            break

        filed = A.parse_date(row["filed"])
        if filed is None or (today - filed).days > C.MAX_SIGNAL_AGE_DAYS:
            skip("signal is too old")
            continue

        direction, tags, why = A.classify_signal(row["forms"], row["reasons"])
        row["direction"] = direction
        row["tags"] = "; ".join(tags)
        if direction == "SHORT" and score < C.MIN_SCORE_TO_TRADE:
            continue
        if direction == "LONG" and score < C.MIN_SCORE_TO_TRADE_LONG:
            continue
        if direction == "SKIP":
            skip(why)
            continue
        if symbol in open_symbols:
            skip("already holding this name")
            continue
        if symbol in avoid:
            skip(avoid[symbol])
            continue
        if new_trades >= C.MAX_NEW_TRADES_PER_RUN or added >= slots:
            skip("position or new-trade limit reached")            # no room: don't spend time asking IBKR about it
            continue
        if new_by_side[direction] >= side_caps[direction]:
            skip(f"all {direction.lower()} slots for this run are used")
            continue
        spent = time.time() - started
        if spent > PLAN_TIME_LIMIT_SECONDS:
            skip("not checked: ran out of time this run (IBKR was slow)")
            continue

        analyzed += 1
        print(f"  checking {symbol} ({analyzed} of up to {C.MAX_CANDIDATES_TO_ANALYZE}, {spent:.0f}s so far)")
        quote = session.quote(symbol)
        if not quote or not quote.get("price"):
            skip("no quote from IBKR (symbol not found or no data)")
            continue
        price = quote["price"]
        row["price"] = round(price, 4)
        if price < C.MIN_PRICE:
            skip(f"price under ${C.MIN_PRICE:g}")
            continue

        flags = []
        bars = session.bars(symbol)
        avg_shares = None
        adv_dollars = None
        if bars:
            raw = A.avg_volume(bars)
            if raw:
                avg_shares = raw * C.HISTORY_VOLUME_MULTIPLIER
                adv_dollars = avg_shares * price
                row["avg_dollar_volume"] = round(adv_dollars)
        else:
            flags.append("NO_HISTORY")
        atr_value = A.atr(bars) if bars else None
        if atr_value:
            row["atr_pct"] = round(atr_value / price, 4)
        if adv_dollars is not None and adv_dollars < C.MIN_AVG_DOLLAR_VOLUME:
            skip(f"too thinly traded (about ${adv_dollars:,.0f} a day)")
            continue

        level = quote.get("shortable_level")
        available = quote.get("shortable_shares")
        row["shortable_level"] = level if level is not None else ""
        row["shortable_shares"] = available if available is not None else ""
        if direction == "SHORT":
            if A.is_num(level) and level < C.MIN_SHORTABLE_LEVEL:
                skip(f"hard to borrow (IBKR shortable level {level:g})")
                continue
            if not A.is_num(level) and not A.is_num(available):
                if not C.ALLOW_UNKNOWN_SHORTABLE:
                    skip("shortability unknown")
                    continue
                flags.append("SHORTABLE_UNKNOWN")

        if bench_closes is None:
            bench_closes = A.closes_by_date(session.bars(C.HEDGE_SYMBOL))
        beta, _ = A.beta_vs(A.closes_by_date(bars), bench_closes) if bars else (None, 0)
        if beta is None:
            beta = C.DEFAULT_BETA
            flags.append("BETA_DEFAULT")
        beta = A.clamp(beta, 0.0, 3.0)
        row["beta"] = round(beta, 2)

        calls, puts = quote.get("call_volume"), quote.get("put_volume")
        if A.is_num(calls) and calls > 0 and A.is_num(puts):
            row["put_call_volume"] = round(puts / calls, 2)

        sized = A.size_position(direction, price, atr_value, equity, avg_shares)
        qty = sized["qty"]
        if direction == "SHORT" and A.is_num(available):
            qty = min(qty, int(available))
        if qty < 1:
            skip("position size rounds to zero")
            continue
        row.update({
            "qty": qty,
            "notional": round(qty * price, 2),
            "pct_of_equity": round(qty * price / equity, 4),
            "stop": round(sized["stop"], 4),
            "target": round(sized["target"], 4),
            "stop_dist": round(sized["stop_dist"], 4),
            "target_dist": round(sized["target_dist"], 4),
            "exit_by": (today + timedelta(days=C.HOLD_CALENDAR_DAYS)).isoformat(),
            "risk_dollars": round(qty * sized["stop_dist"], 2),
        })

        if new_trades >= C.MAX_NEW_TRADES_PER_RUN or added >= slots:
            decision, why_not = "SKIP", "position or new-trade limit reached"
        elif new_by_side[direction] >= side_caps[direction]:
            decision, why_not = "SKIP", f"all {direction.lower()} slots for this run are used"
        else:
            decision, why_not = "TRADE", "passed every check"

        style = A.option_style(score)
        row["opt_style"] = style
        chain = None
        if decision == "TRADE":                       # option prices only for real trades (each look takes a while)
            asked = time.time()
            chain = broker.option_chain(symbol, price)
            session.seconds["options"] += time.time() - asked
            ideas = A.option_ideas(direction, price, chain, style)
            for key, value in ideas.items():
                row[key] = round(value, 4) if isinstance(value, float) else value
            row["_chain"] = chain

        row["flags"] = "; ".join(flags)
        row["decision"] = decision
        row["decision_reason"] = why_not
        if decision == "TRADE":
            new_trades += 1
            added += 1
            new_by_side[direction] += 1
            if plan_spread(row, direction, equity, chain, open_spread_count + spreads_planned):
                spreads_planned += 1
        plan.append(row)
    t = session.seconds
    print(f"  Checked {analyzed} stock(s) in {time.time() - started:.0f}s (IBKR answers: prices {t['prices']:.0f}s, "
          f"history {t['history']:.0f}s, options {t['options']:.0f}s).")
    return plan


def plan_spread(row, direction, equity, chain, spreads_counted):
    """Decides whether a trade also gets an option (a spread or a plain call/put), and how many.
    Returns True if planned."""
    if not C.OPTIONS_ENABLED:
        row["spread_status"] = "options turned off"
        return False
    if spreads_counted >= C.OPTION_MAX_OPEN:
        row["spread_status"] = "too many options open"
        return False
    style = row.get("opt_style", "spread")
    legs = A.option_legs(direction, chain, style)
    if not legs or not row.get("spread_type"):
        row["spread_status"] = "no usable option prices"
        return False
    long_leg, short_leg, _ = legs
    problem = A.spread_problem(row, long_leg, short_leg)
    if problem:
        row["spread_status"] = problem
        return False
    count = A.size_spread(equity, row["spread_debit"])
    if count < 1:
        row["spread_status"] = "too expensive for the option budget"
        return False
    row["spread_qty"] = count
    row["spread_cost"] = round(count * row["spread_debit"] * 100, 2)
    row["spread_status"] = "planned"
    return True


# ---------------------------------------------------------------- orders

def new_trade(row, today, qty, fill):
    sign = -1 if row["direction"] == "SHORT" else 1
    return {
        "trade_id": f"{today.isoformat()}-{row['symbol']}",
        "symbol": row["symbol"], "company": row["company"],
        "direction": row["direction"],
        "tags": [t for t in row["tags"].split("; ") if t],
        "score": row["score"], "forms": row["forms"],
        "qty": qty, "entry_date": today.isoformat(), "entry_price": fill,
        "stop": fill - sign * row["stop_dist"],
        "target": fill + sign * row["target_dist"],
        "exit_by": row["exit_by"], "beta": row["beta"],
    }


def execute_plan(broker, plan, open_trades, open_options, today, events):
    pending = load_pending()
    for row in plan:
        if row["decision"] != "TRADE":
            continue
        action = "SELL" if row["direction"] == "SHORT" else "BUY"
        ref = f"SECW-{row['symbol']}-{today.isoformat()}"
        print(f"  {action} {int(row['qty'])} {row['symbol']} ({row['direction']}, score {row['score']})")
        pending[ref] = {"kind": "stock", "date": today.isoformat(), "qty": int(row["qty"]),
                        "row": {k: row.get(k) for k in ("symbol", "company", "direction", "tags", "score", "forms",
                                                        "stop_dist", "target_dist", "exit_by", "beta", "price")}}
        save_pending(pending)                     # written down BEFORE the order, in case the run stops part way
        result = broker.place_market(row["symbol"], action, int(row["qty"]), ref)
        filled = int(result.get("filled") or 0)
        if filled < 1:
            pending.pop(ref, None)
            save_pending(pending)
            row["decision"] = "NOT FILLED"
            row["decision_reason"] = f"order ended as {result.get('status')}"
            events.append(f"{row['symbol']}: {action} order was not filled ({result.get('status')})")
            continue
        fill = result.get("avg_fill_price") or row["price"]
        open_trades.append(new_trade(row, today, filled, fill))
        save_open_trades(open_trades)
        pending.pop(ref, None)
        save_pending(pending)
        row["decision"] = "FILLED"
        row["decision_reason"] = f"{action} {filled} at {fill:.2f}"
        events.append(f"{action} {filled} {row['symbol']} at {fill:.2f} ({row['direction']}, score {row['score']})")
        if row.get("spread_status") == "planned":
            execute_spread(broker, row, open_options, today, events)


def execute_spread(broker, row, open_options, today, events):
    symbol = row["symbol"]
    chain = row.get("_chain")
    style = row.get("opt_style", "spread")
    legs = A.option_legs(row["direction"], chain, style)
    if not legs:
        row["spread_status"] = "option prices disappeared"
        return
    long_leg, short_leg, right = legs
    if not long_leg.get("conId") or (short_leg is not None and not short_leg.get("conId")):
        row["spread_status"] = "option contracts not found"
        return
    opt = {
        "trade_id": f"{today.isoformat()}-{symbol}-OPTION",
        "stock_trade_id": f"{today.isoformat()}-{symbol}",
        "style": style,
        "symbol": symbol, "underlying": chain.get("underlying", symbol),
        "company": row["company"], "direction": row["direction"],
        "tags": [t for t in row["tags"].split("; ") if t], "score": row["score"],
        "right": right, "expiry": chain["expiry"],
        "long_strike": long_leg["strike"],
        "short_strike": short_leg["strike"] if short_leg else None,
        "long_conid": long_leg["conId"],
        "short_conid": short_leg["conId"] if short_leg else None,
        "multiplier": chain.get("multiplier", "100"), "trading_class": chain.get("trading_class", ""),
        "entry_date": today.isoformat(),
    }
    debit = long_leg["mid"] - (short_leg["mid"] if short_leg else 0.0)
    limit = debit * (1 + C.OPTION_LIMIT_SLIPPAGE)
    if short_leg is not None:
        opt["width"] = abs(long_leg["strike"] - short_leg["strike"])
    ref = f"SECW-OPT-{symbol}-{today.isoformat()}"
    pending = load_pending()
    pending[ref] = {"kind": "option", "date": today.isoformat(), "qty": int(row["spread_qty"]), "limit": limit,
                    "option": dict(opt)}
    save_pending(pending)
    result = broker.place_spread(opt, "BUY", int(row["spread_qty"]), limit, ref)
    filled = int(result.get("filled") or 0)
    pending.pop(ref, None)
    if filled < 1:
        save_pending(pending)
        row["spread_status"] = f"not filled ({result.get('status')})"
        events.append(f"{symbol}: option order was not filled ({result.get('status')})")
        return
    fill = result.get("avg_fill_price") or debit
    if short_leg is not None:
        width = abs(long_leg["strike"] - short_leg["strike"])
        max_profit = max(width - fill, 0.0)
    else:
        max_profit = None
    opt.update({"qty": filled, "entry_debit": fill, "max_profit": max_profit})
    open_options.append(opt)
    save_open_options(open_options)
    save_pending(pending)
    row["spread_status"] = f"BOUGHT {filled} at {fill:.2f}"
    word = "put" if right == "P" else "call"
    if short_leg is None:
        what = f"{long_leg['strike']:g} long {word}"
    else:
        what = f"{long_leg['strike']:g}/{short_leg['strike']:g} {word} spread"
    events.append(
        f"BOUGHT {filled} {symbol} {what} exp {chain['expiry']} at {fill:.2f} "
        f"(most it can lose: ${fill * 100 * filled:,.0f})"
    )


def settle_pending(broker, open_trades, open_options, events):
    """After a run that stopped part way: an order that was sent but never written down may have filled. If the
    account holds it (and no strategy sleeve owns those shares), it is added to the open trades so its stop,
    target and exit date are managed. Returns how many were added."""
    pending = load_pending()
    if not pending:
        return 0
    positions = broker.positions()
    require_connection(broker)
    stocks = {p["symbol"]: p for p in positions if p["sec_type"] == "STK"}
    options = {p.get("con_id"): p for p in positions if p["sec_type"] == "OPT"}
    sleeve_qty = {}
    for held in (risk.sleeve_ledger_positions() or {}).values():
        for symbol, pos in held.items():
            sleeve_qty[symbol] = sleeve_qty.get(symbol, 0) + (pos.get("qty") or 0)
    trade_ids = {t["trade_id"] for t in open_trades}
    option_ids = {o["trade_id"] for o in open_options}
    added = 0
    for ref, rec in sorted(pending.items()):
        try:
            added += _settle_one(rec, stocks, options, sleeve_qty, open_trades, open_options, trade_ids, option_ids, events)
        except Exception as e:                        # a damaged record must never stop the run
            events.append(f"Could not check an earlier unfinished order ({ref}): {e}")
    save_open_trades(open_trades)
    save_open_options(open_options)
    save_pending({})
    return added


def _settle_one(rec, stocks, options, sleeve_qty, open_trades, open_options, trade_ids, option_ids, events):
    if rec.get("kind") == "stock":
        row = rec.get("row") or {}
        symbol = row.get("symbol")
        day = date_of(rec.get("date"))
        if not symbol or day is None:
            return 0
        trade_id = f"{day.isoformat()}-{symbol}"
        if trade_id in trade_ids:
            return 0
        sign = -1 if row.get("direction") == "SHORT" else 1
        pos = stocks.get(symbol)
        mine = sum((t["qty"] if t["direction"] == "LONG" else -t["qty"]) for t in open_trades if t["symbol"] == symbol)
        extra = (pos["qty"] if pos else 0) - sleeve_qty.get(symbol, 0) - mine
        if extra * sign < 1:
            return 0                              # it never filled
        qty = int(min(abs(extra), rec.get("qty") or abs(extra)))
        fill = float((pos.get("avg_cost") if pos else None) or row.get("price"))
        open_trades.append(new_trade(dict(row, tags=row.get("tags") or ""), day, qty, fill))
        trade_ids.add(trade_id)
        events.append(f"Found {qty} {symbol} from an order an earlier try sent but did not write down; "
                      "it is now managed like any other trade")
        return 1
    if rec.get("kind") == "option":
        opt = dict(rec.get("option") or {})
        pos = options.get(opt.get("long_conid"))
        if not opt or opt.get("trade_id") in option_ids or not pos or pos["qty"] < 1:
            return 0
        qty = int(min(pos["qty"], rec.get("qty") or pos["qty"]))
        debit = float(rec.get("limit") or 0.0)
        width = opt.pop("width", None)
        opt.update({"qty": qty, "entry_debit": debit,
                    "max_profit": max(width - debit, 0.0) if width is not None else None})
        open_options.append(opt)
        option_ids.add(opt["trade_id"])
        events.append(f"Found {qty} {opt.get('symbol')} option(s) from an order an earlier try sent but did not "
                      "write down; they are now managed like any other option")
        return 1
    return 0


def date_of(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%d").date()
    except ValueError:
        return None


def still_connected(broker):
    """True while the connection to IB Gateway is up. When it drops, ib_async empties its list of positions, so
    nothing may be decided from positions read after that."""
    fn = getattr(broker, "is_connected", None)
    return True if fn is None else bool(fn())


def require_connection(broker):
    if not still_connected(broker):
        raise RetryLater("Lost the connection to IB Gateway, so nothing more was changed.")


def positions_loaded(broker, open_trades, open_options):
    """False when the tracker has open trades, IBKR reports no positions at all, but IBKR's own account value
    says positions exist. That means the positions have not loaded yet, and every trade would wrongly look
    'closed outside the tracker'."""
    if not open_trades and not open_options:
        return True
    if broker.positions():
        return True
    gross = broker.account_values().get("GrossPositionValue")
    return not gross


def opened_today(open_trades, closed_trades, today):
    """How many stock trades were already opened today, by side, and which names were traded today."""
    day = today.isoformat()
    counts, names = {"LONG": 0, "SHORT": 0}, set()
    for t in list(open_trades) + list(closed_trades):
        if str(t.get("entry_date")) == day and t.get("direction") in counts:
            counts[t["direction"]] += 1
            names.add(t.get("symbol"))
    return counts, names


def names_to_avoid(names_today, account_symbols=()):
    """Symbols the tracker must not open: anything a strategy sleeve owns, anything else the account already holds,
    the hedge and the benchmark."""
    avoid = {C.HEDGE_SYMBOL: "this is the hedge fund the tracker uses", C.BENCHMARK_SYMBOL: "this is the benchmark"}
    ledger = risk.sleeve_ledger_positions()
    for sleeve, held in (ledger or {}).items():
        for symbol in held:
            avoid[symbol] = f"the {sleeve} strategy sleeve owns this name"
    for symbol in names_today:
        avoid.setdefault(symbol, "already traded this name today")
    for symbol in account_symbols:
        avoid.setdefault(symbol, "the account already holds this name (not from a tracker trade)")
    return avoid


def manage_exits(session, open_trades, today):
    broker = session.broker
    held = {p["symbol"]: p for p in broker.positions() if p["sec_type"] == "STK"}
    require_connection(broker)
    still_open, closed_now = [], []
    for trade in open_trades:
        symbol = trade["symbol"]
        price = session.price(symbol)
        position = held.get(symbol)
        if position is None or abs(position["qty"]) < 1:
            exit_price = price or trade["entry_price"]
            closed_now.append(A.close_record(trade, exit_price, today, "closed outside tracker", trade["qty"]))
            continue
        if price is None:
            still_open.append(trade)
            continue
        reason = A.exit_reason(trade, price, today)
        if reason is None:
            still_open.append(trade)
            continue
        action = "BUY" if trade["direction"] == "SHORT" else "SELL"
        result = broker.place_market(symbol, action, int(trade["qty"]), f"SECW-EXIT-{symbol}-{today.isoformat()}")
        filled = int(result.get("filled") or 0)
        if filled < 1:
            still_open.append(trade)
            continue
        exit_price = result.get("avg_fill_price") or price
        closed_now.append(A.close_record(trade, exit_price, today, reason, filled))
        if filled < int(trade["qty"]):
            trade["qty"] = int(trade["qty"]) - filled
            still_open.append(trade)
    return still_open, closed_now


def manage_option_exits(session, open_options, open_trades, today, events):
    broker = session.broker
    open_ids = {t["trade_id"] for t in open_trades}
    held_ids = {p.get("con_id") for p in broker.positions() if p["sec_type"] == "OPT"}
    require_connection(broker)
    still_open, closed_now = [], []
    for opt in open_options:
        quote = broker.spread_quote(opt)
        mid = quote["mid"] if quote else None
        if opt.get("short_conid"):
            label = f"{opt['symbol']} {opt['long_strike']:g}/{opt['short_strike']:g}{opt['right']} spread"
        else:
            label = f"{opt['symbol']} {opt['long_strike']:g}{opt['right']} option"
        if opt["long_conid"] not in held_ids:
            exit_price = mid if mid is not None else opt["entry_debit"]
            closed_now.append(A.close_option_record(opt, exit_price, today, "closed outside tracker", opt["qty"]))
            events.append(f"{label}: no longer in the account, recorded as closed")
            continue
        reason = A.option_exit_reason(opt, mid, today, opt["stock_trade_id"] in open_ids)
        if reason is None:
            still_open.append(opt)
            continue
        limit = mid * (1 - C.OPTION_LIMIT_SLIPPAGE) if mid is not None else 0.01
        result = broker.place_spread(opt, "SELL", int(opt["qty"]), limit, f"SECW-OPTEXIT-{opt['symbol']}-{today.isoformat()}")
        filled = int(result.get("filled") or 0)
        if filled < 1:
            still_open.append(opt)
            events.append(f"{label}: wanted to close ({reason}) but the order was not filled")
            continue
        exit_price = result.get("avg_fill_price") or mid or 0.0
        record = A.close_option_record(opt, exit_price, today, reason, filled)
        closed_now.append(record)
        events.append(f"Closed {label} ({reason}): P&L ${record['pnl']:,.2f}")
        if filled < int(opt["qty"]):
            opt["qty"] = int(opt["qty"]) - filled
            still_open.append(opt)
    return still_open, closed_now


def rebalance_hedge(session, open_trades, today):
    if not C.HEDGE_ENABLED:
        return None
    broker = session.broker
    hedge_price = session.price(C.HEDGE_SYMBOL)
    if not hedge_price:
        return {"status": "no quote for the hedge symbol, skipped"}
    exposures = []
    for trade in open_trades:
        price = session.price(trade["symbol"]) or trade["entry_price"]
        sign = -1 if trade["direction"] == "SHORT" else 1
        exposures.append({"symbol": trade["symbol"], "signed_notional": sign * price * trade["qty"], "beta": trade["beta"]})
    target = A.hedge_target(exposures, hedge_price)
    held = {p["symbol"]: p for p in broker.positions() if p["sec_type"] == "STK"}
    require_connection(broker)
    current = int(held[C.HEDGE_SYMBOL]["qty"]) if C.HEDGE_SYMBOL in held else 0
    delta = target["hedge_shares"] - current
    info = dict(target)
    info.update({
        "symbol": C.HEDGE_SYMBOL, "price": hedge_price, "current_shares": current,
        "m2k_contracts": A.m2k_contracts(target["hedge_dollars"], hedge_price),
        "status": "already balanced", "traded_shares": 0,
    })
    if abs(delta * hedge_price) >= C.MIN_HEDGE_DOLLARS:
        action = "BUY" if delta > 0 else "SELL"
        result = broker.place_market(C.HEDGE_SYMBOL, action, abs(int(delta)), f"SECW-HEDGE-{today.isoformat()}")
        filled = int(result.get("filled") or 0)
        if filled > 0:
            info["traded_shares"] = filled if action == "BUY" else -filled
            info["current_shares"] = current + info["traded_shares"]
            info["status"] = f"{action} {filled} {C.HEDGE_SYMBOL}"
        else:
            info["status"] = f"hedge order ended as {result.get('status')}"
    return info


# ---------------------------------------------------------------- the report

def build_report(session, account_id, today, open_trades, closed_trades, plan, hedge_info, open_options=None, events=None):
    broker = session.broker
    acct = broker.account_values()
    equity = acct.get("NetLiquidation")
    held = {p["symbol"]: p for p in broker.positions() if p["sec_type"] == "STK"}

    option_rows = []
    for opt in open_options or []:
        quote = broker.spread_quote(opt)
        mid = quote["mid"] if quote else None
        mult = float(opt.get("multiplier") or 100)
        qty = int(opt["qty"])
        single = not opt.get("short_conid")
        word = "put" if opt["right"] == "P" else "call"
        if single:
            kind = "Long put" if opt["right"] == "P" else "Long call"
            legs_text = f"Buy {opt['long_strike']:g} {word}"
        else:
            kind = "Bear put spread" if opt["right"] == "P" else "Bull call spread"
            legs_text = f"Buy {opt['long_strike']:g} / Sell {opt['short_strike']:g} {word}"
        max_profit = opt.get("max_profit")
        option_rows.append({
            "symbol": opt["symbol"], "direction": opt["direction"],
            "kind": kind, "legs": legs_text,
            "expiry": opt["expiry"], "qty": qty, "entry_debit": opt["entry_debit"], "mid": mid,
            "pnl": (mid - opt["entry_debit"]) * mult * qty if mid is not None else None,
            "max_loss": opt["entry_debit"] * mult * qty,
            "max_profit": max_profit * mult * qty if max_profit is not None else None,
            "entry_date": opt["entry_date"],
        })

    open_rows, exposures = [], []
    for trade in open_trades:
        price = session.price(trade["symbol"]) or trade["entry_price"]
        sign = -1 if trade["direction"] == "SHORT" else 1
        qty = trade["qty"]
        pnl = sign * (price - trade["entry_price"]) * qty
        notional = sign * price * qty
        entry = A.parse_date(trade["entry_date"])
        open_rows.append({
            "symbol": trade["symbol"], "company": trade["company"], "direction": trade["direction"],
            "tags": "; ".join(trade["tags"]), "score": trade["score"], "qty": qty,
            "entry_price": trade["entry_price"], "price": price, "pnl": pnl,
            "pnl_pct": pnl / (trade["entry_price"] * qty) if qty else 0.0,
            "notional": notional, "stop": trade["stop"], "target": trade["target"],
            "exit_by": trade["exit_by"], "beta": trade["beta"],
            "days_held": (today - entry).days if entry else 0,
        })
        exposures.append({"symbol": trade["symbol"], "signed_notional": notional, "beta": trade["beta"]})

    hedge_exposure = None
    hedge_position = held.get(C.HEDGE_SYMBOL)
    if hedge_position and abs(hedge_position["qty"]) > 0:
        hp = session.price(C.HEDGE_SYMBOL) or hedge_position["market_price"] or 0
        hedge_exposure = {"symbol": C.HEDGE_SYMBOL, "signed_notional": hedge_position["qty"] * hp, "beta": 1.0}

    everything = exposures + ([hedge_exposure] if hedge_exposure else [])
    exposure = {
        "gross": sum(abs(e["signed_notional"]) for e in exposures),
        "net": sum(e["signed_notional"] for e in exposures),
        "net_beta": sum(e["signed_notional"] * e["beta"] for e in exposures),
        "net_beta_after_hedge": sum(e["signed_notional"] * e["beta"] for e in everything),
        "hedge_notional": hedge_exposure["signed_notional"] if hedge_exposure else 0.0,
    }

    histories = {e["symbol"]: A.closes_by_date(session.bars(e["symbol"])) for e in everything}
    risk = A.portfolio_risk([(e["symbol"], e["signed_notional"]) for e in everything], histories) if everything else None
    stress = A.stress_tests(exposures, hedge_exposure)

    curve = []
    for r in read_csv(data_path("equity_curve.csv")):
        if r["date"] != today.isoformat():
            curve.append(r)
    spy = session.price(C.BENCHMARK_SYMBOL)
    if equity:
        curve.append({"date": today.isoformat(), "net_liq": equity, "spy_price": spy if spy else ""})
    curve.sort(key=lambda r: r["date"])
    write_csv(data_path("equity_curve.csv"), curve, ["date", "net_liq", "spy_price"])
    numeric_curve = []
    for r in curve:
        try:
            numeric_curve.append({
                "date": r["date"], "net_liq": float(r["net_liq"]),
                "spy_price": float(r["spy_price"]) if r["spy_price"] not in ("", None) else None,
            })
        except ValueError:
            continue

    spy_adv = None
    spy_bars = session.bars(C.BENCHMARK_SYMBOL)
    if spy_bars and spy:
        raw = A.avg_volume(spy_bars)
        if raw:
            spy_adv = raw * C.HISTORY_VOLUME_MULTIPLIER * spy

    write_csv(data_path("positions_snapshot.csv"), open_rows, [
        "symbol", "company", "direction", "tags", "score", "qty", "entry_price", "price",
        "pnl", "pnl_pct", "notional", "stop", "target", "exit_by", "beta", "days_held",
    ])

    return {
        "account_id": account_id, "generated": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "acct": acct, "equity": equity, "plan": plan, "open_rows": open_rows,
        "closed": closed_trades, "hedge_info": hedge_info, "exposure": exposure,
        "risk": risk, "stress": stress, "curve": numeric_curve,
        "equity_stats": A.equity_stats(numeric_curve),
        "alpha_beta": A.alpha_beta(numeric_curve),
        "trade_stats": A.trade_stats(closed_trades),
        "tag_stats": group_stats(closed_trades),
        "option_rows": option_rows,
        "events": events or [],
        "spy_adv": spy_adv,
    }


def group_stats(closed_trades):
    """Results by signal type for stock trades, then by long/short, then the option trades."""
    def is_option(t):
        return "OPTION_SPREAD" in t["tags"] or "OPTION_SINGLE" in t["tags"]

    stock = [t for t in closed_trades if not is_option(t)]
    spreads = [t for t in closed_trades if "OPTION_SPREAD" in t["tags"]]
    singles = [t for t in closed_trades if "OPTION_SINGLE" in t["tags"]]
    stats = A.stats_by_tag(stock)
    for side in ("LONG", "SHORT"):
        subset = [t for t in stock if t["direction"] == side]
        if subset:
            stats[f"{side} stock trades"] = A.trade_stats(subset)
    if spreads:
        stats["Option spreads (all)"] = A.trade_stats(spreads)
    if singles:
        stats["Single calls and puts (all)"] = A.trade_stats(singles)
    return stats


# ---------------------------------------------------------------- the main entry point

def print_plan(plan):
    trades = [r for r in plan if r["decision"] in ("TRADE", "FILLED", "NOT FILLED")]
    print(f"\nTrade plan: {len(trades)} trade(s), {len(plan) - len(trades)} skipped.")
    for r in plan:
        extra = f" x{r['qty']} @ {r['price']}" if r.get("qty") else ""
        spread = f" | option: {r['spread_status']}" if r.get("spread_status") else ""
        print(f"  [{r['decision']}] {r['symbol'] or '-'} {r['direction']}{extra} score {r['score']}: {r['decision_reason']}{spread}")


def run(mode, broker, signals_path=None, force=False, today=None):
    today = today or today_et()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    account = broker.account_id

    if mode == "trade" and not str(account).startswith("DU"):
        raise SystemExit(f"Safety stop: account {account} is not a paper account (paper accounts start with DU).")
    if mode == "trade" and not market_is_open() and not force:
        raise SystemExit("The US stock market is closed right now. Run this between 9:35 AM and 3:45 PM Eastern, or add --force.")

    session = Session(broker)
    equity = broker.account_values().get("NetLiquidation")
    if not equity:
        raise RetryLater("Could not read the account value from IBKR, so nothing was traded.")
    print(f"Account {account}, net liquidation ${equity:,.0f}")

    open_trades = load_open_trades()
    open_options = load_open_options()
    closed_trades = load_closed_trades()
    hedge_info = None
    events = []

    if mode == "trade" and not positions_loaded(broker, open_trades, open_options):
        raise RetryLater("IBKR had not finished loading the account's positions, so nothing was changed.")

    if mode == "trade":
        settle_pending(broker, open_trades, open_options, events)
        open_trades, closed_now = manage_exits(session, open_trades, today)
        for c in closed_now:
            events.append(f"Closed {c['symbol']} ({c['exit_reason']}): P&L ${c['pnl']:,.2f}")
        open_options, closed_options = manage_option_exits(session, open_options, open_trades, today, events)
        closed_trades += closed_now + closed_options
        for line in events:
            print(f"  {line}")
        save_open_trades(open_trades)
        save_open_options(open_options)
        save_closed_trades(closed_trades)

    plan_file = data_path(f"trade_plan_{today.isoformat()}.csv")
    if mode == "report":
        plan = read_csv(plan_file)
    else:
        try:
            signals = load_signals(signals_path)
        except Exception as e:                        # no new trades today, but exits and the hedge still run
            signals = []
            note = f"No new tracker trades today: {e}. Exits and the hedge still ran."
            print(f"  [SIGNALS] {note}")
            events.append(note)
        print(f"Loaded {len(signals)} ranked signal(s).")
        newest = newest_signal_date(signals)
        if signals and newest and (today - newest).days > STALE_SIGNAL_DAYS:
            note = (f"The newest signal is from {newest.isoformat()}, which is old. "
                    "Check the GitHub scanner (the Actions tab) - it may have stopped running.")
            print(f"  [SIGNALS] {note}")
            events.append(note)
        counts, names_today = opened_today(open_trades, closed_trades, today)
        try:
            mine = {t["symbol"] for t in open_trades}
            in_account = {p["symbol"] for p in broker.positions() if p["sec_type"] == "STK" and abs(p["qty"]) >= 1}
            avoid = names_to_avoid(names_today, in_account - mine)
        except Exception:
            avoid = {}
        plan = build_plan(session, signals, equity, {t["symbol"] for t in open_trades}, today, len(open_options),
                          opened_today=counts, avoid=avoid)
        print("Running the safety checks on the planned trades...")
        try:
            guard_notes = risk.apply_entry_guards(plan, session, open_trades, equity, today)
        except Exception as exc:                       # a guard problem must never stop the tracker
            guard_notes = [f"safety checks skipped because of an error: {exc}"]
        for note in guard_notes:
            print(f"  [GUARD] {note}")
            if mode == "trade":
                events.append(f"Guard: {note}")

    if mode == "trade":
        execute_plan(broker, plan, open_trades, open_options, today, events)
        save_open_trades(open_trades)
        save_open_options(open_options)
        hedge_info = rebalance_hedge(session, open_trades, today)
        if hedge_info:
            print(f"Hedge: {hedge_info['status']}")
            if hedge_info["status"] != "already balanced":
                events.append(f"Hedge: {hedge_info['status']}")

    if mode != "report":
        write_csv(plan_file, plan, PLAN_FIELDS)
        print_plan(plan)

    report = build_report(session, account, today, open_trades, closed_trades, plan, hedge_info, open_options, events)
    if report["spy_adv"] is not None:
        print(f"Volume check: {C.BENCHMARK_SYMBOL} average daily dollar volume is ${report['spy_adv']:,.0f} "
              f"(should be in the tens of billions; if it looks 100 times too small or big, change HISTORY_VOLUME_MULTIPLIER).")
    path = data_path("tracker_dashboard.html")
    write_text(path, dashboard.render(report))
    print(f"\nDashboard saved to {path}")
    return report, path
