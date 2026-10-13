"""
lab_report.py
Writes the lab's results: report_card.html (emailed), summary.json and CSV tables in lab/results.
"""

import html
import math
import os

from lab_core import (HORIZONS, MAIN_HORIZON, NEW_IDEA_MIN_T, RESULTS_DIR, SHORT_BORROW_PER_YEAR, START_EQUITY,
                      write_csv, write_json, write_text)

VERDICT_COLORS = {"WORKS": "#1b7f3b", "PROMISING": "#1b7f3b", "WEAK": "#9a6a00", "MAYBE": "#9a6a00",
                  "NO EDGE": "#8a8f98", "NO": "#8a8f98", "TOO FEW": "#8a8f98", "TOO EARLY": "#8a8f98",
                  "LOSES MONEY": "#b3261e"}
VERDICT_WORDS = {
    "WORKS": "made money after costs, clearly enough to trust, in both years",
    "WEAK": "made a little after costs, but not clearly enough to trust yet",
    "NO EDGE": "did not make money after costs",
    "LOSES MONEY": "clearly lost money after costs",
    "TOO FEW": "not enough events to judge",
    "PROMISING": "passed the strict test for a new signal",
    "MAYBE": "looks good but did not pass the strict test",
    "NO": "did not pass",
}


def pct(x, digits=1, sign=True):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "-"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def num(x, digits=1):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "-"
    return f"{x:.{digits}f}"


def esc(x):
    return html.escape(str(x))


def badge(verdict):
    color = VERDICT_COLORS.get(verdict, "#8a8f98")
    return f'<span style="color:{color};font-weight:700">{esc(verdict)}</span>'


def signal_table(rows, title, note, new=False):
    if not rows:
        return ""
    head = ("<tr><th>Signal</th><th>Side</th><th>Events</th><th>Avg after costs</th><th>Win rate</th>"
            "<th>t-stat</th><th>1st half</th><th>2nd half</th><th>Verdict</th></tr>")
    body = []
    for r in rows:
        s = r["stats"][MAIN_HORIZON]
        if s.get("n", 0) == 0:
            body.append(f"<tr><td>{esc(r.get('name') or r.get('form'))}</td><td>{r['side']}</td><td>0</td>"
                        f"<td colspan=5>-</td><td>{badge('TOO FEW')}</td></tr>")
            continue
        body.append(
            f"<tr><td>{esc(r.get('name') or r.get('form'))}</td><td>{r['side']}</td><td>{s['n']:,}</td>"
            f"<td>{pct(s['avg_after_cost'], 2)}</td><td>{pct(s['win_rate'], 0, False)}</td><td>{num(s['t'])}</td>"
            f"<td>{pct(s['first_half'], 2)}</td><td>{pct(s['second_half'], 2)}</td><td>{badge(r['verdict'])}</td></tr>")
    return (f"<h2>{esc(title)}</h2><p class=note>{note}</p><div class=wrap><table>{head}{''.join(body)}</table></div>")


def horizon_table(rows, title):
    if not rows:
        return ""
    head = "<tr><th>Signal</th>" + "".join(f"<th>{h} day{'s' if h > 1 else ''}</th>" for h in HORIZONS) + \
           "<th>Overnight gap</th></tr>"
    body = []
    for r in rows:
        cells = "".join(f"<td>{pct(r['stats'][h].get('avg_after_cost'), 2) if r['stats'][h].get('n') else '-'}</td>"
                        for h in HORIZONS)
        body.append(f"<tr><td>{esc(r.get('name') or r.get('form'))}</td>{cells}<td>{pct(r['stats'].get('gap'), 2)}</td></tr>")
    return (f"<h3>{esc(title)}</h3><p class=note>Average result after costs if the trade is held for each number of "
            f"trading days. \"Overnight gap\" is how far the stock had already moved (in the trade's favor) between "
            f"the last close and the open when the tracker could first trade.</p>"
            f"<div class=wrap><table>{head}{''.join(body)}</table></div>")


def curve_svg(curve, bench_points=None):
    if not curve or len(curve) < 2:
        return ""
    w, h, pad = 640, 220, 34
    series = [("Unhedged", [c[1] for c in curve], "#2f5fd0"), ("Hedged with IWM", [c[2] for c in curve], "#1b7f3b")]
    vals = [v for _, s, _ in series for v in s]
    lo, hi = min(vals + [START_EQUITY]), max(vals + [START_EQUITY])
    if hi - lo < 1:
        hi = lo + 1
    n = len(curve)

    def xy(k, v):
        return pad + (w - 2 * pad) * k / (n - 1), h - pad - (h - 2 * pad) * (v - lo) / (hi - lo)

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px" role="img" '
             f'aria-label="Replay account value over time">']
    y0 = xy(0, START_EQUITY)[1]
    parts.append(f'<line x1="{pad}" x2="{w - pad}" y1="{y0:.1f}" y2="{y0:.1f}" stroke="#c9ccd3" stroke-dasharray="4 4"/>')
    for label, s, color in series:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(k, v) for k, v in enumerate(s)))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
    parts.append(f'<text x="{pad}" y="14" font-size="11" fill="#555">${hi:,.0f}</text>')
    parts.append(f'<text x="{pad}" y="{h - 8}" font-size="11" fill="#555">${lo:,.0f}</text>')
    parts.append(f'<text x="{pad}" y="{h - 20}" font-size="11" fill="#555">{esc(curve[0][0])}</text>')
    parts.append(f'<text x="{w - pad}" y="{h - 20}" font-size="11" fill="#555" text-anchor="end">{esc(curve[-1][0])}</text>')
    parts.append("</svg>")
    legend = " ".join(f'<span style="color:{c};font-weight:700">&#9644; {esc(l)}</span>' for l, _, c in series)
    return "".join(parts) + f"<p class=note>{legend} &nbsp; dashed line = starting $1,000,000</p>"


def headline(results):
    """The plain-English summary at the top."""
    out = []
    rep = results.get("replay")
    if rep:
        un, hd = rep["unhedged"], rep["hedged"]
        bench = rep.get("benchmarks", {})
        out.append(
            f"Following the tracker's exact rules from {rep['first_day']} to {rep['last_day']}, the SEC strategy "
            f"would have turned $1,000,000 into about ${START_EQUITY * (1 + un['total']):,.0f} ({pct(un['total'])}), "
            f"or {pct(hd['total'])} with the IWM hedge. Worst drop along the way: {pct(un['max_drawdown'])} "
            f"(hedged {pct(hd['max_drawdown'])}). Over the same time IWM did {pct(bench.get('IWM'))} and SPY "
            f"{pct(bench.get('SPY'))}. {rep['trades']:,} trades, {pct(rep['win_rate'], 0, False)} of them winners.")
        if results.get("coverage", {}).get("eightk_read", 1) < 0.9:
            out.append(f"The replay is still partial: only {pct(results['coverage']['eightk_read'], 0, False)} of the "
                       f"8-Ks have been read so far, so some good-news and bad-news trades are missing. Each run reads more.")
    sig = results.get("signals") or {}
    works = [r["name"] for r in sig.get("tags", []) if r["verdict"] == "WORKS"]
    bad = [r["name"] for r in sig.get("tags", []) if r["verdict"] in ("LOSES MONEY", "NO EDGE")]
    if works:
        out.append("Signals that clearly worked: " + "; ".join(works) + ".")
    if bad:
        out.append("Signals that did not make money after costs: " + "; ".join(bad) + ".")
    promising = [r for r in results.get("forms", []) if r["verdict"] == "PROMISING"]
    if promising:
        top = ", ".join(f"{r['form']} ({r['side']})" for r in promising[:5])
        out.append(f"{len(promising)} SEC form type(s) passed the strict test for a new signal (t-stat {NEW_IDEA_MIN_T:g}+, "
                   f"both halves, after costs): {top}.")
    ideas = [r["name"] for r in sig.get("ideas", []) if r["verdict"] == "PROMISING"]
    if ideas:
        out.append("New ideas that passed the strict test: " + "; ".join(ideas) + ".")
    live = results.get("live") or {}
    if live.get("measured"):
        out.append(f"Your real signals so far: {live['measured']:,} measured since {live.get('first_day')}. "
                   f"That is too early to judge; the lab keeps adding them every run.")
    sa = results.get("statarb") or {}
    for r in sa.get("methods", [])[:2]:
        out.append(f"Stat arb, {r['name']}: {pct(r['yearly_before'])} a year before costs, {pct(r['yearly_after'])} "
                   f"after, Sharpe {num(r['sharpe_after'], 2)} -> {r['verdict']} ({VERDICT_WORDS.get(r['verdict'], '')}).")
    return out


def statarb_section(sa):
    """The stat-arb (pairs trading) part of the report card."""
    if not sa:
        return ""
    parts = ["<h2>Statistical arbitrage (pairs trading)</h2>"]
    for note in sa.get("notes", []):
        parts.append(f"<p class=note>{esc(note)}</p>")
    rows = sa.get("methods") or []
    if rows:
        head = ("<tr><th>Method</th><th>Trades</th><th>Winners</th><th>Avg days held</th><th>Per year before costs</th>"
                "<th>Per year after costs</th><th>Sharpe after costs</th><th>Worst drop</th><th>1st half</th>"
                "<th>2nd half</th><th>Market beta</th><th>Verdict</th></tr>")
        body = []
        for r in rows:
            body.append(
                f"<tr><td>{esc(r['name'])}<br><span class=note>{esc(r['what'])}</span></td><td>{r['round_trips']:,}</td>"
                f"<td>{pct(r['win_rate'], 0, False)}</td><td>{num(r['avg_days'], 1)}</td><td>{pct(r['yearly_before'])}</td>"
                f"<td>{pct(r['yearly_after'])}</td><td>{num(r['sharpe_after'], 2)}</td><td>{pct(r['worst_drop'])}</td>"
                f"<td>{pct(r['first_half'])}</td><td>{pct(r['second_half'])}</td><td>{num(r.get('beta_to_spy'), 2)}</td>"
                f"<td>{badge(r['verdict'])}</td></tr>")
        first = rows[0]
        parts.append(f"<p class=note>Tested from {esc(first['first_day'])} to {esc(first['last_day'])} "
                     f"({first['days']} trading days; the first 12 months of prices are only used to pick the pairs). "
                     f"Every trade opens and closes at the next morning's open after the signal, pays costs on every "
                     f"leg and {SHORT_BORROW_PER_YEAR:.0%} a year to borrow the shorted shares. \"Per year\" is the "
                     f"return on the money each trade ties up ($1 per pair or per stock). Sharpe = return per unit of "
                     f"risk (above 1 is good, above 2 is excellent). Market beta near 0 means it does not depend on the "
                     f"market going up.</p>")
        parts.append(f"<div class=wrap><table>{head}{''.join(body)}</table></div>")
        parts.append("<p class=note><b>WORKS</b> = made money after costs, clearly enough to trust (t-stat 2+), in "
                     "both halves of the test; <b>WEAK</b> = made a little after costs, but not clearly enough to trust "
                     "yet; <b>NO EDGE</b> = did not make money after costs; <b>LOSES MONEY</b> = clearly lost money "
                     "after costs; <b>TOO FEW</b> = fewer than 30 trades, not enough to judge. A strategy only goes "
                     "into the paper account if it WORKS here and again on more years of prices.</p>")
    pairs = sa.get("pairs_now") or []
    if pairs:
        parts.append("<h3>The pairs the paper's method would watch right now</h3><p class=note>The 20 pairs whose "
                     "prices moved most closely together over the last 12 months.</p><p>" +
                     ", ".join(f"{esc(p['a'])} / {esc(p['b'])}" for p in pairs) + "</p>")
    return "".join(parts)


def write_report(results, status):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    sig = results.get("signals") or {}
    cov = results.get("coverage", {})
    parts = []
    parts.append("<h1>Signal Lab report card</h1>")
    period = results.get("period")
    if period:
        parts.append(f"<p class=note>Prices from {esc(period[0])} to {esc(period[1])}. "
                     f"Made {esc(status.get('finished', ''))}.</p>")
    lines = headline(results)
    if lines:
        parts.append("<h2>The short version</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in lines) + "</ul>")
    warnings = status.get("warnings") or []
    if warnings:
        parts.append("<h2>Notes about this run</h2><ul>" + "".join(f"<li>{esc(w)}</li>" for w in warnings) + "</ul>")

    rep = results.get("replay")
    if rep:
        un, hd = rep["unhedged"], rep["hedged"]
        parts.append("<h2>Replay of the tracker's rules</h2>")
        parts.append(curve_svg(rep["curve"]))
        rows = [("Total return", pct(un["total"]), pct(hd["total"])),
                ("Per year", pct(un["yearly"]), pct(hd["yearly"])),
                ("Sharpe ratio (return per unit of risk)", num(un["sharpe"], 2), num(hd["sharpe"], 2)),
                ("Worst drop", pct(un["max_drawdown"]), pct(hd["max_drawdown"])),
                ("Ups and downs (yearly volatility)", pct(un["volatility"], 1, False), pct(hd["volatility"], 1, False))]
        parts.append("<div class=wrap><table><tr><th></th><th>Unhedged</th><th>Hedged with IWM</th></tr>" +
                     "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows) + "</table></div>")
        g = rep.get("groups", {})
        parts.append("<div class=wrap><table><tr><th>Trades</th><th>Count</th><th>Avg return</th><th>Win rate</th></tr>" +
                     "".join(f"<tr><td>{esc(k)}</td><td>{v['n']:,}</td><td>{pct(v['avg'], 2)}</td>"
                             f"<td>{pct(v['win_rate'], 0, False)}</td></tr>" for k, v in sorted(g.items())) + "</table></div>")
        parts.append("<p class=note>Shorts assume the shares could be borrowed. Many of the smallest going-concern "
                     "companies are hard or impossible to borrow, so the short side is a best case. Option trades are "
                     "not included.</p>")

    parts.append(signal_table(sig.get("tags", []), "Your signals, one at a time",
                              f"Each row is every day the signal appeared in a stock the tracker could trade (price $2+, "
                              f"$500,000+ traded a day), held {MAIN_HORIZON} trading days from the next morning's open, "
                              f"compared with IWM, after trading costs."))
    if sig.get("tags"):
        legend = "; ".join(f"<b>{k}</b> = {v}" for k, v in VERDICT_WORDS.items()
                           if k in ("WORKS", "WEAK", "NO EDGE", "LOSES MONEY", "TOO FEW"))
        parts.append(f"<p class=note>{legend}.</p>")
    parts.append(horizon_table(sig.get("tags", []), "How the move builds over time"))
    parts.append(signal_table(sig.get("traded", []), "The trades the tracker would take",
                              "Grouped by side, by score and by company size. The size split shows whether the edge "
                              "lives in small companies that big funds can't trade."))
    parts.append(signal_table(sig.get("ideas", []), "New ideas being tested",
                              f"Ideas must pass a stricter test: at least 100 events, a t-stat of {NEW_IDEA_MIN_T:g} or "
                              f"more, money made after costs, and in both halves of the period. <b>PROMISING</b> = "
                              f"{VERDICT_WORDS['PROMISING']}; <b>MAYBE</b> = {VERDICT_WORDS['MAYBE']}.", new=True))
    forms = results.get("forms") or []
    if forms:
        parts.append(signal_table(forms[:30], "Every SEC form type (the 30 strongest results)",
                                  "Each form type with 100+ tradeable events, tested the same way. With this many "
                                  "tests, some will look good by luck, so only PROMISING ones are worth a closer look. "
                                  "The full list is in form_types.csv.", new=True))
    live = results.get("live") or {}
    if live.get("groups"):
        parts.append(signal_table(live["groups"], "Your real signals so far",
                                  f"Read from the lists your scanner saved each morning ({live['measured']:,} measured "
                                  f"over {live.get('trading_days', 0)} trading days, {live.get('pending', 0)} too new to "
                                  f"measure). Needs about 60 trading days before it means much. This grows every run."))

    parts.append(statarb_section(results.get("statarb")))

    cov_lines = [
        f"Insider data: {cov.get('insider_quarters', 0)} of {cov.get('insider_quarters_needed', 0)} quarters "
        f"(the SEC publishes each quarter a few weeks after it ends).",
        f"8-Ks read with the scanner's rules: {pct(cov.get('eightk_read'), 0, False)} "
        f"({cov.get('eightk_read_count', 0):,} of {cov.get('eightk_total', 0):,}).",
        f"Going-concern filings read with the scanner's rule: {cov.get('gc_checked', 0):,} of "
        f"{cov.get('gc_candidates', 0):,} found by full-text search ({cov.get('gc_confirmed_rate_text', '-')} confirmed; "
        f"only confirmed ones count).",
        f"Price days saved: {cov.get('price_days', 0):,}. Stock splits applied: {cov.get('splits_applied', 0):,} "
        f"from the official list, {cov.get('splits_guessed', 0):,} caught by the overnight-jump check.",
    ]
    parts.append("<h2>Data coverage</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in cov_lines) + "</ul>")
    parts.append(
        "<h2>How this was measured</h2><ul>"
        "<li>Each trade opens at the next morning's open after the filing, because that is the first time the tracker "
        "can act.</li>"
        "<li>Results are compared with IWM over the same days, so a rising or falling market doesn't count as skill.</li>"
        f"<li>Costs: 0.05% to 1% each way depending on how much the stock trades, plus {SHORT_BORROW_PER_YEAR:.0%} a year "
        "to borrow shares for shorts.</li>"
        "<li>Any single move bigger than 100% is counted as 100% in the averages, so one wild stock can't decide a result.</li>"
        "<li>The t-stat says how sure we can be that the average isn't luck: 2 means fairly sure, 3 means very sure. "
        "Events on the same day are grouped, so a busy day can't fake a strong result.</li>"
        "<li>1st half / 2nd half: the same average for the older and newer half of the period. A real signal should "
        "work in both.</li>"
        "<li>Two years is one kind of market. More years (paid price data, or WRDS) would show whether a result holds "
        "in crashes and in booms.</li></ul>")

    page = (
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Signal Lab report card</title><style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d1f24;background:#fff;"
        "margin:0;padding:16px;line-height:1.45}"
        "h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 6px}h3{font-size:15px;margin:18px 0 6px}"
        ".note{color:#5a5f69;font-size:13px;margin:4px 0 8px}.wrap{overflow-x:auto}"
        "table{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;min-width:100%}"
        "th,td{padding:5px 8px;border-bottom:1px solid #e3e5ea;text-align:right;white-space:nowrap}"
        "th:first-child,td:first-child{text-align:left;white-space:normal;min-width:170px}"
        "th{background:#f4f5f8;font-weight:600}li{margin:4px 0}"
        "</style></head><body style='max-width:900px;margin:0 auto'>" + "".join(parts) + "</body></html>")
    write_text(os.path.join(RESULTS_DIR, "report_card.html"), page)

    # tables for later analysis
    header = ["group", "name", "side", "verdict"]
    for h in HORIZONS:
        header += [f"n_{h}d", f"avg_after_cost_{h}d", f"win_rate_{h}d", f"t_{h}d", f"first_half_{h}d", f"second_half_{h}d"]
    header += ["overnight_gap", "stopped_trading"]
    rows = []
    for group in ("tags", "traded", "ideas"):
        for r in sig.get(group, []):
            row = [group, r["name"], r["side"], r["verdict"]]
            for h in HORIZONS:
                s = r["stats"][h]
                row += [s.get("n", 0), s.get("avg_after_cost"), s.get("win_rate"), s.get("t"), s.get("first_half"),
                        s.get("second_half")]
            row += [r["stats"].get("gap"), r["stats"].get("stopped_trading")]
            rows.append(row)
    write_csv(os.path.join(RESULTS_DIR, "signals.csv"), header, rows)
    rows = []
    for r in forms:
        row = ["form", r["form"], r["side"], r["verdict"]]
        for h in HORIZONS:
            s = r["stats"][h]
            row += [s.get("n", 0), s.get("avg_after_cost"), s.get("win_rate"), s.get("t"), s.get("first_half"),
                    s.get("second_half")]
        row += [r["stats"].get("gap"), r["stats"].get("stopped_trading")]
        rows.append(row)
    write_csv(os.path.join(RESULTS_DIR, "form_types.csv"), header, rows)
    trades = results.get("replay_trades") or []
    if trades:
        keys = list(trades[0].keys())
        write_csv(os.path.join(RESULTS_DIR, "replay_trades.csv"), keys, [[t[k] for k in keys] for t in trades])
    if rep:
        write_csv(os.path.join(RESULTS_DIR, "replay_curve.csv"), ["date", "unhedged", "hedged"], rep["curve"])
    sa_rows = (results.get("statarb") or {}).get("methods") or []
    if sa_rows:
        keys = ["key", "name", "verdict", "first_day", "last_day", "days", "round_trips", "win_rate", "avg_days",
                "avg_trade", "yearly_before", "yearly_after", "sharpe_before", "sharpe_after", "t", "first_half",
                "second_half", "worst_drop", "beta_to_spy"]
        write_csv(os.path.join(RESULTS_DIR, "statarb.csv"), keys, [[r.get(k) for k in keys] for r in sa_rows])
    sa_trades = results.get("statarb_trades") or []
    sector = [t for t in sa_trades if t.get("method") == "sector"]
    if len(sector) > 1000:                              # keep the file small: every pairs trade, the newest sector ones
        keep = set(id(t) for t in sorted(sector, key=lambda t: t["closed"])[-1000:])
        sa_trades = [t for t in sa_trades if t.get("method") != "sector" or id(t) in keep]
    if sa_trades:
        keys = list(sa_trades[0].keys())
        write_csv(os.path.join(RESULTS_DIR, "statarb_trades.csv"), keys, [[t.get(k) for k in keys] for t in sa_trades])
    summary = {k: v for k, v in results.items() if k not in ("replay_trades", "statarb_trades")}
    if summary.get("replay"):
        summary["replay"] = {k: v for k, v in summary["replay"].items() if k != "curve"}
    summary["status"] = status
    write_json(os.path.join(RESULTS_DIR, "summary.json"), summary)
    return lines
