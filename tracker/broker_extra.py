"""
broker_extra.py
Extra IBKR helpers for the strategy sleeves and the new risk limits. broker_ib.py is not changed.
IBAdapter gives the sleeves one small, simple interface to the broker (and lets the tests use a fake one).
"""

import math
import time

HISTORY_TIMEOUT = 45      # price history for one symbol: give up after this long (it normally takes 1-2 seconds)


def _num(x):
    if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x):
        return float(x)
    return None


def _positive(*values):
    for v in values:
        n = _num(v)
        if n is not None and n > 0:
            return n
    return None


def sector(broker, symbol):
    """IBKR's industry name for a stock (for example 'Biotechnology'), or None if unknown. Cached."""
    cache = getattr(broker, "_sector_cache", None)
    if cache is None:
        cache = broker._sector_cache = {}
    if symbol in cache:
        return cache[symbol]
    value = None
    try:
        contract = broker._stock(symbol)
        if contract is not None:
            details = broker.ib.reqContractDetails(contract)
            if details:
                value = (details[0].industry or details[0].category or None)
    except Exception:
        value = None
    cache[symbol] = value
    return value


class IBAdapter:
    """What the sleeves need from the broker. Wraps the existing IBBroker."""

    def __init__(self, broker):
        self.broker = broker
        self.ib = broker.ib

    @property
    def account_id(self):
        return self.broker.account_id

    def equity(self):
        return self.broker.account_values().get("NetLiquidation")

    def positions(self):
        return [p for p in self.broker.positions() if p["sec_type"] == "STK"]

    def all_positions(self):
        """Every position, stocks and options (for the capital table)."""
        return list(self.broker.positions())

    def is_connected(self):
        fn = getattr(self.broker, "is_connected", None)
        return True if fn is None else bool(fn())

    def gross_position_value(self):
        """What IBKR says all positions are worth together (long + short), or None if unknown."""
        try:
            return self.broker.account_values().get("GrossPositionValue")
        except Exception:
            return None

    @property
    def history_log(self):
        """Times of every price-history request this run (shared with the tracker, so IBKR's pace limit counts both)."""
        return getattr(self.broker, "history_log", None)

    def todays_fills(self):
        """Today's fills from IBKR's own records (see IBBroker.todays_fills), or [] if not available."""
        fn = getattr(self.broker, "todays_fills", None)
        return fn() if fn else []

    def sleep(self, seconds):
        self.ib.sleep(seconds)

    def daily_bars(self, symbol, duration):
        """duration like '2 Y' or '10 D'. Returns [{"date", "close"}, ...] oldest first.
        Prints one line per symbol, so a long download shows it is still working."""
        started = time.time()
        contract = self.broker._stock(symbol)
        if contract is None:
            print(f"  prices: {symbol} not found at IBKR")
            return []
        try:
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True, formatDate=1, timeout=HISTORY_TIMEOUT,
            )
        except Exception:
            bars = []
        out = [{"date": str(b.date)[:10], "close": float(b.close)} for b in (bars or []) if _positive(b.close)]
        print(f"  prices: {symbol} {len(out)} day(s) in {time.time() - started:.1f}s")
        return out

    def quotes(self, symbols):
        """{symbol: {"price", "bid", "ask", "shortable_level"}} for the symbols that have a price."""
        out = {}
        symbols = list(symbols)
        for start in range(0, len(symbols), 40):
            batch = symbols[start:start + 40]
            tickers = {}
            for s in batch:
                contract = self.broker._stock(s)
                if contract is not None:
                    tickers[s] = (contract, self.ib.reqMktData(contract, "236", False, False))
            self.ib.sleep(4)
            for s, (contract, t) in tickers.items():
                price = _positive(t.marketPrice(), t.last, t.midpoint(), t.close)
                if price is not None:
                    out[s] = {
                        "price": price, "bid": _num(t.bid), "ask": _num(t.ask),
                        "shortable_level": _num(t.shortable),
                    }
                self.ib.cancelMktData(contract)
        return out

    def limit_order(self, symbol, action, qty, limit, ref):
        from ib_async import LimitOrder
        contract = self.broker._stock(symbol)
        if contract is None:
            return {"status": "no contract", "filled": 0, "avg_fill_price": None}
        order = LimitOrder(action, int(qty), round(float(limit), 2))
        order.orderRef = ref
        order.tif = "DAY"
        trade = self.ib.placeOrder(contract, order)
        return self.broker._wait_for(trade, order)
