"""
sleeve_engine.py
Runs the strategy sleeves: trend, sector rotation, momentum, mean reversion, pairs, and the idle-cash
Treasury-bill sleeve. It keeps its own ledger so it never touches the SEC-filing tracker's trades.
Works with any broker adapter that has the same methods as broker_extra.IBAdapter.
Paper account only.
"""

import csv
import html
import json
import shutil
import math
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import allocator
import config as C
import risk
import sleeve_config as SC
import sleeve_strategies as S

ET = ZoneInfo("America/New_York")


def _clock():
    return time.time()


TRADE_FIELDS = ["date", "sleeve", "symbol", "action", "qty", "price", "reason", "realized_pnl"]
VALUE_FIELDS = ["date", "sleeve", "market_value", "realized", "unrealized"]


def data_path(name):
    return os.path.join(C.DATA_DIR, name)


def today_et():
    return datetime.now(ET).date()


def market_is_open(now=None):
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _f(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def append_csv(path, fields, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------- the ledger

class Ledger:
    """What each sleeve owns. Saved in tracker_data/sleeves_state.json."""

    def __init__(self, path=None):
        self.path = path or data_path("sleeves_state.json")
        self.positions = {}
        self.realized = {}
        self.last_rebalance = {}
        self.stopped = {}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = {}
        except ValueError:
            try:
                shutil.copy(self.path, self.path + ".corrupt")
            except OSError:
                pass
            raise SystemExit(f"The sleeves memory file {self.path} is damaged. A copy was saved as .corrupt. "
                             "Nothing was traded. Restore it from GitHub/backup or ask for help before deleting it.")
        self.positions = raw.get("positions", {})
        self.realized = raw.get("realized", {})
        self.last_rebalance = raw.get("last_rebalance", {})
        self.stopped = raw.get("stopped", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "positions": self.positions, "realized": self.realized,
                "last_rebalance": self.last_rebalance, "stopped": self.stopped,
            }, f, indent=2)
        os.replace(tmp, self.path)

    def qty(self, sleeve, symbol):
        return self.positions.get(sleeve, {}).get(symbol, {}).get("qty", 0)

    def total_qty(self, symbol):
        return sum(p.get(symbol, {}).get("qty", 0) for p in self.positions.values())

    def symbols(self):
        return {s for p in self.positions.values() for s in p}

    def apply_fill(self, sleeve, symbol, signed_qty, price, today_iso, reason=""):
        book = self.positions.setdefault(sleeve, {})
        pos = book.get(symbol)
        q = pos["qty"] if pos else 0
        cost = pos["avg_cost"] if pos else 0.0
        entry = pos["entry_date"] if pos else today_iso
        new_q = q + signed_qty
        realized = 0.0
        if q == 0 or q * signed_qty > 0:
            cost = (abs(q) * cost + abs(signed_qty) * price) / abs(new_q)
        else:
            closing = min(abs(signed_qty), abs(q))
            realized = (price - cost) * closing * (1 if q > 0 else -1)
            if abs(signed_qty) > abs(q):
                cost, entry = price, today_iso
        if new_q == 0:
            book.pop(symbol, None)
        else:
            book[symbol] = {"qty": new_q, "avg_cost": cost, "entry_date": entry}
        if realized:
            self.realized[sleeve] = self.realized.get(sleeve, 0.0) + realized
        append_csv(data_path("sleeve_trades.csv"), TRADE_FIELDS, {
            "date": today_iso, "sleeve": sleeve, "symbol": symbol,
            "action": "BUY" if signed_qty > 0 else "SELL", "qty": abs(signed_qty),
            "price": round(price, 4), "reason": reason, "realized_pnl": round(realized, 2),
        })
        return realized


# ---------------------------------------------------------------- price history (cached on disk)

def split_ratio(old, new, check_days=5):
    """If the last few days in both the saved and the new prices disagree by the same ratio (a stock split or a
    price adjustment since the last download), returns that ratio, else None."""
    shared = sorted(d for d in new if d in old)[-check_days:]
    pairs = [(new[d], old[d]) for d in shared if old[d] > 0 and new[d] > 0]
    if len(pairs) < 2:
        return None
    ratios = sorted(a / b for a, b in pairs)
    mid = ratios[len(ratios) // 2]
    if abs(mid - 1) <= 0.02 or any(abs(r / mid - 1) >= 0.005 for r in ratios):
        return None
    return mid


class PriceStore:
    def __init__(self, adapter, today_iso, fetch_seconds=None, price_dir=None, clock=None):
        self.adapter = adapter
        self.today = today_iso
        self.clock = clock or (lambda: _clock())
        self.deadline = self.clock() + fetch_seconds if fetch_seconds is not None else None
        self.price_dir = price_dir or data_path("prices")
        self.request_times = []
        self.cache = {}
        self.fetched = 0
        self.no_data = []
        self.skipped_for_time = []
        self.rescaled = []

    def _file(self, symbol):
        return os.path.join(self.price_dir, symbol.replace("/", "_") + ".csv")

    def _load(self, symbol):
        rows = {}
        try:
            with open(self._file(symbol), newline="") as f:
                for r in csv.DictReader(f):
                    c = _f(r.get("close"))
                    if c and c > 0:
                        rows[r["date"]] = c
        except FileNotFoundError:
            pass
        return rows

    def _save(self, symbol, rows):
        os.makedirs(self.price_dir, exist_ok=True)
        keep = sorted(rows.items())[-3000:]
        with open(self._file(symbol), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "close"])
            for d, c in keep:
                w.writerow([d, c])

    def expected_last(self):
        d = date.fromisoformat(self.today) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()

    def _out_of_time(self):
        return self.deadline is not None and self.clock() > self.deadline

    def _throttle(self):
        """Keeps history requests under IBKR's limit. Returns False if we ran out of time waiting."""
        while True:
            now = self.clock()
            self.request_times = [t for t in self.request_times if now - t < 600]
            if len(self.request_times) < SC.HISTORY_REQUESTS_PER_10_MIN:
                self.request_times.append(now)
                return True
            wait = 600 - (now - self.request_times[0]) + 1
            if self.deadline is not None and now + wait > self.deadline:
                return False
            print(f"  waiting {wait:.0f}s so IBKR is not asked for history too fast...")
            self.adapter.sleep(wait)

    def get(self, symbol):
        """{"dates", "close"} of completed days (today's unfinished bar is dropped), or None."""
        if symbol in self.cache:
            return self.cache[symbol]
        rows = self._load(symbol)
        last = max(rows) if rows else None
        if last is None or last < self.expected_last():
            if self.deadline is not None and self._out_of_time():
                self.skipped_for_time.append(symbol)
            else:
                gap = (date.fromisoformat(self.today) - date.fromisoformat(last)).days if last else 9999
                duration = f"{SC.PRICE_YEARS} Y" if gap > 40 else f"{max(5, gap + 3)} D"
                if self._throttle():
                    bars = self.adapter.daily_bars(symbol, duration)
                    self.fetched += 1
                    new = {b["date"]: b["close"] for b in bars if b["date"] < self.today}
                    ratio = split_ratio(rows, new) if rows and new else None
                    if ratio:                            # a split: rescale the saved days so the history stays smooth
                        rows = {d: c * ratio for d, c in rows.items()}
                        self.rescaled.append(symbol)
                    rows.update(new)
                    if bars:
                        self._save(symbol, rows)
                else:
                    self.skipped_for_time.append(symbol)
        dates = sorted(d for d in rows if d < self.today)
        if not dates:
            self.no_data.append(symbol)
            self.cache[symbol] = None
            return None
        self.cache[symbol] = {"dates": dates, "close": [rows[d] for d in dates]}
        return self.cache[symbol]

    def needs_fetch(self, symbol):
        rows = self._load(symbol)
        return not rows or max(rows) < self.expected_last()

    def get_many(self, symbols):
        out = {}
        for s in symbols:
            d = self.get(s)
            if d:
                out[s] = d
        return out


class CacheOnlyStore(PriceStore):
    """Reads saved prices only. Never asks the broker."""

    def get(self, symbol):
        if symbol in self.cache:
            return self.cache[symbol]
        rows = self._load(symbol)
        dates = sorted(d for d in rows if d < self.today)
        self.cache[symbol] = {"dates": dates, "close": [rows[d] for d in dates]} if dates else None
        return self.cache[symbol]


# ---------------------------------------------------------------- planning

def _trunc_qty(value, price):
    if not price or price <= 0:
        return 0
    n = int(math.floor(abs(value) / price))
    return n if value >= 0 else -n


def plan_sleeves(adapter, store, ledger, today_iso, equity, positions, flags, allocations=None, brain=None):
    """Works out what every sleeve wants to own. Returns a plan dict. Places nothing.
    brain: the market brain's decision (market_brain.today). Its "scales" replace the old regime rule and its
    "params" replace a strategy's settings, but only for the parts switched "on"."""
    by_symbol = {p["symbol"]: p for p in positions}
    ledger_symbols = ledger.symbols()

    blocked = {s for s, p in by_symbol.items() if abs(p["qty"]) >= 1 and s not in ledger_symbols}
    blocked.add(C.HEDGE_SYMBOL)
    mismatches = []
    for s in ledger_symbols:
        broker_qty = by_symbol.get(s, {}).get("qty", 0)
        if abs(broker_qty - ledger.total_qty(s)) >= 1:
            mismatches.append({"symbol": s, "ledger": ledger.total_qty(s), "broker": broker_qty})
            blocked.add(s)
    nonsleeve_gross = sum(abs(p["market_value"] or 0.0) for s, p in by_symbol.items() if s not in ledger_symbols)

    spy = store.get("SPY")
    regime = risk.market_regime(spy["close"] if spy else None, SC.REGIME_SMA_DAYS)
    scale = risk.regime_scale(regime)
    loss = risk.daily_loss_status(equity, risk.previous_equity(today_iso))
    brain_scales = (brain or {}).get("scales")
    brain_params = (brain or {}).get("params") or {}

    def last_price(symbol, data=None):
        p = by_symbol.get(symbol)
        if p and p.get("market_price"):
            return p["market_price"]
        d = (data or {}).get(symbol) or store.cache.get(symbol)
        if d and d["close"]:
            return d["close"][-1]
        return None

    sleeves = {}
    raw_targets = {}      # sleeve -> {symbol: dollar value (signed)} for sleeves that made a decision
    fixed_gross = 0.0

    for name in S.SLEEVE_ORDER:
        if not SC.SLEEVE_ENABLED.get(name):
            continue
        freq = S.FREQ[name]
        due = freq == "daily" or S.month_changed(ledger.last_rebalance.get(name), today_iso)
        held = dict(ledger.positions.get(name, {}))
        share = (allocations or {}).get(name, SC.SLEEVE_ALLOCATION.get(name, 0.0))
        if brain_scales is not None:                     # the market brain's weather dial is on
            size = brain_scales.get(name, 1.0)
        else:
            size = scale if name in SC.REGIME_SCALED_SLEEVES else 1.0
        capital = equity * share * size
        params = brain_params.get(name) or None
        sp = {"name": name, "due": due, "capital": capital, "share": share, "size": size, "params": params,
              "notes": [], "info": [], "status": "", "decided": False, "stopped": []}
        sleeves[name] = sp

        if not due:
            sp["status"] = "holding until the next monthly rebalance"
            fixed_gross += sum(abs(pos["qty"] * (last_price(s) or pos["avg_cost"])) for s, pos in held.items())
            continue

        universe = [s for s in S.UNIVERSE[name] if s not in blocked]
        data = store.get_many(universe)
        oldest_ok = (date.fromisoformat(store.expected_last()) - timedelta(days=4)).isoformat()
        stale = [x for x, d in data.items() if d["dates"][-1] < oldest_ok]
        if stale:
            flags.append(f"{name}: ignored {len(stale)} symbols with old prices ({', '.join(sorted(stale)[:5])}).")
            data = {x: d for x, d in data.items() if x not in stale}
        coverage = len(data) / max(1, len(universe))
        if len(universe) < len(S.UNIVERSE[name]) * 0.5 or coverage < SC.MIN_DATA_COVERAGE:
            sp["status"] = f"waiting for price history ({len(data)} of {len(S.UNIVERSE[name])} symbols ready)"
            flags.append(f"{name}: not enough price history yet. Run: python tracker/run_sleeves.py prefetch")
            fixed_gross += sum(abs(pos["qty"] * (last_price(s) or pos["avg_cost"])) for s, pos in held.items())
            continue
        if name in ledger.stopped and S.FREQ[name] == "monthly":
            ledger.stopped[name] = {}
        result = S.RUNNERS[name](data, held, today_iso, params)
        sp["notes"] = result["notes"]
        sp["info"] = result["info"]
        sp["decided"] = True
        sp["status"] = "rebalancing" if freq == "monthly" else "checked"
        values = {}
        for s, w in result["weights"].items():
            if s in blocked:
                continue
            values[s] = w * capital
        for s in held:                                   # held but not wanted now: close it (unless blocked)
            if s not in values and s not in blocked:
                values[s] = 0.0
        raw_targets[name] = values
        fixed_gross += sum(abs(pos["qty"] * (last_price(x) or pos["avg_cost"])) for x, pos in held.items() if x in blocked)
        sp["_data"] = data

    # stops for monthly sleeves that did not rebalance today (and the ones that did, for safety)
    forced_zero = {}
    for name, sp in sleeves.items():
        stop = SC.SLEEVE_STOP_PCT.get(name)
        if not stop or sp["decided"] and S.FREQ[name] == "monthly":
            continue
        for s, pos in ledger.positions.get(name, {}).items():
            price = last_price(s)
            if not price or s in blocked:
                continue
            q = pos["qty"]
            hit = (q > 0 and price < pos["avg_cost"] * (1 - stop)) or (q < 0 and price > pos["avg_cost"] * (1 + stop))
            if hit:
                forced_zero.setdefault(name, set()).add(s)
                sp["stopped"].append(s)
                sp["notes"].append(f"stop loss on {s}: sell and wait for the next monthly rebalance")

    # shrink everything if the whole account would be too invested
    scaled_gross = sum(abs(v) for values in raw_targets.values() for v in values.values())
    available = SC.MAX_TOTAL_GROSS_PCT * equity - nonsleeve_gross - fixed_gross
    factor = 1.0
    if scaled_gross > 0 and available < scaled_gross:
        factor = max(0.0, available / scaled_gross)
        flags.append(f"Total exposure limit: sleeve positions shrunk to {factor * 100:.0f}% of plan.")

    # turn dollar targets into share targets
    targets = {}          # (sleeve, symbol) -> target shares
    prices = {}
    for name, sp in sleeves.items():
        held = ledger.positions.get(name, {})
        names = set(held)
        if name in raw_targets:
            names |= set(raw_targets[name])
        for s in names:
            cur = held.get(s, {}).get("qty", 0)
            if s in blocked:
                targets[(name, s)] = cur
                continue
            if s in forced_zero.get(name, set()):
                targets[(name, s)] = 0
                prices[s] = last_price(s) or ledger.positions.get(name, {}).get(s, {}).get("avg_cost")
                continue
            if name not in raw_targets:                  # no decision today: stay as is
                targets[(name, s)] = cur
                continue
            price = last_price(s, sp.get("_data"))
            if price is None:
                targets[(name, s)] = cur
                continue
            prices[s] = price
            targets[(name, s)] = _trunc_qty(raw_targets[name].get(s, 0.0) * factor, price)

    # the idle-cash sleeve
    cash_info = None
    if SC.CASH_SLEEVE_ENABLED:
        gross_after = 0.0
        for (name, s), q in targets.items():
            price = prices.get(s) or last_price(s) or ledger.positions.get(name, {}).get(s, {}).get("avg_cost") or 0.0
            gross_after += abs(q) * price
        cash_symbol = SC.CASH_SYMBOL
        cash_data = store.get(cash_symbol)
        if cash_data is None and SC.CASH_FALLBACK_SYMBOL:
            cash_symbol = SC.CASH_FALLBACK_SYMBOL
            cash_data = store.get(cash_symbol)
        if cash_symbol in blocked:
            cash_data = None
        cash_price = last_price(cash_symbol, {cash_symbol: cash_data} if cash_data else None)
        if cash_price:
            have = ledger.qty("cash", cash_symbol)
            other_symbol = SC.CASH_FALLBACK_SYMBOL if cash_symbol == SC.CASH_SYMBOL else SC.CASH_SYMBOL
            target_value = max(0.0, equity * (1 - SC.CASH_BUFFER_PCT) - nonsleeve_gross - gross_after)
            want = _trunc_qty(target_value, cash_price)
            diff_value = abs(want - have) * cash_price
            if diff_value < SC.CASH_REBALANCE_BAND_PCT * equity:
                want = have
            prices[cash_symbol] = cash_price
            targets[("cash", cash_symbol)] = want
            if ledger.qty("cash", other_symbol):
                other_price = last_price(other_symbol)
                if other_price:
                    prices[other_symbol] = other_price
                    targets[("cash", other_symbol)] = 0
            cash_info = {"symbol": cash_symbol, "price": cash_price, "target_value": target_value,
                         "have": have, "target": want}
        else:
            flags.append("Idle-cash sleeve: no price for SGOV or BIL, skipped.")

    return {
        "blocked": sorted(blocked), "mismatches": mismatches, "nonsleeve_gross": nonsleeve_gross,
        "regime": regime, "regime_scale": scale, "loss": loss, "factor": factor,
        "sleeves": sleeves, "targets": targets, "prices": prices, "cash": cash_info,
        "forced_zero": forced_zero,
    }


def make_orders(plan, ledger, quotes_fn, flags):
    """Turns share targets into a list of orders (sells first). quotes_fn(symbols) -> quote dict."""
    wanted = []
    halted = plan["loss"]["halt"]
    for (name, symbol), tgt in plan["targets"].items():
        cur = ledger.qty(name, symbol)
        if tgt == cur:
            continue
        price = plan["prices"].get(symbol)
        if not price:
            continue
        if halted:                                         # loss limit: only allow reducing exposure
            if abs(tgt) > abs(cur) or cur * tgt < 0:
                tgt = 0 if cur * tgt < 0 else cur
                if tgt == cur:
                    continue
        delta = tgt - cur
        value = abs(delta) * price
        is_exit = tgt == 0
        if not is_exit and value < SC.MIN_ORDER_DOLLARS:
            continue
        if cur != 0 and tgt != 0 and cur * tgt > 0 and value < SC.REBALANCE_BAND * abs(tgt) * price and name != "cash":
            continue
        if cur * tgt < 0:                                  # crossing zero: close first, then open
            wanted.append({"sleeve": name, "symbol": symbol, "delta": -cur, "price": price, "kind": "close", "flip": True})
            wanted.append({"sleeve": name, "symbol": symbol, "delta": tgt, "price": price, "kind": "open", "flip": True})
        else:
            kind = "close" if abs(tgt) < abs(cur) else "open"
            wanted.append({"sleeve": name, "symbol": symbol, "delta": delta, "price": price, "kind": kind, "flip": False})
    if not wanted:
        return []

    quotes = quotes_fn(sorted({o["symbol"] for o in wanted}))
    orders, dropped = [], set()
    for o in wanted:
        q = quotes.get(o["symbol"])
        o["qty"] = abs(o["delta"])
        o["action"] = "BUY" if o["delta"] > 0 else "SELL"
        if not q or not q.get("price"):
            o["status"] = "skipped: no quote"
            dropped.add((o["sleeve"], o["symbol"]))
            orders.append(o)
            continue
        o["price"] = q["price"]
        base = 0 if (o["flip"] and o["kind"] == "open") else ledger.qty(o["sleeve"], o["symbol"])
        opens_short = o["delta"] < 0 and base + o["delta"] < 0
        if opens_short:
            level = q.get("shortable_level")
            if level is not None and level < SC.MIN_SHORTABLE_LEVEL:
                o["status"] = f"skipped: hard to borrow (level {level:g})"
                dropped.add((o["sleeve"], o["symbol"]))
            elif level is None and not SC.ALLOW_UNKNOWN_SHORTABLE:
                o["status"] = "skipped: shortability unknown"
                dropped.add((o["sleeve"], o["symbol"]))
        slip = SC.SLEEVE_LIMIT_SLIPPAGE
        o["limit"] = round(o["price"] * (1 + slip if o["action"] == "BUY" else 1 - slip), 2)
        orders.append(o)

    # a pair is opened as a pair: if one new leg is dropped, drop the other new leg too
    for o in orders:
        if o.get("status") or o["sleeve"] != "pairs" or o["kind"] != "open":
            continue
        partner = S.PARTNER.get(o["symbol"])
        if partner and ("pairs", partner) in dropped and ledger.qty("pairs", partner) == 0:
            o["status"] = "skipped: the other leg of the pair cannot be traded"
    # a flipped position needs its closing leg to fill before the opening leg is sent
    for o in orders:
        if o.get("status") and o["kind"] == "close" and o["flip"]:
            for p in orders:
                if p["sleeve"] == o["sleeve"] and p["symbol"] == o["symbol"] and p["kind"] == "open" and p["flip"]:
                    p["status"] = "skipped: the closing leg could not be sent"

    live = [o for o in orders if not o.get("status")]
    live.sort(key=lambda o: (0 if o["kind"] == "close" else 1, -abs(o["delta"]) * o["price"]))
    for i, o in enumerate(live):
        o["id"] = i + 1
        if i >= SC.MAX_ORDERS_PER_RUN:
            o["status"] = "deferred to the next run (order limit)"
    # final check: a new pair needs BOTH legs to be real orders that will be sent
    for _ in range(2):
        for o in live:
            if o.get("status") or o["sleeve"] != "pairs" or o["kind"] != "open":
                continue
            partner = S.PARTNER.get(o["symbol"])
            if not partner or ledger.qty("pairs", partner) != 0:
                continue
            if not any(p["sleeve"] == "pairs" and p["symbol"] == partner and p["kind"] == "open" and not p.get("status")
                       for p in live):
                o["status"] = "skipped: the other leg of the pair is not being traded"
    for o in live:
        if o["flip"] and o["kind"] == "open":
            for p in live:
                if p["sleeve"] == o["sleeve"] and p["symbol"] == o["symbol"] and p["kind"] == "close" and p["flip"]:
                    o["requires"] = p["id"]
    others = [o for o in orders if o.get("status") and "id" not in o]
    for o in others:
        o["id"] = None
    return live + others


def reconcile_late_fills(adapter, ledger, orders, today_iso, events):
    """A fill can land just after we cancel a slow order. If the broker holds a little more or less than the
    ledger for a symbol we traded this run, and the gap is no bigger than the order, fix the ledger."""
    touched = {}
    for o in orders:
        if o.get("id") and (o.get("status") or "").startswith(("NOT FILLED", "PARTIAL")):
            touched.setdefault(o["symbol"], o)
    if not touched:
        return
    by_symbol = {p["symbol"]: p for p in adapter.positions()}
    for symbol, o in touched.items():
        broker_qty = by_symbol.get(symbol, {}).get("qty", 0)
        diff = broker_qty - ledger.total_qty(symbol)
        if abs(diff) < 1 or abs(diff) > o["qty"]:
            continue
        if (diff > 0) != (o["action"] == "BUY"):
            continue
        price = (by_symbol.get(symbol) or {}).get("market_price") or o["price"]
        ledger.apply_fill(o["sleeve"], symbol, diff, price, today_iso, "late fill found after cancel")
        ledger.save()
        events.append(f"{o['sleeve']}: late fill of {abs(diff):g} {symbol} found and recorded")


def execute_orders(adapter, ledger, orders, today_iso, events):
    by_id = {o["id"]: o for o in orders if o.get("id")}
    filled_by_id = {}
    for o in orders:
        if o.get("status") or not o.get("id"):
            continue
        required = o.get("requires")
        if required:
            need = by_id[required]
            if filled_by_id.get(required, 0) < need["qty"]:
                o["status"] = "skipped: the closing leg did not fully fill"
                continue
        ref = f"SLEEVE-{o['sleeve']}-{o['symbol']}-{today_iso}"
        result = adapter.limit_order(o["symbol"], o["action"], int(o["qty"]), o["limit"], ref)
        filled = int(result.get("filled") or 0)
        filled_by_id[o["id"]] = filled
        if filled < 1:
            o["status"] = f"NOT FILLED ({result.get('status')})"
            continue
        fill = result.get("avg_fill_price") or o["price"]
        signed = filled if o["action"] == "BUY" else -filled
        ledger.apply_fill(o["sleeve"], o["symbol"], signed, fill, today_iso, f"{o['sleeve']} {o['kind']}")
        ledger.save()
        o["filled"] = filled
        o["fill_price"] = fill
        o["status"] = "FILLED" if filled >= o["qty"] else f"PARTIAL {filled} of {int(o['qty'])}"
        events.append(f"{o['sleeve']}: {o['action']} {filled} {o['symbol']} at {fill:.2f}")


# ---------------------------------------------------------------- reporting

def sleeve_rows(ledger, plan, positions, today_iso):
    by_symbol = {p["symbol"]: p for p in positions}
    rows = []
    names = [n for n in S.SLEEVE_ORDER if n in plan["sleeves"]]
    if SC.CASH_SLEEVE_ENABLED:
        names.append("cash")
    for name in names:
        held = ledger.positions.get(name, {})
        pos_rows, mv, unreal = [], 0.0, 0.0
        for s, pos in sorted(held.items()):
            price = (by_symbol.get(s) or {}).get("market_price") or pos["avg_cost"]
            value = pos["qty"] * price
            pnl = (price - pos["avg_cost"]) * pos["qty"]
            mv += value
            unreal += pnl
            pos_rows.append({"symbol": s, "qty": pos["qty"], "avg_cost": pos["avg_cost"], "price": price,
                             "value": value, "pnl": pnl, "entry_date": pos["entry_date"]})
        sp = plan["sleeves"].get(name, {})
        rows.append({
            "name": name,
            "allocation": sp.get("share") if name != "cash" else None,
            "normal": SC.SLEEVE_ALLOCATION.get(name),
            "capital": sp.get("capital"),
            "due": sp.get("due"), "status": sp.get("status", ""),
            "notes": sp.get("notes", []), "info": sp.get("info", []),
            "market_value": mv, "realized": ledger.realized.get(name, 0.0), "unrealized": unreal,
            "positions": pos_rows,
        })
    return rows


def summary_text(report):
    lines = ["STRATEGY SLEEVES" + (" (PLAN ONLY, no orders placed)" if report["mode"] == "plan" else "")]
    reg = report["regime"]
    if reg["state"] != "unknown":
        used = "" if report.get("sizing") != "weather dial" else " - not used for sizing, the weather dial is on"
        lines.append(f"  Market regime: {reg['state']} (SPY {reg['spy']:.2f} vs {reg['sma_days']}-day average {reg['sma']:.2f}, "
                     f"20-day volatility {reg['vol20'] * 100:.0f}%){used}")
    if report["loss"]["halt"]:
        lines.append(f"  DAILY LOSS LIMIT HIT ({report['loss']['change'] * 100:.1f}%): no new positions today.")
    for row in report["sleeves"]:
        lines.append(
            f"  - {row['name']}: {len(row['positions'])} position(s), value ${row['market_value']:,.0f}, "
            f"realized ${row['realized']:,.0f}, open P&L ${row['unrealized']:,.0f}"
            + (f" [{row['status']}]" if row["status"] and row["name"] != "cash" else "")
        )
    if report["events"]:
        lines.append("  Trades:")
        lines += [f"    {e}" for e in report["events"][:25]]
    elif report["orders"]:
        placed = [o for o in report["orders"] if o.get("id")]
        lines.append(f"  {len(placed)} order(s) planned.")
    else:
        lines.append("  No sleeve orders today.")
    for flag in report["flags"][:8]:
        lines.append(f"  ! {flag}")
    if report["mismatches"]:
        lines.append("  ! The account does not match the sleeve ledger for: "
                     + ", ".join(m["symbol"] for m in report["mismatches"]) + " (those symbols are left alone).")
    if report.get("brain_lines"):
        lines.append("")
        lines += report["brain_lines"]
    if report.get("capital"):
        lines.append("")
        lines.append(allocator.capital_text(report["capital"]["rows"], report["capital"]["totals"], report["capital"]["state"]))
    return "\n".join(lines)


def render_dashboard(report):
    esc = html.escape

    def money(x, signed=False):
        v = _f(x)
        if v is None:
            return "-"
        sign = "+" if signed and v > 0 else ""
        return f"{sign}{'-' if v < 0 else ''}${abs(v):,.0f}"

    def tone(x):
        v = _f(x)
        return "" if not v else ("pos" if v > 0 else "neg")

    def table(headers, rows, empty):
        if not rows:
            return f'<p class="empty">{esc(empty)}</p>'
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(c) + "</tr>" for c in rows)
        return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def td(text, cls=""):
        return f'<td class="{cls}">{esc(str(text))}</td>'

    reg = report["regime"]
    reg_text = "unknown (history still loading)" if reg["state"] == "unknown" else (
        f"{reg['state']}: SPY {reg['spy']:.2f}, {reg['sma_days']}-day average {reg['sma']:.2f}, volatility {reg['vol20'] * 100:.0f}%")
    total_value = sum(r["market_value"] for r in report["sleeves"] if r["name"] != "cash")
    total_realized = sum(r["realized"] for r in report["sleeves"])
    total_open = sum(r["unrealized"] for r in report["sleeves"])
    tiles = "".join(
        f'<div class="tile"><div class="tl">{esc(a)}</div><div class="tv {c}">{esc(b)}</div></div>'
        for a, b, c in [
            ("Account value", money(report["equity"]), ""),
            ("Sleeve positions", money(total_value), ""),
            ("Sleeve realized P&L", money(total_realized, True), tone(total_realized)),
            ("Sleeve open P&L", money(total_open, True), tone(total_open)),
            ("Market regime", reg["state"], ""),
            ("Daily loss limit", "HIT" if report["loss"]["halt"] else "OK", "neg" if report["loss"]["halt"] else "pos"),
        ]
    )
    summary_rows = [[
        td(r["name"]), td(f"{r['allocation'] * 100:.1f}% (normal {r['normal'] * 100:.0f}%)" if r["allocation"] is not None else "rest"),
        td(money(r["capital"]) if r["capital"] is not None else "-"), td(len(r["positions"])),
        td(money(r["market_value"])), td(money(r["realized"], True), tone(r["realized"])),
        td(money(r["unrealized"], True), tone(r["unrealized"])), td(r["status"]),
    ] for r in report["sleeves"]]
    sections = []
    for r in report["sleeves"]:
        pos_rows = [[
            td(p["symbol"]), td("SHORT" if p["qty"] < 0 else "LONG"), td(p["qty"]), td(f"{p['avg_cost']:.2f}"),
            td(f"{p['price']:.2f}"), td(money(p["value"])), td(money(p["pnl"], True), tone(p["pnl"])), td(p["entry_date"]),
        ] for p in r["positions"]]
        notes = "".join(f"<li>{esc(n)}</li>" for n in r["notes"][:12])
        sections.append(
            f"<h2>{esc(r['name'])}</h2>" + (f"<ul>{notes}</ul>" if notes else "")
            + table(["Symbol", "Side", "Shares", "Avg cost", "Now", "Value", "P&L", "Since"], pos_rows, "Nothing held.")
        )
    order_rows = [[
        td(o.get("id") or "-"), td(o["sleeve"]), td(o["symbol"]), td(o.get("action", "-")), td(int(o.get("qty") or 0)),
        td(f"{o.get('price', 0):.2f}"), td(f"{o.get('limit', 0):.2f}" if o.get("limit") else "-"),
        td(o.get("status") or "planned"),
    ] for o in report["orders"]]
    flags = "".join(f"<li>{esc(x)}</li>" for x in report["flags"])
    mism = "".join(f"<li>{esc(m['symbol'])}: ledger {m['ledger']}, account {m['broker']}</li>" for m in report["mismatches"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy sleeves</title><style>
body{{background:#0f1115;color:#e5e7eb;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px 16px 48px}}
h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:26px 0 8px;text-transform:capitalize}}
.sub{{color:#9ca3af;margin-bottom:16px}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tile{{background:#16181d;border:1px solid #262a33;border-radius:8px;padding:10px 12px}}.tl{{color:#9ca3af;font-size:12px}}
.tv{{font-size:18px;font-weight:600}}.pos{{color:#34d399}}.neg{{color:#f87171}}.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #262a33;white-space:nowrap}}
th{{color:#9ca3af;font-weight:500}}.empty{{color:#9ca3af}}li{{margin:2px 0}}
</style></head><body>
<h1>Strategy sleeves</h1>
<div class="sub">{esc(report['generated'])} | account {esc(report['account_id'])} | {esc(report['mode'])} mode | paper money only, results ignore commissions and slippage</div>
<div class="tiles">{tiles}</div>
<p>Market regime: {esc(reg_text)}</p>
{('<h2>Warnings</h2><ul>' + flags + '</ul>') if flags else ''}
{('<h2>Account does not match the ledger</h2><ul>' + mism + '</ul>') if mism else ''}
{('<h2>Market brain</h2>' + report["brain_html"]) if report.get("brain_html") else ''}
<h2>Capital by strategy</h2>
{allocator.capital_html(report["capital"]["rows"], report["capital"]["totals"], report["capital"]["state"]) if report.get("capital") else '<p class="empty">Not available.</p>'}
<h2>Sleeves</h2>
{table(["Sleeve", "Share this month", "Money", "Positions", "Value", "Realized", "Open P&L", "Status"], summary_rows, "No sleeves.")}
<h2>Orders</h2>
{table(["#", "Sleeve", "Symbol", "Action", "Shares", "Price", "Limit", "Result"], order_rows, "No orders this run.")}
{''.join(sections)}
</body></html>"""


# ---------------------------------------------------------------- the main entry point

def _brain_module():
    """The market brain, or None if its files are not there yet."""
    import importlib.util
    if importlib.util.find_spec("market_brain") is None or importlib.util.find_spec("brain_config") is None:
        return None
    import market_brain
    return market_brain


def symbols_for_today(ledger, today_iso, brain_module=None):
    """Every symbol this run may ask IBKR for: the daily sleeves first, then the brain, then monthly sleeves
    that rebalance today."""
    out = ["SPY", SC.CASH_SYMBOL]
    for name in S.SLEEVE_ORDER:
        if SC.SLEEVE_ENABLED.get(name) and S.FREQ[name] == "daily":
            out += S.UNIVERSE[name]
    first = list(dict.fromkeys(out))
    if brain_module is not None and any(brain_module.mode(p) != "off" for p in brain_module.PARTS):
        out += brain_module.symbols()
    for name in S.SLEEVE_ORDER:
        if SC.SLEEVE_ENABLED.get(name) and S.FREQ[name] == "monthly" and S.month_changed(ledger.last_rebalance.get(name), today_iso):
            out += S.UNIVERSE[name]
    return first, list(dict.fromkeys(out))


def fetch_window(store, symbols):
    """Seconds the automatic run may spend loading prices: enough for every symbol that needs a new day, at
    IBKR's pace (about 55 requests per 10 minutes), but never more than 50 minutes."""
    need = sum(1 for s in symbols if store.needs_fetch(s))
    rounds = math.ceil(need / max(1, SC.HISTORY_REQUESTS_PER_10_MIN))
    return min(3000, max(SC.AUTO_FETCH_SECONDS, (rounds - 1) * 600 + SC.AUTO_FETCH_SECONDS))


def run(mode, adapter, force=False, today=None, auto=False):
    """mode: plan (look only), trade (place orders), report (refresh the page from saved data)."""
    today = today or today_et()
    today_iso = today.isoformat()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    flags, events = [], []

    if mode == "trade" and not str(adapter.account_id).startswith("DU"):
        raise SystemExit(f"Safety stop: account {adapter.account_id} is not a paper account (paper accounts start with DU).")
    if mode == "trade" and not SC.SLEEVES_ENABLED:
        raise SystemExit("The sleeves are turned off in sleeve_config.py (SLEEVES_ENABLED).")
    if mode == "trade" and os.path.exists(data_path("PAUSE_SLEEVES")):
        flags.append("Paused: the file PAUSE_SLEEVES exists, so this run only planned. Delete that file to resume.")
        mode = "plan"
    if mode == "trade" and not market_is_open() and not force:
        raise SystemExit("The US stock market is closed right now. Run this between 9:35 AM and 3:45 PM Eastern, or add --force.")
    overlap = S.universes_overlap()
    if overlap:
        raise SystemExit("These symbols are in more than one sleeve, fix the lists: " + ", ".join(overlap))

    equity = adapter.equity()
    if not equity:
        raise SystemExit("Could not read the account value from IBKR.")
    print(f"Sleeves: account {adapter.account_id}, net liquidation ${equity:,.0f}, mode {mode}")

    ledger = Ledger()
    positions = adapter.positions()
    try:
        brain_module = _brain_module()
    except Exception as e:
        brain_module = None
        flags.append(f"Market brain could not load ({e}); the normal rules were used.")
    if mode == "report":
        store = CacheOnlyStore(adapter, today_iso)
    else:
        first, wanted = symbols_for_today(ledger, today_iso, brain_module)
        store = PriceStore(adapter, today_iso)
        if auto:                                 # long enough for every symbol, so no strategy is starved of prices
            store = PriceStore(adapter, today_iso, fetch_seconds=fetch_window(store, wanted))
        store.get_many(first)                    # the daily strategies' prices first

    try:
        alloc_state, allocations = allocator.current(today_iso, save=(mode == "trade"))
    except Exception as e:                       # never let the allocation step stop the sleeves
        flags.append(f"Automatic shifting failed ({e}); every sleeve uses its normal share today.")
        alloc_state, allocations = None, dict(allocator.base_allocations())
    brain = None
    if brain_module is not None:
        try:
            brain = brain_module.today(store, today_iso, allocations, save=(mode == "trade"))
        except Exception as e:                   # never let the brain stop the sleeves
            flags.append(f"Market brain skipped ({e}); the normal rules were used.")
            brain = None
    if brain:
        allocations = brain["allocations"]
    plan = plan_sleeves(adapter, store, ledger, today_iso, equity, positions, flags, allocations, brain)
    orders = []
    if mode != "report":
        orders = make_orders(plan, ledger, adapter.quotes, flags)
    if store.skipped_for_time:
        flags.append(f"Ran out of time loading history for {len(store.skipped_for_time)} symbols. Run: python tracker/run_sleeves.py prefetch")
    if store.no_data and mode != "report":
        flags.append("No price history for: " + ", ".join(sorted(set(store.no_data))[:20]))
    if store.rescaled:
        flags.append("Price history adjusted for a split: " + ", ".join(sorted(set(store.rescaled))))

    if mode == "trade":
        execute_orders(adapter, ledger, orders, today_iso, events)
        reconcile_late_fills(adapter, ledger, orders, today_iso, events)
        for name, sp in plan["sleeves"].items():
            if not sp["decided"]:
                continue
            mine = [o for o in orders if o["sleeve"] == name]
            bad = [o for o in mine if not str(o.get("status", "")).startswith("FILLED")]
            if not bad:
                ledger.last_rebalance[name] = today_iso
            for s in plan["forced_zero"].get(name, set()):
                if ledger.qty(name, s) == 0:
                    ledger.stopped.setdefault(name, {})[s] = today_iso
        for name, symbols in plan["forced_zero"].items():
            for s in symbols:
                if ledger.qty(name, s) == 0:
                    ledger.stopped.setdefault(name, {})[s] = today_iso
        ledger.save()
        positions = adapter.positions()

    rows = sleeve_rows(ledger, plan, positions, today_iso)
    if mode == "trade":
        for r in rows:
            append_csv(data_path("sleeve_values.csv"), VALUE_FIELDS, {
                "date": today_iso, "sleeve": r["name"], "market_value": round(r["market_value"], 2),
                "realized": round(r["realized"], 2), "unrealized": round(r["unrealized"], 2),
            })

    capital = None
    try:
        everything = adapter.all_positions() if hasattr(adapter, "all_positions") else positions
        budgets = dict(allocations)              # what each sleeve may really use today (share x size)
        for name, sp in plan["sleeves"].items():
            budgets[name] = sp["share"] * sp["size"]
        cap_rows, cap_totals = allocator.capital_rows(equity, everything, ledger.positions, budgets)
        capital = {"rows": cap_rows, "totals": cap_totals, "state": alloc_state}
    except Exception as e:
        flags.append(f"Could not build the capital table: {e}")

    brain_lines, brain_html = [], ""
    if brain_module is not None and brain:
        try:
            brain_lines, brain_html = brain_module.summary_lines(brain), brain_module.summary_html(brain)
        except Exception as e:                   # a wording problem must never hide the day's trades
            flags.append(f"Market brain summary skipped ({e}).")

    report = {
        "generated": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"), "mode": mode, "capital": capital,
        "account_id": adapter.account_id, "equity": equity, "regime": plan["regime"],
        "loss": plan["loss"], "factor": plan["factor"], "sleeves": rows, "orders": orders,
        "events": events, "flags": flags, "mismatches": plan["mismatches"], "blocked": plan["blocked"],
        "cash": plan["cash"], "brain": brain,
        "sizing": "weather dial" if (brain and brain.get("scales") is not None) else "200-day rule",
        "brain_lines": brain_lines, "brain_html": brain_html,
    }
    path = data_path("sleeves_dashboard.html")
    with open(path, "w") as f:
        f.write(render_dashboard(report))
    with open(data_path("sleeves_report.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(summary_text(report))
    print(f"\nSleeves dashboard saved to {path}")
    return report, path
