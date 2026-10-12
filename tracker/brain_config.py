"""
brain_config.py
Settings for the Market Brain (market_brain.py). Change numbers here and nowhere else.

The brain has three parts. Each one has a switch:
  "off"   = not used and not shown
  "watch" = worked out every day and shown in the email, but it does NOT change any trade
  "on"    = it changes the trades
Run the test first (python tracker/run_brain.py test). Only turn a part "on" if the test says it helps.
"""

WEATHER = "watch"     # Part 1: turns the stock strategies up or down with the market's "weather"
TILT = "watch"        # Part 2: moves money toward the strategy that has done best in this kind of weather
TUNE = "watch"        # Part 3: every 3 months, re-picks each strategy's settings from its recent record

# ---------------------------------------------------------------- market data
HISTORY_FOLDER = "market_history"     # inside tracker_data; keeps every day ever downloaded (never trimmed)
MARKET = "SPY"
SECTOR_ETFS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]   # the 9 original sector funds (since 1998)
CREDIT = ("HYG", "IEF")               # junk bonds vs safe Treasury bonds: junk falling behind = trouble ahead
FETCH_TRY_YEARS = [30, 25, 20, 15, 10, 5]   # ask IBKR for this many years, longest first, keep the first that works

# ---------------------------------------------------------------- the weather score (no settings here are fitted)
TREND_DAYS = 200          # is SPY above its 200-day average?
MOMENTUM_DAYS = 252       # is SPY up over the last year?
VOL_DAYS = 20             # how jumpy has SPY been over the last month?
VOL_CALM = 0.15           # yearly volatility at or under 15% counts as fully calm
VOL_STORM = 0.35          # ...at or over 35% counts as fully stormy
BREADTH_DAYS = 200        # how many sectors are above their own 200-day average?
CREDIT_DAYS = 100         # are junk bonds doing better or worse than Treasuries lately?
SUNNY_AT = 0.6            # score of 0.6 or more = sunny
FAIR_AT = 0.1             # 0.1 up to 0.6 = fair
CLOUDY_AT = -0.4          # -0.4 up to 0.1 = cloudy, under -0.4 = stormy

# ---------------------------------------------------------------- Part 1: the weather dial
WEATHER_SLEEVES = ("momentum", "meanrev", "sector")   # the stock-buying strategies it turns up or down
LEARN_MIN_DAYS = 756      # needs 3 years of history before it learns anything (until then: normal size)
LEARN_SHRINK_DAYS = 1260  # doubts small samples: 5 years of days pull an estimate halfway back to the starting guess
PRIOR_RETURN = 0.07       # the starting guess for the market's yearly return in any weather (its long-run average)
DIAL_MIN = 0.25           # never less than a quarter of the normal size
DIAL_MAX = 1.25           # never more than 1.25 times the normal size (the extra comes out of T-bills)

# ---------------------------------------------------------------- Part 2: the strategy tilt
TILT_MIN_DAYS = 504       # needs 2 years of a strategy's record first
TILT_SHRINK_DAYS = 500
TILT_STRENGTH = 0.25      # how strongly a better record moves money
TILT_MIN = 0.7            # a strategy keeps at least 0.7 times its share
TILT_MAX = 1.3            # ...and gets at most 1.3 times. Money only moves between strategies.

# ---------------------------------------------------------------- Part 3: settings tuning
TUNE_EVERY_MONTHS = 3     # re-pick in January, April, July and October
TUNE_WINDOW_DAYS = 756    # judge each choice on its last 3 years
TUNE_MIN_DAYS = 504       # need at least 2 years to judge
TUNE_MARGIN = 0.20        # a different setting must beat the normal one by 0.20 Sharpe to be used
TUNE_KEEP_MARGIN = 0.10   # keep the current pick unless another one beats it by 0.10
CANDIDATES = {            # the normal settings in sleeve_config.py are always in the running too
    "trend": [{"sma_days": 150}, {"sma_days": 250}],
    "momentum": [{"lookback_days": 126}, {"top_n": 15}, {"lookback_days": 126, "top_n": 15}],
    "meanrev": [{"entry_rsi": 5.0}, {"entry_rsi": 15.0}],
    "sector": [{"lookback_days": 63}, {"lookback_days": 252}],
    "pairs": [{"entry_z": 1.5}, {"entry_z": 2.5}],
}

# ---------------------------------------------------------------- the test (run_brain.py test)
TEST_COST_BPS = 5.0           # trading cost per dollar traded
SPY_DIVIDEND_YIELD = 0.018    # IBKR prices leave out dividends; this adds SPY's back, roughly
HELPS_SHARPE = 0.05           # a part "helps" if it raises the Sharpe ratio by at least this...
HELPS_MAX_DD_WORSE = 0.02     # ...and does not make the worst drop more than 2 points deeper
