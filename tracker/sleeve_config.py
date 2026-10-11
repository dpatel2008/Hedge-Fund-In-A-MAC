"""
sleeve_config.py
Every setting for the strategy sleeves and the new risk limits. Change numbers here and nowhere else.
Paper account only.
"""

# ---------------------------------------------------------------- master switches
SLEEVES_ENABLED = True           # False = sleeves never place orders (the "plan" look-only command still works). Change to True to go live, by hand and in the daily auto run
PUBLISH_RESULTS = False           # True = publish results.json to GitHub after each run (see the steps)

# Which sleeves are on, and the share of the account value each one may use at most.
SLEEVE_ENABLED = {
    "trend": True,       # holds broad ETFs only while they are above their 200-day average
    "sector": False,      # holds the 3 strongest sector ETFs each month
    "momentum": True,    # holds the strongest large stocks each month
    "meanrev": True,     # buys sharp 1-3 day drops in strong stocks, sells the bounce
    "pairs": False,       # long one stock, short its twin, when the gap between them is unusually wide
}
SLEEVE_ALLOCATION = {                 # the NORMAL share; automatic shifting moves it between half and 1.5 times this
    "trend": 0.18,
    "sector": 0.10,
    "momentum": 0.14,
    "meanrev": 0.08,
    "pairs": 0.10,
}                                     # together 60% of the account

# ---------------------------------------------------------------- SEC-filing tracker budget
TRACKER_MAX_GROSS_PCT = 0.35          # the tracker's stocks + options (long + short) may use at most 35% of the account.
                                      # 35% tracker + 60% sleeves = 95%, the total limit below.

# ---------------------------------------------------------------- automatic shifting of money between sleeves
DYNAMIC_ALLOCATION = True             # False = every sleeve always gets exactly its normal share
ALLOC_LOOKBACK_DAYS = 300             # replay each strategy over about the last 300 trading days
ALLOC_MIN_DAYS = 120                  # need at least this many replayed days, or the sleeve keeps its normal share
ALLOC_SHORT_VOL_DAYS = 63             # "lately" = the last 3 months
ALLOC_VOL_MULT_MIN = 0.6              # swinging much more than usual: down to 0.6 times
ALLOC_VOL_MULT_MAX = 1.3              # much calmer than usual: up to 1.3 times
ALLOC_LOSER_MULT = 0.8                # lost money over the last year: 0.8 times
ALLOC_WINNER_SHARPE = 0.5             # a year this good or better...
ALLOC_WINNER_MULT = 1.15              # ...gets 1.15 times
ALLOC_DRAWDOWN_LIMIT = 0.15           # 15% or more below its high: cut in half
ALLOC_MIN_MULT = 0.5                  # never less than half the normal share
ALLOC_MAX_MULT = 1.5                  # never more than 1.5 times the normal share

# ---------------------------------------------------------------- idle cash
CASH_SLEEVE_ENABLED = True        # park unused cash in a Treasury bill ETF
CASH_SYMBOL = "SGOV"
CASH_FALLBACK_SYMBOL = "BIL"
CASH_BUFFER_PCT = 0.10            # always keep this share of the account as plain cash
CASH_REBALANCE_BAND_PCT = 0.03    # only trade the T-bill ETF when it is off target by this much of the account

# ---------------------------------------------------------------- risk limits
MAX_TOTAL_GROSS_PCT = 0.95        # tracker trades + sleeves together never exceed this share of the account
DAILY_LOSS_LIMIT_PCT = 0.02       # if the account fell this much since the last saved day, no NEW positions today
MAX_ORDERS_PER_RUN = 40
MIN_ORDER_DOLLARS = 500
REBALANCE_BAND = 0.20             # leave a held position alone if it is within 20% of its target size
SLEEVE_LIMIT_SLIPPAGE = 0.003     # limit orders are placed 0.3% through the quote so they fill
MIN_SHORTABLE_LEVEL = 1.5
ALLOW_UNKNOWN_SHORTABLE = True

# Stop-loss per position for the monthly sleeves (None = no stop). Stopped names wait for the next monthly rebalance.
SLEEVE_STOP_PCT = {"trend": None, "sector": 0.12, "momentum": 0.20, "meanrev": None, "pairs": None}

# Market regime. Stock sleeves that lean long are shrunk when the market looks weak.
REGIME_SMA_DAYS = 200
REGIME_BELOW_SMA_SCALE = 0.5      # momentum and mean reversion use half size when SPY is below its 200-day average
REGIME_STRESSED_VOL = 0.30        # ...and a quarter size when it is also this volatile (20-day, yearly)
REGIME_STRESSED_SCALE = 0.25
REGIME_SCALED_SLEEVES = ("momentum", "meanrev")

# ---------------------------------------------------------------- protection for the SEC-filing tracker (Phase 2)
TRACKER_GUARDS_ENABLED = True
TRACKER_DAILY_LOSS_LIMIT = True       # no new tracker trades when the daily loss limit is hit
TRACKER_BLOCK_LONGS_WHEN_STRESSED = True   # no new tracker BUYS when SPY is below its 50-day average and very volatile
TRACKER_REGIME_SMA_DAYS = 50
MAX_PER_INDUSTRY = 2                  # at most this many tracker trades in the same IBKR industry (open + new)

# ---------------------------------------------------------------- strategy settings
TREND = {"sma_days": 200, "vol_days": 60, "band": 0.01, "max_weight": 0.35}
SECTOR = {"lookback_days": 126, "top_n": 3}
MOMENTUM = {
    "lookback_days": 252, "skip_days": 21, "sma_days": 200, "top_n": 10,
    "vol_days": 60, "max_weight": 0.20, "short_n": 0, "short_gross": 0.5,
}                                      # short_n = 0 keeps the momentum sleeve long-only
MEANREV = {
    "rsi_days": 2, "entry_rsi": 10.0, "exit_rsi": 70.0, "sma_days": 200, "exit_sma_days": 5,
    "max_hold_days": 7, "stop_pct": 0.10, "max_positions": 8,
}
PAIRS = {
    "lookback_days": 120, "z_days": 60, "entry_z": 2.0, "max_entry_z": 3.5, "exit_z": 0.5,
    "stop_z": 4.0, "max_hold_days": 30, "min_corr": 0.6, "max_phi": 0.97, "max_pairs": 6,
}

# ---------------------------------------------------------------- price history
PRICE_YEARS = 3                       # years of daily prices to keep (same download time as 2: one request per stock)
HISTORY_REQUESTS_PER_10_MIN = 55      # IBKR allows 60; stay under it
AUTO_FETCH_SECONDS = 240              # during the automatic run, stop fetching history after this long
MIN_DATA_COVERAGE = 0.7               # a sleeve waits if less than 70% of its stocks have history
MANUAL_CLIENT_ID = 18                 # run_sleeves.py connects with this number (the scheduled run uses 17)
