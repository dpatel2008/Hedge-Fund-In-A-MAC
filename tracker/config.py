"""
config.py
Every setting for the tracker lives here. Change numbers here and nowhere else.
"""

import os

# Where the daily ranked list comes from (your GitHub repo must be public).
SIGNALS_URL = (
    "https://raw.githubusercontent.com/dpatel2008/sec-filing-watcher/"
    "main/daily_signals_ranked.csv"
)
# Saved next to the tracker folder, whatever folder you run the tracker from.
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tracker_data")

# The Lovable dashboard (your trades and the money made). The morning email links to it. "" leaves the link out.
DASHBOARD_URL = "https://signal-watcher-pro.lovable.app/portfolio"

# Interactive Brokers connection (paper account only).
IB_HOST = "127.0.0.1"
IB_PORTS = [4002, 7497]      # 4002 = IB Gateway paper, 7497 = Trader Workstation paper
IB_CLIENT_ID = 17
IB_MARKET_DATA_TYPE = 3      # 3 = delayed (free), 1 = live (needs a subscription)

# Which signals to trade.
MIN_SCORE_TO_TRADE = 35          # bearish signals (shorts)
MIN_SCORE_TO_TRADE_LONG = 35     # bullish signals (buys)
TRADE_LONGS = False              # buys (insider buying, good-news 8-Ks): off since Oct 15, 2026. The Signal Lab's
                                 # 2-year test: buys lost about 0.2% per trade after costs, shorts made about 1.8%.
SHORT_ONLY_WITH_OTHER_SIGNALS = ("INSIDER_SELLING",)   # shorts from this alone lost money (Signal Lab), so it needs
                                                      # another bearish signal (dilution, late filing, bad 8-K...)
SKIP_SPAC_GOING_CONCERN = True   # going-concern warnings from SPACs (blank-check shells) lost money (Signal Lab)
MAX_SIGNAL_AGE_DAYS = 5
MAX_CANDIDATES_TO_ANALYZE = 40
MAX_NEW_TRADES_PER_RUN = 8       # all new trades together
MAX_NEW_SHORTS_PER_RUN = 8       # of those, at most this many shorts
MAX_NEW_LONGS_PER_RUN = 3        # and at most this many longs (only if TRADE_LONGS is True)
MAX_OPEN_POSITIONS = 20          # room to use the tracker's whole budget (TRACKER_MAX_GROSS_PCT in sleeve_config.py)
TRADE_13D_LONGS = False      # the SEC list can show the filer instead of the target

# Liquidity filters.
MIN_PRICE = 2.0
MIN_AVG_DOLLAR_VOLUME = 500_000
MAX_PCT_OF_DAILY_VOLUME = 0.05
HISTORY_VOLUME_MULTIPLIER = 1     # IBKR on this account reports volume in shares

# Sizing and exits.
RISK_PER_TRADE_PCT = 0.005   # risk 0.5% of account value per trade
MAX_POSITION_PCT = 0.05      # never more than 5% of account value in one stock
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 3.0
HOLD_CALENDAR_DAYS = 14
DEFAULT_ATR_PCT = 0.05       # used only when price history is missing

# Shorting.
ALLOW_UNKNOWN_SHORTABLE = True
MIN_SHORTABLE_LEVEL = 2.5    # IBKR: above 2.5 easy to borrow, 1.5 to 2.5 only if shares can be found, below 1.5 not
                             # available. 2.5 = easy-to-borrow only (Oct 15, 2026: "maybe" names were held, then cancelled)
SHORT_ORDER_WAIT_SECONDS = 120   # a new short may wait this long for its fill (IBKR can hold it while it finds shares)
SKIP_EXCHANGES = ("PINK", "OTC", "OTCBB", "OTCQB", "OTCQX", "GREY", "EXPM")   # over-the-counter listings: not traded

# Hedge and benchmark.
HEDGE_ENABLED = True
HEDGE_SYMBOL = "IWM"
BENCHMARK_SYMBOL = "SPY"
MIN_HEDGE_DOLLARS = 1000
HEDGE_MAX_TO_NET = 1.0       # the hedge is never bigger than the tracker's net long/short dollars (1.0 = dollar for
                             # dollar). Without it, a short-only book of jumpy small stocks could need IWM worth 2x the shorts.
DEFAULT_BETA = 1.2
HISTORY_DAYS = 120
MIN_RETURNS_FOR_BETA = 20

# Option spreads (paper account only). A spread's worst case is the money paid for it.
OPTIONS_ENABLED = True
OPTION_TARGET_DAYS = 30          # pick the expiry closest to this many days away
OPTION_MIN_DAYS = 14
OPTION_SPREAD_WIDTH_PCT = 0.10   # the second leg is 10% away from the stock price
OPTION_MAX_CANDIDATES = 10
OPTION_RISK_PCT = 0.0025         # spend at most 0.25% of the account on each spread
OPTION_MAX_CONTRACTS = 25
OPTION_MAX_OPEN = 20             # most spreads open at once
OPTION_MIN_DEBIT = 0.15          # skip spreads costing under $0.15 a share
OPTION_MIN_REWARD_RISK = 1.0     # skip unless the best case is at least as big as the cost
OPTION_MAX_LEG_GAP_PCT = 0.5     # skip if a leg's bid/ask gap is over 50% of its price
OPTION_LIMIT_SLIPPAGE = 0.05     # buy up to 5% above the middle price, sell down to 5% below
OPTION_TAKE_PROFIT_PCT = 0.75    # close at 75% of the best case
OPTION_STOP_LOSS_PCT = 0.60      # close after losing 60% of what was paid
OPTION_CLOSE_DTE = 5             # close when this few days are left before expiry

# Plain long calls and puts (instead of a spread) for the strongest signals. Worst case is the money paid.
OPTION_SINGLE_ENABLED = True
OPTION_SINGLE_MIN_SCORE = 50         # signals scoring this much or more get a single call or put
OPTION_SINGLE_MIN_PRICE = 0.30       # skip options costing under $0.30 a share
OPTION_SINGLE_TAKE_PROFIT_PCT = 1.0  # close after the option doubles (gains 100%)
OPTION_SINGLE_STOP_LOSS_PCT = 0.50   # close after losing half of what was paid

ORDER_WAIT_SECONDS = 30
