"""
lab_events.py
Turns the saved SEC data into "events" the lab can test:
  1. signals   - the exact signals the daily scanner would have produced each day, with the same points
                 (insider clusters, going concern, dilution, late filings, 8-K good and bad news)
  2. forms     - one event per company per day for EVERY form type, to find new signals
  3. live      - the real signals your scanner produced, read from the repo's history (grows every day)
"""

import csv
import io
import re
import subprocess
from datetime import datetime, time as dtime, timedelta

from lab_core import ET, PROJECT, log, parse_day
import lab_sec as S

SPAC_NAME = re.compile(r"\b(?:acquisition|merger|spac|blank check)\s+(?:corp|corporation|co|company|inc|ltd|limited|"
                       r"holdings|holding|i{1,3}|iv|v|vi{0,3})\b|\bacquisition corp", re.IGNORECASE)
SUBJECT_FORMS = re.compile(r"^(?:SC 13[DG]|SCHEDULE 13[DG]|SC TO-T|SC 14D9|144)", re.IGNORECASE)


def is_spac(company, sic=""):
    return str(sic).strip() == "6770" or bool(SPAC_NAME.search(company or ""))


# ---------------------------------------------------------------- 1. the scanner's own signals

def build_signals(start, end, status):
    """One signal per company per filing day, scored exactly like scoring_engine.py (before market data)."""
    import form4_cluster_check as F4
    import scoring_engine as SE
    import sec_daily_watcher as W

    live_forms = set(W.FORM_TYPES) - {"4"}
    companies = {}

    def company(cik, day, name):
        key = (cik, day)
        c = companies.get(key)
        if c is None:
            c = companies[key] = {"cik": cik, "date": day, "company": name, "forms": set(), "eightk": [],
                                  "gc": False, "gc_form": "", "sic": "", "insider": None}
        elif name and not c["company"]:
            c["company"] = name
        return c

    first_8k_owner = {}
    for form, cik, day, accession, name in S.read_index(start, end, keep=lambda f: f in live_forms):
        c = company(cik, day, name)
        c["forms"].add(form)
        if form == "8-K" and accession not in first_8k_owner:
            first_8k_owner[accession] = (cik, day)          # the scanner credits an 8-K to the first company listed
            c["eightk"].append(accession)

    # insider trading, grouped by the company and the day, with the scanner's thresholds
    groups = {}
    for r in S.read_insider(start, end):
        if r.get("saw_sale") != "1" and r.get("saw_buy") != "1":
            continue
        key = (r["issuer_cik"], r["filing_date"])
        g = groups.setdefault(key, {"sellers": {}, "planned": {}, "buyers": {}, "exec_buyers": {}, "shares": 0.0,
                                    "stake_up": 0.0, "symbol": r.get("symbol", "")})
        owner = r.get("owners") or "Unknown"
        if r.get("saw_sale") == "1":
            g["sellers"][owner] = g["sellers"].get(owner, 0.0) + float(r.get("sold_shares") or 0)
            g["planned"][owner] = r.get("planned", "")
        if r.get("saw_buy") == "1":
            dollars = float(r.get("bought_dollars") or 0)
            g["buyers"][owner] = g["buyers"].get(owner, 0.0) + dollars
            if r.get("is_exec") == "1":
                g["exec_buyers"][owner] = g["exec_buyers"].get(owner, 0.0) + dollars
            bought = float(r.get("bought_shares") or 0)
            after = float(r.get("owned_after_buy") or 0)
            g["shares"] += bought
            if after > bought > 0:
                g["stake_up"] = max(g["stake_up"], bought / (after - bought))
            elif after > 0 and bought >= after:
                g["stake_up"] = max(g["stake_up"], 10.0)      # a brand-new holder
    for (cik, day), g in groups.items():
        c = company(cik, day, "")
        c["forms"].add("4")
        real = {o: d for o, d in g["buyers"].items() if d >= F4.MIN_BUYER_DOLLARS}
        exec_best = max(g["exec_buyers"].values(), default=0.0)
        planned = [p for p in g["planned"].values()]
        c["insider"] = {
            "num_sellers": len(g["sellers"]),
            "sell_cluster": len(g["sellers"]) >= 2,
            "sellers_planned": sum(1 for p in planned if p == "1"),
            "sellers_unplanned": sum(1 for p in planned if p == "0"),
            "num_buyers": len(real),
            "buy_cluster": len(real) >= 2,
            "exec_buy": bool(real) and exec_best >= F4.MIN_EXEC_DOLLARS,
            "dollars": sum(real.values()),
            "exec_dollars": exec_best,
            "stake_up": g["stake_up"],
        }

    # going concern: full-text search candidates that the scanner's own rule confirmed when the filing was read
    # (candidates not read yet are left out until a later run reads them)
    checked = S.read_scans("gc")
    rejected, unread = 0, 0
    for r in S.read_going_concern():
        day = r.get("date", "")
        if not (start.isoformat() <= day <= end.isoformat()):
            continue
        flag = checked.get(r["accession"], {}).get("flag")
        if flag != "1":
            rejected += flag == "0"
            unread += flag is None
            continue
        c = company(r["cik"], day, r.get("company", ""))
        c["gc"] = True
        c["gc_form"] = r.get("form", "")
        c["sic"] = r.get("sic", "")
        c["forms"].add(r.get("form", "10-Q"))

    scans = S.read_scans("8k")
    signals = []
    unscanned_8k = 0
    for c in companies.values():
        score, reasons = 0, []
        for form in sorted(c["forms"]):
            points = SE.FORM_TYPE_POINTS.get(form, 0)
            if points:
                score += points
                reasons.append(f"{form} filed (+{points})")
        ins = c["insider"]
        if ins and ins["sell_cluster"]:
            n = ins["num_sellers"]
            points = min(25 + max(0, n - 2) * 5, 40)
            score += points
            reasons.append(f"{n} insiders sold stock (+{points})")
        if c["gc"]:
            score += 35
            reasons.append("Going concern language in filing (+35)")
        buy = ins if ins and (ins["buy_cluster"] or ins["exec_buy"]) else None
        if buy:
            if buy["buy_cluster"]:
                points = min(35 + max(0, buy["num_buyers"] - 2) * 5, 50)
                score += points
                reasons.append(f"{buy['num_buyers']} insiders bought stock on the open market, about "
                               f"${buy['dollars']:,.0f} (+{points})")
                if buy["exec_buy"]:
                    score += 10
                    reasons.append(f"A CEO, CFO or president was one of the buyers, about ${buy['exec_dollars']:,.0f} (+10)")
            else:
                score += SE.EXEC_BUY_POINTS
                reasons.append(f"Executive bought stock (CEO/CFO/president), about ${buy['exec_dollars']:,.0f} "
                               f"(+{SE.EXEC_BUY_POINTS})")
        good, bad, missing = set(), set(), 0
        for accession in c["eightk"]:
            row = scans.get(accession)
            if row is None:
                missing += 1
                continue
            good.update(x for x in row.get("good", "").split(";") if x)
            bad.update(x for x in row.get("bad", "").split(";") if x)
        if missing:
            unscanned_8k += 1
        if good:
            points = min(25 * len(good), 40)
            score += points
            reasons.append(f"Positive 8-K news: {', '.join(sorted(good))} (+{points})")
        if bad:
            points = min(sum(SE.NEGATIVE_8K_POINTS.get(x, 10) for x in bad), 45)
            score += points
            reasons.append(f"Negative 8-K news: {', '.join(sorted(bad))} (+{points})")
        if score <= 0:
            continue
        signals.append({
            "cik": c["cik"], "date": parse_day(c["date"]), "company": c["company"],
            "forms": sorted(c["forms"]), "reasons": reasons, "score": score,
            "bullish": bool(buy or good), "insider": ins, "gc": c["gc"], "gc_form": c["gc_form"],
            "spac": is_spac(c["company"], c["sic"]), "good": sorted(good), "bad": sorted(bad),
            "has_8k": bool(c["eightk"]), "eightk_unread": missing > 0,
        })
    info = status.setdefault("signals", {})
    info["signals"] = len(signals)
    info["going_concern_rejected_on_reread"] = rejected
    info["going_concern_not_read_yet"] = unread
    info["signals_with_unread_8k"] = unscanned_8k
    log(f"Events: {len(signals):,} scanner signals rebuilt ({unscanned_8k:,} still have an 8-K not read yet).")
    return signals


def signal_tags(sig):
    """The tracker's own tags and direction for a rebuilt signal."""
    import analytics as A
    forms = "; ".join(sig["forms"])
    reasons = " | ".join(sig["reasons"])
    direction, tags, _ = A.classify_signal(forms, reasons, sig.get("company", ""))
    return direction, tags


# ---------------------------------------------------------------- 2. every form type

def build_form_events(start, end, tickers):
    """{(form, cik, day)} for every filing by a company that has (or had) a ticker. For forms filed ABOUT another
    company (13D, 13G, 144, tender offers), the filer itself is dropped when the filing also names the company."""
    events = set()
    about = {}
    for form, cik, day, accession, _ in S.read_index(start, end):
        if not tickers.known(cik):
            if SUBJECT_FORMS.match(form):
                about.setdefault(accession, []).append((form, cik, day))
            continue
        if SUBJECT_FORMS.match(form):
            about.setdefault(accession, []).append((form, cik, day))
        else:
            events.add((form, cik, day))
    for accession, rows in about.items():
        filer = accession[:10]
        named = [r for r in rows if r[1] != filer] if len({r[1] for r in rows}) > 1 else rows
        for form, cik, day in named:
            if tickers.known(cik):
                events.add((form, cik, day))
    log(f"Events: {len(events):,} company-days across every form type.")
    return events


# ---------------------------------------------------------------- 3. the real signals, from the repo's history

def live_signals(status):
    """The ranked list your scanner saved each morning, from git history. Each list is matched to the trading day
    the tracker would have used it (a list saved before 9:35 AM New York time is used that morning)."""
    info = status.setdefault("live_record", {})
    try:
        out = subprocess.run(["git", "-C", PROJECT, "log", "--format=%H %ct", "--author=github-actions",
                              "--", "daily_signals_ranked.csv"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        info["problem"] = f"Could not read the repo history ({type(e).__name__})."
        return []
    if out.returncode != 0:
        info["problem"] = "Could not read the repo history (git said: " + out.stderr.strip()[:120] + ")."
        return []
    by_day = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ts = parts[0], int(parts[1])
        when = datetime.fromtimestamp(ts, ET)
        day = when.date()
        if when.time() >= dtime(9, 35) or day.weekday() >= 5:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
        if day not in by_day or by_day[day][1] < ts:
            by_day[day] = (sha, ts)
    signals = []
    for day, (sha, _) in sorted(by_day.items()):
        shown = subprocess.run(["git", "-C", PROJECT, "show", f"{sha}:daily_signals_ranked.csv"],
                               capture_output=True, text=True, timeout=120)
        if shown.returncode != 0:
            continue
        for r in csv.DictReader(io.StringIO(shown.stdout)):
            try:
                score = int(float(r.get("score") or 0))
            except ValueError:
                continue
            signals.append({"day": day, "ticker": (r.get("ticker") or "").strip().upper(), "company": r.get("company", ""),
                            "score": score, "forms": r.get("form_types", ""), "reasons": r.get("reasons", ""),
                            "filed": parse_day(r.get("date_filed"))})
    info["days"] = len(by_day)
    info["signals"] = len(signals)
    log(f"Events: {len(signals):,} real signals from {len(by_day)} saved daily lists.")
    return signals
