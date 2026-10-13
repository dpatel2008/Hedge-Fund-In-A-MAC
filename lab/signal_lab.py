"""
signal_lab.py
The Signal Lab: measures which signals really make money, the way a big quant fund tests ideas before trading them.
GitHub runs it twice a week (the "Signal Lab" workflow) and emails the report card. It only reads public data and
writes reports in lab/results. It never places orders and never touches the tracker.

Each run:
  1. downloads new daily prices for every US stock (free plan: 2 years, 5 calls a minute)
  2. downloads new SEC data: every filing of every form type, insider trades, going-concern warnings
  3. reads more 8-Ks and 10-K/10-Qs with the daily scanner's exact rules (a little more each run until all are read)
  4. measures every signal, every form type and your real signals so far, and replays the tracker's rules
  5. writes the report card

  python lab/signal_lab.py
"""

import os
import threading
import traceback
from datetime import datetime, timedelta

from lab_core import DATA_DIR, ET, PRICE_YEARS, Budget, SecClient, log, quarters_between, today_et
import lab_events as E
import lab_prices as LP
import lab_report as R
import lab_sec as S
import lab_study as ST

GC_CHECKS_PER_RUN = int(os.environ.get("LAB_GC_CHECKS", "1000000"))   # tests can lower this


def safely(status, name, func, *args):
    """Runs one step. A problem in one step is written down and the rest of the run carries on."""
    try:
        return func(*args)
    except Exception as e:
        log(f"PROBLEM in {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        status["warnings"].append(f"{name} had a problem ({type(e).__name__}: {str(e)[:160]}). The rest of the run "
                                  f"carried on; the next run tries again.")
        return None


def eightk_tasks(start, end):
    """Every 8-K in the period, credited to the first company listed (as the daily scanner does)."""
    seen = {}
    for form, cik, day, accession, _ in S.read_index(start, end, keep=lambda f: f == "8-K"):
        if accession not in seen:
            seen[accession] = cik
    return seen


def main():
    minutes = float(os.environ.get("LAB_MINUTES", "300"))
    budget = Budget(minutes)
    status = {"started": datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z"), "warnings": [], "minutes_allowed": minutes}
    today = today_et()
    default_start = today - timedelta(days=int(PRICE_YEARS * 365.25))
    saved = LP.saved_days()
    start = min([default_start] + saved[:1]) - timedelta(days=10)
    end = today
    log(f"Signal Lab starting. Period: {start.isoformat()} to {end.isoformat()}. Time allowed: {minutes:.0f} minutes.")
    os.makedirs(DATA_DIR, exist_ok=True)

    key = os.environ.get("POLYGON_API_KEY") or os.environ.get("MASSIVE_API_KEY") or ""
    prices = threading.Thread(target=safely, args=(status, "Price download", LP.download_prices, key, budget, status),
                              daemon=True)
    prices.start()

    sec = SecClient()
    safely(status, "SEC filing lists", S.update_indexes, sec, start, end, budget, status)
    safely(status, "SEC insider data", S.update_insider, sec, start, end, budget, status)
    safely(status, "SEC ticker list", S.update_tickers, sec, status)
    safely(status, "Going-concern search", S.update_going_concern, sec, start, end, budget, status)

    # read filings with the daily scanner's rules: going-concern double-checks first, then 8-Ks
    checked = S.read_scans("gc")
    gc_todo = S.ordered([(r["accession"], r["cik"]) for r in S.read_going_concern() if r["accession"] not in checked])
    if gc_todo and budget.downloads_allowed():
        log(f"SEC: double-checking up to {min(len(gc_todo), GC_CHECKS_PER_RUN):,} of {len(gc_todo):,} going-concern filings...")
        counts = safely(status, "Going-concern double-check", S.scan_filings, sec, budget, "gc", gc_todo, GC_CHECKS_PER_RUN)
        if counts:
            log(f"  Read {counts['read']:,} ({counts['failed']:,} could not be downloaded).")
    eightks = safely(status, "8-K list", eightk_tasks, start, end) or {}
    read_8k = S.read_scans("8k")
    todo = S.ordered([(a, c) for a, c in eightks.items() if a not in read_8k])
    if todo and budget.downloads_allowed():
        log(f"SEC: reading 8-Ks with the scanner's rules: {len(todo):,} left of {len(eightks):,} "
            f"(this run reads as many as time allows).")
        counts = safely(status, "8-K reading", S.scan_filings, sec, budget, "8k", todo)
        if counts:
            log(f"  Read {counts['read']:,} ({counts['failed']:,} could not be downloaded).")

    if prices.is_alive():
        log("Waiting for the price download to finish...")
    prices.join()
    status["sec_requests"] = sec.requests
    status["sec_stopped_early"] = sec.stopped
    for k in ("prices", "sec_lists", "insider", "going_concern"):
        problems = (status.get(k) or {}).get("problems") or []
        problem = (status.get(k) or {}).get("problem")
        for p in problems + ([problem] if problem else []):
            status["warnings"].append(p)
    status["warnings"].extend(status.get("problems") or [])
    if (status.get("prices") or {}).get("splits_problem"):
        status["warnings"].append(status["prices"]["splits_problem"])
    if sec.stopped:
        status["warnings"].append("The SEC asked us to slow down several times, so SEC downloads stopped early. "
                                  "The next run continues where this one left off.")

    results = safely(status, "Study", study, start, end, status) or {}
    status["finished"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")
    status["minutes_used"] = round(budget.used_minutes(), 1)
    lines = safely(status, "Report", R.write_report, results, status) or []
    log("Summary:")
    for line in lines:
        log("  " + line)
    log(f"Signal Lab finished in {budget.used_minutes():.0f} minutes.")


def study(start, end, status):
    results = {}
    cov = results["coverage"] = {}
    eightks = eightk_tasks(start, end)
    read_8k = S.read_scans("8k")
    cov["eightk_total"] = len(eightks)
    cov["eightk_read_count"] = sum(1 for a in eightks if a in read_8k)
    cov["eightk_read"] = cov["eightk_read_count"] / len(eightks) if eightks else 0.0
    gc = S.read_going_concern()
    checked = S.read_scans("gc")
    flags = [checked[r["accession"]]["flag"] for r in gc if r["accession"] in checked]
    cov["gc_candidates"] = len(gc)
    cov["gc_checked"] = len(flags)
    cov["gc_confirmed_rate_text"] = f"{sum(f == '1' for f in flags) / len(flags):.0%}" if flags else "-"
    cov["insider_quarters"] = len(S.insider_quarters_covered(start, end))
    cov["insider_quarters_needed"] = len(quarters_between(start, end))

    panel = LP.load_panel()
    if panel is None or len(panel.days) < 40:
        status["warnings"].append("Not enough price days saved yet to measure anything. The price download "
                                  "continues next run (the first full download takes about 2 hours).")
        cov["price_days"] = len(panel.days) if panel else 0
        return results
    cov["price_days"] = len(panel.days)
    cov["splits_applied"] = panel.split_notes["from_list"]
    cov["splits_guessed"] = panel.split_notes["guessed"]
    results["period"] = (panel.days[0].isoformat(), panel.days[-1].isoformat())
    log(f"Study: {len(panel.days)} trading days x {len(panel.tickers):,} symbols loaded.")
    M = ST.Market(panel)
    tickers = S.TickerMap(S.read_insider(start - timedelta(days=400), end))
    first, last = panel.days[0] - timedelta(days=7), panel.days[-1]

    signals = E.build_signals(first, last, status)
    ready = ST.prepare_signals(M, tickers, signals)
    log(f"Study: {len(ready):,} of {len(signals):,} signals matched to a traded stock.")
    results["signals"] = ST.study_signals(M, ready)
    rep = safely(status, "Replay", ST.replay, M, ready)
    if rep:
        results["replay"] = ST.summarize_replay(M, rep)
        results["replay_trades"] = rep["trades"]
    form_events = E.build_form_events(first, last, tickers)
    results["forms"] = safely(status, "Form-type scan", ST.study_forms, M, tickers, form_events) or []
    form_events = None
    live = E.live_signals(status)
    results["live"] = safely(status, "Live record", ST.study_live, M, live) or {}
    return results


if __name__ == "__main__":
    main()
