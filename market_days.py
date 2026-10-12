"""
market_days.py
Which days the US stock market is open (New York Stock Exchange calendar), so the tracker never tries to
trade on a holiday and knows which day was the last full trading day.

The dates come from the NYSE's own holiday list (published December 2025). After 2028 the list needs new
dates; until then every weekday counts as a trading day and the email says so.
"""

from datetime import date, timedelta

HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
    # 2028 (no New Year's Day holiday: January 1, 2028 is a Saturday)
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
    "2028-11-23", "2028-12-25",
}

EARLY_CLOSE = {          # the market closes at 1:00 PM Eastern on these days
    "2026-11-27", "2026-12-24",
    "2027-11-26",
    "2028-07-03", "2028-11-24",
}

KNOWN_THROUGH = "2028-12-31"


def _iso(d):
    return d if isinstance(d, str) else d.isoformat()


def _date(d):
    return date.fromisoformat(d) if isinstance(d, str) else d


def is_holiday(d):
    return _iso(d) in HOLIDAYS


def is_trading_day(d):
    """True on weekdays that are not market holidays."""
    return _date(d).weekday() < 5 and not is_holiday(d)


def previous_trading_day(d):
    """The last trading day BEFORE d (d itself is not counted)."""
    day = _date(d) - timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def close_minutes(d):
    """When the market closes on day d, in minutes after midnight Eastern (4:00 PM, or 1:00 PM on early-close days)."""
    return 13 * 60 if _iso(d) in EARLY_CLOSE else 16 * 60


def calendar_is_current(d):
    """False once the holiday list above has run out (then holidays are not known)."""
    return _iso(d) <= KNOWN_THROUGH
