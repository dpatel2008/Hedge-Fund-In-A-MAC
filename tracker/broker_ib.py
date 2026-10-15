"""
broker_ib.py
The only file that talks to Interactive Brokers. Uses the free ib_async library.
Needs IB Gateway (or Trader Workstation) running and logged in to a PAPER account.
"""

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ib_async import ComboLeg, Contract, IB, LimitOrder, MarketOrder, Option, Stock

import config as C

SYNC_SECONDS = 20          # time allowed to load the account's positions, orders and values when connecting
REQUEST_TIMEOUT = 90       # no single question to IBKR may wait longer than this (stops a run from hanging forever)
HISTORY_TIMEOUT = 30       # price history for one stock: give up after this long (it normally takes 1-2 seconds)
CONNECT_TRIES = 2
QUICK_ANSWER_SECONDS = 15  # a stock lookup that failed faster than this was a real "no" (or several listings), not silence
US_EXCHANGES = ("ARCA", "NYSE", "NASDAQ", "AMEX", "BATS")   # preferred listings when a symbol has more than one
OUR_ORDER_TAGS = ("SECW-", "SLEEVE-")    # every order this project places has an order reference starting with one of these


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


def _why(trade, prefix=""):
    """IBKR's last message about an order (for example "Order held while securities are located."), or ""."""
    try:
        for entry in reversed(trade.log or []):
            message = str(getattr(entry, "message", "") or "").strip()
            if message:
                for start in ("Warning ", "Error "):            # "Warning 404, reqId 214: Order held while..."
                    if message.startswith(start) and ": " in message:
                        message = message.split(": ", 1)[1]
                return prefix + message[:160]
    except Exception:
        pass
    return ""


def _ib_symbol(symbol):
    return symbol.replace("-", " ").replace(".", " ")


class IBBroker:
    def __init__(self, request_timeout=None):
        self.ib = IB()
        self.ib.RequestTimeout = REQUEST_TIMEOUT if request_timeout is None else request_timeout
        self.account_id = ""
        self._stocks = {}
        self.history_log = []      # when each price-history request was sent (the sleeves count these too)
        self.connect_problem = ""

    # ------------------------------------------------------------ connection

    def connect(self):
        """Connects and makes sure the account's values and positions have fully loaded.
        First it asks ib_async to load everything (positions, orders, fills, account values) and to complain if any
        part is late. If the gateway answers but some part keeps being late, the last try connects anyway and
        checks the two parts that matter (the account value and the positions) itself.
        Raises SystemExit if that does not work (IB Gateway closed, not logged in, or not answering)."""
        problems = []
        gateway_answered = False
        for attempt in range(CONNECT_TRIES + 1):
            strict = attempt < CONNECT_TRIES
            if not strict and not gateway_answered:
                break                                  # nothing is listening: a gentler try would not help
            for port in C.IB_PORTS:
                try:
                    self.ib.connect(C.IB_HOST, port, clientId=C.IB_CLIENT_ID, timeout=SYNC_SECONDS,
                                    raiseSyncErrors=strict)
                    break
                except Exception as e:                 # refused, timed out, or part of the account data was late
                    if not isinstance(e, ConnectionRefusedError):
                        gateway_answered = True
                    problems.append(f"port {port}: {type(e).__name__} {e}".strip())
                    try:
                        self.ib.disconnect()
                    except Exception:
                        pass
            if self.ib.isConnected():
                accounts = self.ib.managedAccounts()
                self.account_id = accounts[0] if accounts else ""
                if strict:
                    break
                if self.account_loaded():
                    print("Note: IBKR was slow to send part of the account data (" + "; ".join(problems[-2:]) +
                          "). Connected anyway after checking the account value and positions.")
                    break
                problems.append("connected, but the account value or positions did not load")
                self.disconnect()
            if attempt + 1 <= CONNECT_TRIES:
                time.sleep(5)
        if not self.ib.isConnected():
            self.connect_problem = "; ".join(problems[-4:])
            raise SystemExit(
                "Could not connect to Interactive Brokers.\n"
                "Make sure IB Gateway is open, logged in to your PAPER account, and its API "
                "settings are on (see the setup steps).\n"
                f"Details: {self.connect_problem}"
            )
        self.ib.reqMarketDataType(C.IB_MARKET_DATA_TYPE)
        return self.account_id

    def account_loaded(self):
        """True when the account value is known and, if IBKR says positions exist, they have arrived."""
        try:
            values = self.account_values()
            if not values.get("NetLiquidation"):
                return False
            return not values.get("GrossPositionValue") or bool(self.ib.portfolio())
        except Exception:
            return False

    def disconnect(self):
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

    def is_connected(self):
        try:
            return bool(self.ib.isConnected())
        except Exception:
            return False

    def cancel_leftover_orders(self):
        """Cancels orders this project placed earlier that are still waiting (for example after a run was stopped
        part way). Returns how many were cancelled. Never raises."""
        cancelled = 0
        try:
            for trade in self.ib.openTrades():
                ref = getattr(trade.order, "orderRef", "") or ""
                if ref.startswith(OUR_ORDER_TAGS) and not trade.isDone():
                    self.ib.cancelOrder(trade.order)
                    cancelled += 1
            if cancelled:
                self.ib.sleep(2)
        except Exception:
            pass
        return cancelled

    # ------------------------------------------------------------ account

    def account_values(self):
        """NetLiquidation, GrossPositionValue and a few other numbers, in US dollars.
        Uses the account values IBKR keeps sending since the connection was made (no extra request). Only if
        those are missing does it ask for the account summary."""
        wanted = {
            "NetLiquidation", "TotalCashValue", "GrossPositionValue",
            "AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL",
        }

        def pick(items):
            out = {}
            ranked = sorted(items, key=lambda v: 1 if v.currency == "USD" else 0)    # USD wins over BASE
            for item in ranked:
                if item.tag in wanted and item.currency in ("USD", "BASE", ""):
                    try:
                        out[item.tag] = float(item.value)
                    except (TypeError, ValueError):
                        pass
            return out

        try:
            out = pick(self.ib.accountValues(self.account_id) if self.account_id else self.ib.accountValues())
        except Exception:
            out = {}
        if not out.get("NetLiquidation"):
            try:
                summary = pick(self.ib.accountSummary(self.account_id) if self.account_id else self.ib.accountSummary())
                for key, value in summary.items():
                    out.setdefault(key, value)
                if summary.get("NetLiquidation"):
                    out["NetLiquidation"] = summary["NetLiquidation"]
            except Exception:
                pass
        return out

    def positions(self):
        out = []
        for item in self.ib.portfolio():
            contract = item.contract
            out.append({
                "symbol": contract.symbol.replace(" ", "-"),
                "sec_type": contract.secType,
                "con_id": contract.conId,
                "qty": float(item.position),
                "avg_cost": float(item.averageCost),
                "market_price": _num(item.marketPrice),
                "market_value": _num(item.marketValue),
                "unrealized": _num(item.unrealizedPNL),
                "realized": _num(item.realizedPNL),
            })
        return out

    def todays_fills(self):
        """Every fill IBKR reports for this connection number today: [{"ref", "symbol", "shares" (signed), "price",
        "exec_id"}]. Used to repair the sleeves' records after a run that stopped part way. Never raises."""
        out = []
        try:
            for fill in self.ib.fills():
                ex = fill.execution
                shares = _num(ex.shares) or 0.0
                out.append({
                    "ref": ex.orderRef or "", "symbol": fill.contract.symbol.replace(" ", "-"),
                    "sec_type": fill.contract.secType,
                    "shares": shares if ex.side == "BOT" else -shares,
                    "price": _num(ex.price), "exec_id": ex.execId, "time": str(ex.time),
                })
        except Exception:
            return []
        return out

    # ------------------------------------------------------------ market data

    def _stock(self, symbol):
        if symbol in self._stocks:
            return self._stocks[symbol]
        contract = Stock(_ib_symbol(symbol), "SMART", "USD")
        started = time.time()
        try:
            self.ib.qualifyContracts(contract)
        except Exception:
            pass
        if not contract.conId and time.time() - started < QUICK_ANSWER_SECONDS:
            contract = self._pick_listing(symbol) or contract     # IBKR answered but found several listings
        self._stocks[symbol] = contract if contract.conId else None
        return self._stocks[symbol]

    def _pick_listing(self, symbol):
        """When a symbol matches more than one IBKR listing (some ETFs, like HYG), picks the US-listed one.
        Returns a contract with its conId set, or None."""
        try:
            details = self.ib.reqContractDetails(Stock(_ib_symbol(symbol), "SMART", "USD")) or []
        except Exception:
            return None
        listed = [d.contract for d in details
                  if d.contract and d.contract.secType == "STK" and d.contract.currency == "USD" and d.contract.conId]
        for exchange in US_EXCHANGES:
            for c in listed:
                if c.primaryExchange == exchange:
                    found = Stock(c.symbol, "SMART", "USD", primaryExchange=exchange)
                    found.conId = c.conId
                    found.localSymbol = c.localSymbol
                    found.tradingClass = c.tradingClass
                    return found
        return None

    def snapshot(self, symbol):
        contract = self._stock(symbol)
        if contract is None:
            return None
        ticker = self.ib.reqMktData(contract, "100,101,236", False, False)
        self.ib.sleep(3)
        self.ib.cancelMktData(contract)
        price = _positive(ticker.marketPrice(), ticker.last, ticker.midpoint(), ticker.close)
        if price is None:
            return None
        return {
            "symbol": symbol,
            "price": price,
            "exchange": str(getattr(contract, "primaryExchange", "") or ""),
            "bid": _num(ticker.bid),
            "ask": _num(ticker.ask),
            "volume": _num(ticker.volume),
            "shortable_level": _num(ticker.shortable),
            "shortable_shares": _num(ticker.shortableShares),
            "call_volume": _num(ticker.callVolume),
            "put_volume": _num(ticker.putVolume),
            "call_oi": _num(ticker.callOpenInterest),
            "put_oi": _num(ticker.putOpenInterest),
        }

    def history(self, symbol, days):
        contract = self._stock(symbol)
        if contract is None:
            return []
        self.history_log.append(time.time())
        try:
            bars = self.ib.reqHistoricalData(
                contract, endDateTime="", durationStr=f"{days} D",
                barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1, timeout=HISTORY_TIMEOUT,
            )
        except Exception:
            return []
        out = []
        for bar in bars or []:
            try:
                out.append({
                    "date": str(bar.date)[:10],
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                })
            except (TypeError, ValueError):
                continue
        return out

    def option_chain(self, symbol, price):
        contract = self._stock(symbol)
        if contract is None:
            return None
        try:
            chains = self.ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
        except Exception:
            return None
        chains = [c for c in chains if c.exchange == "SMART"] or list(chains)
        if not chains:
            return None
        chain = max(chains, key=lambda c: (len(c.expirations), len(c.strikes)))

        today = datetime.now(ZoneInfo("America/New_York")).date()
        best = None
        for expiry in sorted(chain.expirations):
            try:
                dte = (datetime.strptime(expiry, "%Y%m%d").date() - today).days
            except ValueError:
                continue
            if dte >= C.OPTION_MIN_DAYS and (best is None or abs(dte - C.OPTION_TARGET_DAYS) < abs(best[1] - C.OPTION_TARGET_DAYS)):
                best = (expiry, dte)
        strikes = sorted(chain.strikes)
        if best is None or not strikes:
            return None
        expiry, dte = best

        def nearest(target):
            return min(strikes, key=lambda k: abs(k - target))

        atm = nearest(price)
        low = nearest(price * (1 - C.OPTION_SPREAD_WIDTH_PCT))
        high = nearest(price * (1 + C.OPTION_SPREAD_WIDTH_PCT))
        wanted = {
            "atm_call": (atm, "C"), "atm_put": (atm, "P"),
            "otm_call": (high, "C"), "otm_put": (low, "P"),
        }
        options = {
            name: Option(
                contract.symbol, expiry, strike, right, "SMART",
                multiplier=chain.multiplier or "100", currency="USD",
                tradingClass=chain.tradingClass,
            )
            for name, (strike, right) in wanted.items()
        }
        try:
            self.ib.qualifyContracts(*options.values())
        except Exception:
            pass

        tickers = {}
        for name, option in options.items():
            if option.conId:
                tickers[name] = self.ib.reqMktData(option, "", False, False)
        self.ib.sleep(4)

        out = {
            "expiry": expiry, "dte": dte, "underlying": contract.symbol,
            "multiplier": chain.multiplier or "100", "trading_class": chain.tradingClass,
        }
        for name in wanted:
            quote = None
            ticker = tickers.get(name)
            if ticker is not None:
                greeks = ticker.modelGreeks
                mid = _positive(
                    ticker.midpoint(),
                    greeks.optPrice if greeks else None,
                    ticker.last,
                    ticker.close,
                )
                if mid is not None:
                    quote = {
                        "strike": wanted[name][0],
                        "right": wanted[name][1],
                        "conId": options[name].conId,
                        "bid": _positive(ticker.bid),
                        "ask": _positive(ticker.ask),
                        "mid": mid,
                        "iv": _num(greeks.impliedVol) if greeks else None,
                        "delta": _num(greeks.delta) if greeks else None,
                        "gamma": _num(greeks.gamma) if greeks else None,
                        "vega": _num(greeks.vega) if greeks else None,
                        "theta": _num(greeks.theta) if greeks else None,
                    }
            out[name] = quote
        for name, ticker in tickers.items():
            self.ib.cancelMktData(options[name])
        return out

    def spread_quote(self, opt):
        """Current value of an open spread or single option: {"mid", "bid", "ask"} per share, or None if no prices."""
        contracts = {}
        keys = ["long_conid"] + (["short_conid"] if opt.get("short_conid") else [])
        for key in keys:
            contract = Contract(conId=int(opt[key]), exchange="SMART")
            try:
                self.ib.qualifyContracts(contract)
            except Exception:
                return None
            contracts[key] = contract
        tickers = {key: self.ib.reqMktData(c, "", False, False) for key, c in contracts.items()}
        self.ib.sleep(4)
        legs = {}
        for key, ticker in tickers.items():
            greeks = ticker.modelGreeks
            legs[key] = {
                "bid": _positive(ticker.bid),
                "ask": _positive(ticker.ask),
                "mid": _positive(
                    ticker.midpoint(), greeks.optPrice if greeks else None, ticker.last, ticker.close
                ),
            }
        for contract in contracts.values():
            self.ib.cancelMktData(contract)
        long_leg = legs["long_conid"]
        if "short_conid" not in legs:
            if long_leg["mid"] is None:
                return None
            return {"mid": long_leg["mid"], "bid": long_leg["bid"], "ask": long_leg["ask"]}
        short_leg = legs["short_conid"]
        if long_leg["mid"] is None or short_leg["mid"] is None:
            return None
        bid = ask = None
        if all(x is not None for x in (long_leg["bid"], long_leg["ask"], short_leg["bid"], short_leg["ask"])):
            bid = long_leg["bid"] - short_leg["ask"]
            ask = long_leg["ask"] - short_leg["bid"]
        return {"mid": long_leg["mid"] - short_leg["mid"], "bid": bid, "ask": ask}

    # ------------------------------------------------------------ orders

    def _wait_for(self, trade, order, wait_seconds=None):
        limit = wait_seconds or C.ORDER_WAIT_SECONDS
        waited = 0
        while not trade.isDone() and waited < limit:
            self.ib.sleep(1)
            waited += 1
            if waited % 30 == 0 and waited < limit:          # a sign of life for the 20-minute safety stop
                print(f"    still waiting for IBKR to fill it ({waited}s){_why(trade, ' - ')}")
        if not trade.isDone():
            self.ib.cancelOrder(order)
            self.ib.sleep(2)
        status = trade.orderStatus
        return {
            "status": status.status,
            "filled": int(status.filled),
            "avg_fill_price": _positive(status.avgFillPrice),
            "why": _why(trade),
        }

    def place_market(self, symbol, action, qty, ref, wait_seconds=None):
        """wait_seconds: how long to wait for the fill before cancelling (default ORDER_WAIT_SECONDS). New shorts wait
        longer: IBKR may hold them a minute or two while it finds shares to borrow."""
        contract = self._stock(symbol)
        if contract is None:
            return {"status": "no contract", "filled": 0, "avg_fill_price": None}
        order = MarketOrder(action, qty)
        order.orderRef = ref
        order.tif = "DAY"
        trade = self.ib.placeOrder(contract, order)
        return self._wait_for(trade, order, wait_seconds)

    def place_spread(self, opt, action, qty, limit, ref):
        """BUY opens the spread (buy the near leg, sell the far leg). SELL closes it.
        The limit is the price per share of the whole spread. A single option (no short leg) is
        bought or sold on its own."""
        if not opt.get("short_conid"):
            contract = Contract(
                conId=int(opt["long_conid"]), secType="OPT", exchange="SMART", currency="USD"
            )
            order = LimitOrder(action, qty, round(max(limit, 0.01), 2))
            order.orderRef = ref
            order.tif = "DAY"
            return self._wait_for(self.ib.placeOrder(contract, order), order)
        legs = [
            ComboLeg(conId=int(opt["long_conid"]), ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=int(opt["short_conid"]), ratio=1, action="SELL", exchange="SMART"),
        ]
        combo = Contract(
            symbol=opt["underlying"], secType="BAG", currency="USD", exchange="SMART", comboLegs=legs
        )
        order = LimitOrder(action, qty, round(max(limit, 0.01), 2))
        order.orderRef = ref
        order.tif = "DAY"
        trade = self.ib.placeOrder(combo, order)
        return self._wait_for(trade, order)
