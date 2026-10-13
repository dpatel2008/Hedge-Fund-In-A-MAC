"""
weather_test.py
Does New York's weather line up with how the stock market does? Research found a small "sunshine effect" (traders
are in a better mood on sunny mornings), so this tests it on your own data, the careful way:
  * weather: New York City (Central Park, near the stock exchanges), every day since 1990, from Open-Meteo
    (free, no sign-up; ERA5 weather data, CC BY 4.0)
  * market: the S&P 500 fund SPY from the Market Brain's saved history on this Mac (back to 1993)
  * each kind of weather (sunny, cloudy, rainy, snowy, much warmer or colder than normal) is compared with all
    other days, after taking out the normal ups and downs of each month of the year
  * "same day" is what the research measured; "next day" is what you could actually trade (you only know today's
    weather once today is over)
The weather is saved in tracker_data/weather, and each run only downloads the days that are new.

  python lab/weather_test.py              (from the project folder; IB Gateway is not needed)
  python lab/weather_test.py --no-open    (do not open the page in the browser)
"""

import csv
import html
import math
import os
import subprocess
import sys
import time
from datetime import date, timedelta

import requests

FOLDER = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(FOLDER)
sys.path.insert(0, os.path.join(PROJECT, "tracker"))

import config as C  # noqa: E402
import market_brain as M  # noqa: E402

LAT, LON = 40.7812, -73.9665          # Central Park, New York
WEATHER_URL = os.environ.get("WEATHER_URL", "https://archive-api.open-meteo.com/v1/archive")
WEATHER_DIR = os.path.join(C.DATA_DIR, "weather")
WEATHER_FILE = os.path.join(WEATHER_DIR, "new_york_daily.csv")
REPORT_FILE = os.path.join(WEATHER_DIR, "weather_report.html")
RESULTS_FILE = os.path.join(WEATHER_DIR, "weather_results.csv")
FIELDS = ["date", "morning_cloud", "sunshine_hours", "daylight_hours", "rain_mm", "snow_cm", "temp_max_c", "temp_min_c"]
FIRST_DAY = date(1990, 1, 1)
DELAY_DAYS = 6                        # the weather service adds each day about 5 days later
CHUNK_YEARS = 4
SPLIT_DAY = date(2010, 1, 1)          # older half vs newer half


# ---------------------------------------------------------------- weather

def load_weather():
    rows = {}
    try:
        with open(WEATHER_FILE, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    rows[r["date"]] = r
    except (OSError, csv.Error):
        pass
    return rows


def save_weather(rows):
    os.makedirs(WEATHER_DIR, exist_ok=True)
    tmp = WEATHER_FILE + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, WEATHER_FILE)


def _get(params, tries=4):
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(WEATHER_URL, params=params, timeout=180)
            if resp.status_code == 200:
                return resp.json()
            print(f"  The weather service answered {resp.status_code}: {resp.text[:150]}")
        except (requests.RequestException, ValueError) as e:
            print(f"  Could not reach the weather service ({type(e).__name__}).")
        if attempt < tries:
            time.sleep(10 * attempt)
    return None


def download(start, end):
    """Daily New York weather from start to end, plus the average cloud cover from 6 to 10 AM."""
    params = {
        "latitude": LAT, "longitude": LON, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,sunshine_duration,"
                 "daylight_duration",
        "hourly": "cloud_cover", "timezone": "America/New_York",
    }
    data = _get(params)
    if not data or "daily" not in data:
        return None
    morning = {}
    hourly = data.get("hourly") or {}
    for t, c in zip(hourly.get("time") or [], hourly.get("cloud_cover") or []):
        if c is None or len(t) < 13:
            continue
        if 6 <= int(t[11:13]) <= 9:
            morning.setdefault(t[:10], []).append(float(c))
    d = data["daily"]
    rows = {}
    for i, day in enumerate(d.get("time") or []):
        def val(key, scale=1.0):
            v = (d.get(key) or [None] * (i + 1))[i]
            return "" if v is None else round(float(v) * scale, 3)
        clouds = morning.get(day)
        rows[day] = {"date": day, "morning_cloud": round(sum(clouds) / len(clouds), 1) if clouds else "",
                     "sunshine_hours": val("sunshine_duration", 1 / 3600), "daylight_hours": val("daylight_duration", 1 / 3600),
                     "rain_mm": val("precipitation_sum"), "snow_cm": val("snowfall_sum"),
                     "temp_max_c": val("temperature_2m_max"), "temp_min_c": val("temperature_2m_min")}
    return rows


def update_weather(first_needed):
    """Downloads only the days that are not saved yet. Returns all saved days."""
    rows = load_weather()
    last_ready = date.today() - timedelta(days=DELAY_DAYS)
    start = first_needed
    if rows:
        newest = date.fromisoformat(max(rows))
        oldest = date.fromisoformat(min(rows))
        start = newest + timedelta(days=1) if oldest <= first_needed else first_needed
    if start > last_ready:
        print(f"Weather: up to date ({len(rows):,} days saved, through {max(rows) if rows else '-'}).")
        return rows
    print(f"Weather: downloading New York weather from {start} to {last_ready}...")
    s = start
    while s <= last_ready:
        e = min(date(s.year + CHUNK_YEARS, 1, 1) - timedelta(days=1), last_ready)
        got = download(s, e)
        if got is None:
            print("  Could not download the weather this time. Using what is saved; run again later for the rest.")
            break
        rows.update(got)
        save_weather(rows)
        print(f"  {s} to {e}: {len(got):,} days")
        s = e + timedelta(days=1)
    return rows


# ---------------------------------------------------------------- the test

def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def market_returns():
    """{date: SPY daily return}, with each calendar month's normal average taken out."""
    hist = M.load_history().get(C.BENCHMARK_SYMBOL if C.BENCHMARK_SYMBOL in M.symbols() else "SPY")
    if not hist or len(hist["dates"]) < 500:
        return None, None
    raw = {}
    for k in range(1, len(hist["dates"])):
        a, b = hist["close"][k - 1], hist["close"][k]
        if a > 0 and b > 0:
            raw[hist["dates"][k]] = b / a - 1
    by_month = {}
    for d, r in raw.items():
        by_month.setdefault(int(d[5:7]), []).append(r)
    month_avg = {m: sum(v) / len(v) for m, v in by_month.items()}
    adjusted = {d: r - month_avg[int(d[5:7])] for d, r in raw.items()}
    return adjusted, raw


def weather_types(weather):
    """Each kind of weather, as a test on one day's saved weather."""
    temps = {}
    for r in weather.values():
        t = _f(r.get("temp_max_c"))
        if t is not None:
            temps.setdefault(int(r["date"][5:7]), []).append(t)
    normal = {m: sum(v) / len(v) for m, v in temps.items()}
    shares = sorted(s for s in (_sun_share(r) for r in weather.values()) if s is not None)
    low = shares[len(shares) // 5] if shares else 0
    high = shares[len(shares) * 4 // 5] if shares else 1

    def anomaly(r):
        t = _f(r.get("temp_max_c"))
        return None if t is None else t - normal.get(int(r["date"][5:7]), t)

    return [
        ("Sunny morning (clouds under 25% from 6 to 10 AM)", lambda r: _lt(r, "morning_cloud", 25)),
        ("Partly cloudy morning (25% to 75%)", lambda r: _between(r, "morning_cloud", 25, 75)),
        ("Cloudy morning (clouds over 75%)", lambda r: _ge(r, "morning_cloud", 75)),
        ("Sunniest 20% of days (sunshine vs. daylight)", lambda r: (_sun_share(r) or -1) >= high),
        ("Gloomiest 20% of days", lambda r: _sun_share(r) is not None and _sun_share(r) <= low),
        ("Rainy day (5+ mm of rain)", lambda r: _ge(r, "rain_mm", 5)),
        ("Dry day (no rain)", lambda r: _lt(r, "rain_mm", 0.1)),
        ("Snow day (1+ cm of snow)", lambda r: _ge(r, "snow_cm", 1)),
        ("Much warmer than normal for the month (+5 C / +9 F)", lambda r: (anomaly(r) or 0) >= 5),
        ("Much colder than normal for the month (-5 C / -9 F)", lambda r: (anomaly(r) or 0) <= -5),
    ]


def _sun_share(r):
    s, d = _f(r.get("sunshine_hours")), _f(r.get("daylight_hours"))
    return s / d if s is not None and d else None


def _lt(r, k, x):
    v = _f(r.get(k))
    return v is not None and v < x


def _ge(r, k, x):
    v = _f(r.get(k))
    return v is not None and v >= x


def _between(r, k, lo, hi):
    v = _f(r.get(k))
    return v is not None and lo <= v < hi


def compare(days, returns, test, weather):
    """Average return on days with this weather vs. all other days, with a t-stat and the two halves."""
    yes, no, yes_old, no_old, yes_new, no_new = [], [], [], [], [], []
    for d, r in days:
        w = weather.get(d)
        if w is None:
            continue
        hit = test(w)
        (yes if hit else no).append(r)
        if d < SPLIT_DAY.isoformat():
            (yes_old if hit else no_old).append(r)
        else:
            (yes_new if hit else no_new).append(r)
    if len(yes) < 30 or len(no) < 30:
        return {"days": len(yes)}
    my, mn = sum(yes) / len(yes), sum(no) / len(no)
    vy = sum((x - my) ** 2 for x in yes) / (len(yes) - 1)
    vn = sum((x - mn) ** 2 for x in no) / (len(no) - 1)
    se = math.sqrt(vy / len(yes) + vn / len(no))
    diff = my - mn

    def half(a, b):
        return (sum(a) / len(a) - sum(b) / len(b)) if len(a) >= 15 and len(b) >= 15 else float("nan")

    return {"days": len(yes), "avg": my, "others": mn, "diff": diff, "t": diff / se if se > 0 else float("nan"),
            "yearly": diff * 252, "old": half(yes_old, no_old), "new": half(yes_new, no_new)}


def verdict(s):
    if s.get("days", 0) < 30 or "t" not in s:
        return "TOO FEW"
    same = s["old"] == s["old"] and s["new"] == s["new"] and (s["old"] > 0) == (s["new"] > 0) == (s["diff"] > 0)
    if abs(s["t"]) >= 3 and same:
        return "REAL"
    if abs(s["t"]) >= 2:
        return "MAYBE"
    return "LUCK"


def run():
    adjusted, raw = market_returns()
    if adjusted is None:
        print("The Market Brain's market history is not saved on this Mac yet.")
        print("With IB Gateway open, run:  python tracker/run_brain.py fetch   then run this test again.")
        return 1
    dates = sorted(adjusted)
    first_market = date.fromisoformat(dates[0])
    weather = update_weather(max(FIRST_DAY, first_market - timedelta(days=10)))
    if len(weather) < 500:
        print("Not enough weather saved yet to test anything. Try again later.")
        return 1
    same_day = [(d, adjusted[d]) for d in dates]
    next_day = [(dates[k - 1], adjusted[dates[k]]) for k in range(1, len(dates))]   # today's weather, tomorrow's return
    results = []
    for name, test in weather_types(weather):
        a = compare(same_day, adjusted, test, weather)
        b = compare(next_day, adjusted, test, weather)
        results.append((name, a, verdict(a), b, verdict(b)))
    covered = [d for d in dates if d in weather]
    write_outputs(results, covered, raw)
    return 0


# ---------------------------------------------------------------- output

def pct(x, digits=3):
    return "-" if x is None or x != x else f"{x * 100:+.{digits}f}%"


def write_outputs(results, covered, raw):
    first, last = covered[0], covered[-1]
    years = (date.fromisoformat(last) - date.fromisoformat(first)).days / 365.25
    print()
    print(f"New York weather vs. the S&P 500 (SPY), {first} to {last} ({years:.0f} years, {len(covered):,} trading days)")
    print("Difference in SPY's daily return on days with this weather vs. all other days (normal monthly ups and downs removed)")
    print()
    print(f"{'Weather':54} {'Days':>6} {'Same day':>10} {'t':>6} {'Verdict':>8}   {'Next day':>10} {'t':>6} {'Verdict':>8}")
    for name, a, va, b, vb in results:
        print(f"{name[:54]:54} {a.get('days', 0):>6,} {pct(a.get('diff')):>10} {a.get('t', float('nan')):>6.1f} {va:>8}   "
              f"{pct(b.get('diff')):>10} {b.get('t', float('nan')):>6.1f} {vb:>8}")
    print()
    print("REAL = t-stat 3+ and the same direction in 1993-2009 and 2010 on. MAYBE = t-stat 2 to 3. LUCK = below 2.")
    print("With 10 kinds of weather tested twice, one or two MAYBEs can show up by luck alone.")

    os.makedirs(WEATHER_DIR, exist_ok=True)
    with open(RESULTS_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["weather", "days", "same_day_diff", "same_day_t", "same_day_old_half", "same_day_new_half", "same_day_verdict",
                    "next_day_diff", "next_day_t", "next_day_old_half", "next_day_new_half", "next_day_verdict"])
        for name, a, va, b, vb in results:
            w.writerow([name, a.get("days", 0), a.get("diff"), a.get("t"), a.get("old"), a.get("new"), va,
                        b.get("diff"), b.get("t"), b.get("old"), b.get("new"), vb])

    colors = {"REAL": "#1b7f3b", "MAYBE": "#9a6a00", "LUCK": "#8a8f98", "TOO FEW": "#8a8f98"}
    rows = []
    for name, a, va, b, vb in results:
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td>{a.get('days', 0):,}</td>"
            f"<td>{pct(a.get('diff'))}</td><td>{pct(a.get('yearly'), 1)}</td><td>{_num(a.get('t'))}</td>"
            f"<td>{pct(a.get('old'))}</td><td>{pct(a.get('new'))}</td>"
            f"<td style='color:{colors[va]};font-weight:700'>{va}</td>"
            f"<td>{pct(b.get('diff'))}</td><td>{_num(b.get('t'))}</td>"
            f"<td style='color:{colors[vb]};font-weight:700'>{vb}</td></tr>")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weather and the market</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d1f24;background:#fff;margin:0 auto;
padding:16px;max-width:1000px;line-height:1.45}}
h1{{font-size:22px;margin:0 0 4px}}.note{{color:#5a5f69;font-size:13px}}.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;min-width:100%}}
th,td{{padding:5px 8px;border-bottom:1px solid #e3e5ea;text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left;white-space:normal;min-width:220px}}th{{background:#f4f5f8}}
</style></head><body>
<h1>Weather and the market</h1>
<p class=note>New York City weather vs. the S&amp;P 500 (SPY), {first} to {last}: {years:.0f} years, {len(covered):,} trading days.
Each row compares SPY's daily return on days with that weather against all other days, after removing each month's
normal average. "Same day" is what research measured. "Next day" is what you could actually trade, because you only know a
day's weather once it is over.</p>
<div class=wrap><table><tr><th>Weather</th><th>Days</th><th>Same day: difference</th><th>Per year</th><th>t-stat</th>
<th>1993-2009</th><th>2010 on</th><th>Verdict</th><th>Next day: difference</th><th>t-stat</th><th>Verdict</th></tr>
{''.join(rows)}</table></div>
<p class=note><b>REAL</b> = t-stat 3 or more and the same direction in both halves. <b>MAYBE</b> = t-stat 2 to 3.
<b>LUCK</b> = below 2. Ten kinds of weather are tested twice, so one or two MAYBEs can appear by luck alone.
"Per year" is the daily difference times 252 trading days, before trading costs; trading in and out each day costs
money, so a small difference would not survive.</p>
<p class=note>Weather data: Open-Meteo.com (ERA5, CC BY 4.0). Market data: the Market Brain's saved SPY history.</p>
</body></html>"""
    with open(REPORT_FILE, "w") as f:
        f.write(page)
    print(f"Page saved: {REPORT_FILE}")


def _num(x):
    return "-" if x is None or x != x else f"{x:.1f}"


if __name__ == "__main__":
    code = run()
    if code == 0 and "--no-open" not in sys.argv:
        try:
            subprocess.run(["open", REPORT_FILE], check=False)
        except OSError:
            pass
    sys.exit(code)
