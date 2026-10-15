"""
email_overview.py
The portfolio overview at the top of the daily email:
  * money made since the last run, this month and since the start, each next to what SPY did
  * a chart of the account against SPY
  * money by strategy (what each one is using now, its open and its closed profit or loss)
  * today's trades
  * the closed-trade scorecard and the latest closed trades
It only reads what the tracker and the sleeves already saved (no IBKR, no website), and a missing or damaged
file only leaves that part out. The plain-text summary is still in the email, under the overview.

See it any time (no orders, nothing sent):   python tracker/email_overview.py
It saves tracker_data/email_preview.html. Double-click that file to open it in your browser.
"""

import base64
import csv
import html
import json
import math
import os
import struct
import sys
import zlib
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C  # noqa: E402

CHART_ID = "overview-chart@tracker"
NAMES = {"tracker": "SEC-filing tracker", "hedge": "IWM hedge", "trend": "Trend", "sector": "Sector rotation",
         "momentum": "Momentum", "meanrev": "Mean reversion", "pairs": "Pairs", "cash": "T-bills (SGOV)",
         "plain": "Plain cash"}
SHORT_NAMES = {"tracker": "Tracker", "hedge": "Hedge", "trend": "Trend", "sector": "Sector", "momentum": "Momentum",
               "meanrev": "Mean rev.", "pairs": "Pairs", "cash": "T-bills"}
ORDER = ["tracker", "hedge", "trend", "sector", "momentum", "meanrev", "pairs", "cash", "plain"]
MAX_TRADE_ROWS = 40          # today's trades shown in the overview (the full list is in the text below it)
RECENT_CLOSED = 6            # latest closed tracker trades shown

GREEN, RED, GRAY, INK, BLUE, SPY_GRAY = "#15803d", "#b91c1c", "#6b7280", "#111827", "#2563eb", "#9ca3af"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
CARD = "background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:14px 12px;margin:0 0 12px"
H2 = f"margin:0 0 10px;font-size:15px;font-weight:700;color:{INK}"
TH = ("padding:6px 4px;border-bottom:1px solid #d1d5db;color:#6b7280;font-weight:600;font-size:11px;"
      "text-transform:uppercase;letter-spacing:.04em;text-align:left")
THR = TH + ";text-align:right"
TD = "padding:6px 4px;border-bottom:1px solid #eef0f3;font-size:13px;vertical-align:top"
TDR = TD + ";text-align:right;white-space:nowrap"
SMALL = f"font-size:11px;color:{GRAY}"
esc = html.escape


# ---------------------------------------------------------------- reading the saved files

def _path(name):
    return os.path.join(C.DATA_DIR, name)


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _day(text):
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _csv(name):
    try:
        with open(_path(name), newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def _json(name, default):
    try:
        with open(_path(name)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def spy_closes():
    """{day: SPY close} from the sleeves' saved prices (tracker_data/prices/SPY.csv), or {}."""
    out = {}
    for r in _csv(os.path.join("prices", "SPY.csv")):
        d, c = _day(r.get("date")), _f(r.get("close"))
        if d is not None and c and c > 0:
            out[d] = c
    return out


def spy_before(day, closes):
    """The last SPY close before `day` (what SPY stood at going into that morning's run), or None."""
    earlier = [d for d in closes if d < day]
    return closes[max(earlier)] if earlier else None


def equity_points():
    """[(day, account value, SPY price or None)] from equity_curve.csv, one per day, oldest first.
    A day saved without SPY's price (IBKR sent none) uses SPY's close from the day before, from the saved prices."""
    by_day = {}
    for r in _csv("equity_curve.csv"):
        d, v = _day(r.get("date")), _f(r.get("net_liq"))
        if d is None or not v or v <= 0:
            continue
        spy = _f(r.get("spy_price"))
        by_day[d] = (v, spy if spy and spy > 0 else None)
    if any(s is None for v, s in by_day.values()):
        closes = spy_closes()
        by_day = {d: (v, s if s is not None else spy_before(d, closes)) for d, (v, s) in by_day.items()}
    return [(d, v, s) for d, (v, s) in sorted(by_day.items())]


def _tags(value):
    if isinstance(value, list):
        return [str(t) for t in value]
    return [t for t in str(value or "").split("; ") if t]


def closed_trades(tracker_report=None):
    """The tracker's closed trades (stocks and options), oldest first."""
    raw = tracker_report.get("closed") if tracker_report else None
    if raw is None:
        raw = _csv("closed_trades.csv")
    out = []
    for t in raw:
        out.append({
            "symbol": str(t.get("symbol") or ""), "direction": str(t.get("direction") or ""),
            "tags": _tags(t.get("tags")), "qty": _f(t.get("qty")) or 0.0,
            "entry_date": str(t.get("entry_date") or ""), "exit_date": str(t.get("exit_date") or ""),
            "entry_price": _f(t.get("entry_price")), "exit_price": _f(t.get("exit_price")),
            "exit_reason": str(t.get("exit_reason") or ""), "pnl": _f(t.get("pnl")) or 0.0,
            "return_pct": _f(t.get("return_pct")), "hold_days": _f(t.get("hold_days")),
            "trade_id": str(t.get("trade_id") or ""),
        })
    return out


def is_option(t):
    return ("OPTION_SPREAD" in t["tags"] or "OPTION_SINGLE" in t["tags"]
            or "OPTION" in t["direction"] or "SPREAD" in t["direction"])


def sleeve_trades():
    out = []
    for r in _csv("sleeve_trades.csv"):
        out.append({"date": str(r.get("date") or ""), "sleeve": str(r.get("sleeve") or ""),
                    "symbol": str(r.get("symbol") or ""), "action": str(r.get("action") or "").upper(),
                    "qty": _f(r.get("qty")) or 0.0, "price": _f(r.get("price")),
                    "pnl": _f(r.get("realized_pnl")) or 0.0})
    return out


def load_sleeve_report(day):
    """Today's sleeves report (saved by the sleeves at the end of their run), or None."""
    data = _json("sleeves_report.json", {})
    if data and str(data.get("generated") or "")[:10] == day.isoformat():
        return data
    return None


# ---------------------------------------------------------------- numbers and words

def money(x, signed=False):
    if x is None:
        return "&ndash;"
    sign = "-" if x < -0.5 else ("+" if signed and x >= 0.5 else "")
    return f"{sign}${abs(x):,.0f}"


def pct(x, signed=True, digits=2):
    if x is None:
        return "&ndash;"
    shown = round(abs(x) * 100, digits)
    sign = "-" if x < 0 and shown else ("+" if signed and x > 0 and shown else "")
    return f"{sign}{shown:.{digits}f}%"


def tone(x, eps=0.5):
    if x is None or abs(x) < eps:
        return INK
    return GREEN if x > 0 else RED


def colored(text, x, eps=0.5, bold=False):
    weight = ";font-weight:700" if bold else ""
    return f'<span style="color:{tone(x, eps)}{weight}">{text}</span>'


def day_words(d):
    return f"{d:%a} {d:%b} {d.day}"


def price_words(x):
    if x is None:
        return "&ndash;"
    return f"${x:,.2f}"


def shares_words(x):
    if x is None:
        return "&ndash;"
    return f"{x:,.0f}"


# ---------------------------------------------------------------- 1. money made

def periods(points):
    """The cards at the top: since the last run, this month, since the start (repeats left out)."""
    if len(points) < 2:
        return []
    end = points[-1]
    month_start = end[0].replace(day=1)
    before_month = [p for p in points if p[0] < month_start]
    wanted = [("Since the last run", points[-2]),
              ("This month", before_month[-1] if before_month else None),
              ("Since the start", points[0])]
    out, seen = [], {}
    for label, base in wanted:
        if base is None or base is end:
            continue
        if base[0] in seen:
            out[seen[base[0]]]["label"] = label       # the same day twice: keep the wider label
            continue
        bd, bv, bs = base
        ed, ev, es = end
        spy = (es / bs - 1) if (bs and es) else None
        seen[bd] = len(out)
        out.append({"label": label, "since": bd, "dollars": ev - bv, "pct": ev / bv - 1, "spy": spy,
                    "spy_dollars": bv * spy if spy is not None else None})
    return out


def period_cards(points):
    cards = periods(points)
    if not cards:
        return ('<p style="margin:0;font-size:13px;color:#6b7280">Money made shows up once there are two days of '
                'results.</p>')
    width = f"{100 // len(cards)}%"
    cells = []
    for i, c in enumerate(cards):
        gap_left = "0" if i == 0 else "4px"
        gap_right = "0" if i == len(cards) - 1 else "4px"
        big = money(c["dollars"], True)
        size = {7: 18, 8: 16, 9: 14}.get(len(big), 18 if len(big) < 7 else 13)   # long numbers still fit on a phone
        if c["spy"] is None:
            versus = "SPY: no price saved"
        else:
            gap = (c["pct"] - c["spy"]) * 100
            if abs(gap) < 0.005:
                where = "even with SPY"
            else:
                where = f"{abs(gap):.2f} pts {'ahead of' if gap > 0 else 'behind'} SPY"
            versus = (f'SPY {colored(pct(c["spy"]), c["spy"], 1e-5)} ({money(c["spy_dollars"], True)})<br>'
                      f'<span style="color:{tone(gap, 0.005)}">{where}</span>')
        cells.append(
            f'<td width="{width}" style="padding:0 {gap_right} 0 {gap_left};vertical-align:top">'
            f'<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:10px 8px 12px">'
            f'<div style="{SMALL};text-transform:uppercase;letter-spacing:.04em;font-weight:600">{esc(c["label"])}</div>'
            f'<div style="{SMALL};margin-bottom:6px">since {esc(day_words(c["since"]))}</div>'
            f'<div style="font-size:{size}px;line-height:1.2;white-space:nowrap">{colored(big, c["dollars"], bold=True)}</div>'
            f'<div style="font-size:13px;margin:2px 0 6px">{colored(pct(c["pct"]), c["pct"], 1e-5)}</div>'
            f'<div style="font-size:11px;line-height:1.45;color:#374151">{versus}</div>'
            f'</div></td>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse"><tr>{"".join(cells)}</tr></table>')


# ---------------------------------------------------------------- 2. the chart (a small PNG drawn here)

GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "%": ("11000", "11001", "00010", "00100", "01000", "10011", "00011"),
}


class Canvas:
    """A see-through picture (RGBA) with smooth lines, saved as PNG. Plain Python, nothing to install."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.px = bytearray(width * height * 4)

    def blend(self, x, y, rgb, alpha):
        if alpha <= 0 or not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) * 4
        under = self.px[i + 3] / 255.0
        out = alpha + under * (1 - alpha)
        for k in range(3):
            self.px[i + k] = int(round((rgb[k] * alpha + self.px[i + k] * under * (1 - alpha)) / out))
        self.px[i + 3] = int(round(out * 255))

    def rect(self, x0, y0, x1, y1, rgb, alpha=1.0):
        for y in range(max(0, int(y0)), min(self.h, int(y1))):
            for x in range(max(0, int(x0)), min(self.w, int(x1))):
                self.blend(x, y, rgb, alpha)

    def stroke(self, points, rgb, width, dot=0.0):
        """A smooth line through the points, and a round dot of radius `dot` on the last one."""
        half = width / 2.0
        cover = {}

        def mark(x, y, c):
            key = y * self.w + x
            if c > cover.get(key, 0.0):
                cover[key] = c

        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            dx, dy = x1 - x0, y1 - y0
            length2 = dx * dx + dy * dy
            for y in range(max(0, int(min(y0, y1) - half - 2)), min(self.h, int(max(y0, y1) + half + 3))):
                py = y + 0.5
                for x in range(max(0, int(min(x0, x1) - half - 2)), min(self.w, int(max(x0, x1) + half + 3))):
                    px = x + 0.5
                    t = ((px - x0) * dx + (py - y0) * dy) / length2 if length2 else 0.0
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                    ex, ey = px - (x0 + t * dx), py - (y0 + t * dy)
                    c = half + 0.5 - math.sqrt(ex * ex + ey * ey)
                    if c > 0:
                        mark(x, y, min(c, 1.0))
        if dot and points:
            cx, cy = points[-1]
            for y in range(max(0, int(cy - dot - 2)), min(self.h, int(cy + dot + 3))):
                for x in range(max(0, int(cx - dot - 2)), min(self.w, int(cx + dot + 3))):
                    c = dot + 0.5 - math.hypot(x + 0.5 - cx, y + 0.5 - cy)
                    if c > 0:
                        mark(x, y, min(c, 1.0))
        for key, c in cover.items():
            self.blend(key % self.w, key // self.w, rgb, c)

    def text_width(self, text, scale):
        return len(text) * 6 * scale - scale

    def text(self, x, y, text, rgb, scale):
        for ch in text:
            rows = GLYPHS.get(ch)
            if rows:
                for r, line in enumerate(rows):
                    for c, bit in enumerate(line):
                        if bit == "1":
                            self.rect(x + c * scale, y + r * scale, x + (c + 1) * scale, y + (r + 1) * scale, rgb)
            x += 6 * scale

    def png(self):
        stride = self.w * 4
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)
            raw += self.px[y * stride:(y + 1) * stride]

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _nice_step(raw):
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * power >= raw:
            return m * power
    return 10 * power


def chart_series(points):
    """(account % since the start, SPY % since the start or None) for each day."""
    if len(points) < 2:
        return None
    v0, s0 = points[0][1], points[0][2]
    account = [(v / v0 - 1) * 100 for _, v, _ in points]
    spy = [((s / s0 - 1) * 100 if (s0 and s) else None) for _, _, s in points]
    return account, spy


def chart_png(points, width=960, height=340):
    """The account (blue) against SPY (gray), in % since the first day. Drawn at twice the shown size, so it is
    sharp on phones. Returns PNG bytes, or None with fewer than two days."""
    series = chart_series(points)
    if not series:
        return None
    account, spy = series
    values = account + [s for s in spy if s is not None] + [0.0]
    lo, hi = min(values), max(values)
    if hi - lo < 0.2:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.1, mid + 0.1
    step = _nice_step((hi - lo) / 4)
    lo_tick, hi_tick = math.floor(lo / step - 1e-9) * step, math.ceil(hi / step + 1e-9) * step
    digits = 2 if step < 0.1 else (1 if step < 1 else 0)
    scale = 4
    labels = []
    k = 0
    while lo_tick + k * step <= hi_tick + step / 1000:
        v = lo_tick + k * step
        shown = round(v, digits)
        text = "0%" if shown == 0 else f"{'+' if shown > 0 else '-'}{abs(shown):.{digits}f}%"
        labels.append((v, text))
        k += 1
    canvas = Canvas(width, height)
    left = max(canvas.text_width(t, scale) for _, t in labels) + 26
    right, top, bottom = width - 26, 22, height - 22

    def y_of(v):
        return bottom - (v - lo_tick) / (hi_tick - lo_tick) * (bottom - top)

    def x_of(i):
        return left + i * (right - left) / (len(points) - 1)

    grid = _rgb(SPY_GRAY)
    for v, text in labels:
        y = int(round(y_of(v)))
        zero = abs(v) < step / 1000
        canvas.rect(left, y - (2 if zero else 1), right + 1, y + (1 if zero else 1), grid, 0.85 if zero else 0.35)
        canvas.text(left - 16 - canvas.text_width(text, scale), y - 3 * scale - 1, text, (128, 134, 145), scale)
    spy_points = [(x_of(i), y_of(s)) for i, s in enumerate(spy) if s is not None]
    if len(spy_points) >= 2:
        canvas.stroke(spy_points, grid, 4, dot=6)
    canvas.stroke([(x_of(i), y_of(a)) for i, a in enumerate(account)], _rgb(BLUE), 6, dot=8)
    return canvas.png()


def chart_block(points, has_chart):
    if not has_chart:
        return ""
    account, spy = chart_series(points)
    swatch = ('<span style="display:inline-block;width:14px;height:3px;border-radius:2px;background:{};'
              'vertical-align:middle;margin-right:5px"></span>')
    legend = (f'{swatch.format(BLUE)}Your account {colored(pct(account[-1] / 100), account[-1], 1e-5, True)}')
    if spy[0] is not None and spy[-1] is not None:
        legend += (f'&nbsp;&nbsp;&nbsp;{swatch.format(SPY_GRAY)}SPY '
                   f'{colored(pct(spy[-1] / 100), spy[-1], 1e-5, True)}')
    return (f'<div style="{CARD}"><div style="{H2}">Account vs SPY since {esc(day_words(points[0][0]))}</div>'
            f'<div style="font-size:12px;color:#374151;margin:0 0 8px">{legend}</div>'
            f'<img src="cid:{CHART_ID}" width="560" alt="Chart: your account against SPY" '
            f'style="display:block;width:100%;max-width:560px;height:auto;border:0">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="{SMALL};padding-top:4px">{esc(day_words(points[0][0]))}</td>'
            f'<td style="{SMALL};padding-top:4px;text-align:right">{esc(day_words(points[-1][0]))}</td>'
            f'</tr></table></div>')


# ---------------------------------------------------------------- 3. money by strategy

def strategy_rows(tracker_report, sleeve_report, closed):
    """[{key, name, using, using_pct, open, closed}] for each strategy that has money in it or a result."""
    state = _json("sleeves_state.json", {})
    realized = state.get("realized") if isinstance(state.get("realized"), dict) else {}
    held = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    tracker_closed = sum(t["pnl"] for t in closed)
    capital = (sleeve_report or {}).get("capital") or {}
    cap_rows = {r.get("key"): r for r in capital.get("rows") or [] if isinstance(r, dict)}
    equity = _f((capital.get("totals") or {}).get("equity"))
    if not equity and tracker_report:
        equity = _f(tracker_report.get("equity"))
    rows = []
    for key in ORDER:
        r = cap_rows.get(key)
        closed_pnl = None
        if key == "tracker":
            closed_pnl = tracker_closed
        elif key not in ("hedge", "plain"):
            closed_pnl = _f(realized.get(key)) or 0.0
        if r is not None:
            using, open_pnl = _f(r.get("gross")) or 0.0, (_f(r.get("pnl")) if key != "plain" else None)
        elif key == "tracker" and tracker_report:
            expo = tracker_report.get("exposure") or {}
            options = sum(abs((_f(o.get("mid")) or _f(o.get("entry_debit")) or 0.0) * (_f(o.get("qty")) or 0) * 100)
                          for o in tracker_report.get("option_rows") or [])
            using = (_f(expo.get("gross")) or 0.0) + options
            open_pnl = (sum(_f(o.get("pnl")) or 0.0 for o in tracker_report.get("open_rows") or [])
                        + sum(_f(o.get("pnl")) or 0.0 for o in tracker_report.get("option_rows") or []))
        else:
            using, open_pnl = None, None
        has_money = bool(using) and abs(using) >= 1
        has_result = bool(closed_pnl) and abs(closed_pnl) >= 0.5
        if key == "tracker" or has_money or has_result or (key in held and held.get(key)):
            if key == "plain" and not has_money:
                continue
            rows.append({"key": key, "name": NAMES[key], "using": using,
                         "using_pct": (using / equity) if (using is not None and equity) else None,
                         "open": open_pnl, "closed": closed_pnl})
    return rows


def strategy_table(rows, from_sleeves):
    if not rows:
        return ""
    body = []
    totals = {"using": None, "open": None, "closed": None}
    for r in rows:
        total = None
        if r["open"] is not None or r["closed"] is not None:
            total = (r["open"] or 0.0) + (r["closed"] or 0.0)
        if r["key"] != "plain":
            for k in ("using", "open", "closed"):
                if r[k] is not None:
                    totals[k] = (totals[k] or 0.0) + r[k]
        share = f'<div style="{SMALL}">{pct(r["using_pct"], signed=False, digits=1)}</div>' if r["using_pct"] is not None else ""
        body.append(
            f'<tr><td style="{TD}">{esc(r["name"])}</td>'
            f'<td style="{TDR}">{money(r["using"])}{share}</td>'
            f'<td style="{TDR}">{colored(money(r["open"], True), r["open"]) if r["open"] is not None else "&ndash;"}</td>'
            f'<td style="{TDR}">{colored(money(r["closed"], True), r["closed"]) if r["closed"] is not None else "&ndash;"}</td>'
            f'<td style="{TDR}">{colored(money(total, True), total, bold=True) if total is not None else "&ndash;"}</td></tr>')
    total_all = None
    if totals["open"] is not None or totals["closed"] is not None:
        total_all = (totals["open"] or 0.0) + (totals["closed"] or 0.0)

    def cell(x):
        return colored(money(x, True), x, bold=True) if x is not None else "&ndash;"

    body.append(
        f'<tr><td style="{TD};font-weight:700;border-bottom:0">All strategies</td>'
        f'<td style="{TDR};font-weight:700;border-bottom:0">{money(totals["using"])}</td>'
        f'<td style="{TDR};border-bottom:0">{cell(totals["open"])}</td>'
        f'<td style="{TDR};border-bottom:0">{cell(totals["closed"])}</td>'
        f'<td style="{TDR};border-bottom:0">{cell(total_all)}</td></tr>')
    note = ("Using = long + short value right now. Open = profit or loss on positions still held. "
            "Closed = profit or loss already taken. The hedge's closed trades, T-bill interest and dividends are not "
            "split out, so this will not match the account's change exactly.")
    if not from_sleeves:
        note = "The strategy sleeves did not report today, so only saved results are shown for them. " + note
    return (f'<div style="{CARD}"><div style="{H2}">Money by strategy</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
            f'<tr><th style="{TH}">Strategy</th><th style="{THR}">Using</th><th style="{THR}">Open</th>'
            f'<th style="{THR}">Closed</th><th style="{THR}">Total</th></tr>{"".join(body)}</table>'
            f'<p style="{SMALL};margin:8px 0 0;line-height:1.45">{esc(note)}</p></div>')


# ---------------------------------------------------------------- 4. today's trades

def todays_trades(day, tracker_report, closed):
    """[{strategy, action, what, shares, price, pnl, note}] for everything traded today."""
    today = day.isoformat()
    out = []
    open_trades = _json("open_trades.json", [])
    seen = set()
    opened_and_closed = [t for t in closed if t["entry_date"] == today and not is_option(t)]
    for t in [x for x in open_trades if isinstance(x, dict)] + opened_and_closed:
        if str(t.get("entry_date")) != today or t.get("trade_id") in seen:
            continue
        seen.add(t.get("trade_id"))
        out.append({"strategy": "tracker", "action": "Short" if t.get("direction") == "SHORT" else "Buy",
                    "what": str(t.get("symbol") or ""), "shares": _f(t.get("qty")), "price": _f(t.get("entry_price")),
                    "pnl": None, "note": ""})
    for o in _json("open_options.json", []):
        if not isinstance(o, dict) or str(o.get("entry_date")) != today:
            continue
        word = "put" if o.get("right") == "P" else "call"
        strikes = f"{_f(o.get('long_strike')) or 0:g}"
        if o.get("short_strike") not in (None, ""):
            strikes += f"/{_f(o.get('short_strike')) or 0:g}"
            word += " spread"
        out.append({"strategy": "tracker", "action": "Buy", "what": f"{o.get('symbol', '')} {strikes} {word}",
                    "shares": _f(o.get("qty")), "price": _f(o.get("entry_debit")), "pnl": None, "note": ""})
    for t in closed:
        if t["exit_date"] != today:
            continue
        if is_option(t):
            action = "Sell"
        else:
            action = "Cover" if t["direction"] == "SHORT" else "Sell"
        out.append({"strategy": "tracker", "action": action, "what": t["symbol"], "shares": t["qty"],
                    "price": t["exit_price"], "pnl": t["pnl"], "note": t["exit_reason"]})
    hedge = (tracker_report or {}).get("hedge_info") or {}
    traded = _f(hedge.get("traded_shares")) or 0.0
    if traded:
        out.append({"strategy": "hedge", "action": "Buy" if traded > 0 else "Sell", "what": str(hedge.get("symbol") or "IWM"),
                    "shares": abs(traded), "price": _f(hedge.get("price")), "pnl": None, "note": ""})
    for t in sleeve_trades():
        if t["date"] != today:
            continue
        out.append({"strategy": t["sleeve"], "action": "Buy" if t["action"] == "BUY" else "Sell", "what": t["symbol"],
                    "shares": t["qty"], "price": t["price"], "pnl": t["pnl"] or None, "note": ""})
    rank = {k: i for i, k in enumerate(ORDER)}
    out.sort(key=lambda r: rank.get(r["strategy"], len(ORDER)))
    return out


def trades_table(trades, mode):
    if not trades:
        words = "Plan only: no orders were placed." if mode == "plan" else "No trades today."
        return f'<div style="{CARD}"><div style="{H2}">Today\'s trades</div><p style="margin:0;font-size:13px;color:#374151">{words}</p></div>'
    shown = trades[:MAX_TRADE_ROWS]
    with_pnl = any(t["pnl"] is not None for t in shown)       # the P&L column only when something was closed
    rows = []
    for t in shown:
        note = f'<div style="{SMALL}">{esc(t["note"])}</div>' if t["note"] else ""
        pnl = ""
        if with_pnl:
            pnl = f'<td style="{TDR}">{colored(money(t["pnl"], True), t["pnl"]) if t["pnl"] is not None else ""}</td>'
        rows.append(
            f'<tr><td style="{TD};white-space:nowrap">{esc(SHORT_NAMES.get(t["strategy"], t["strategy"]))}</td>'
            f'<td style="{TD}">{esc(t["action"])} <b>{esc(t["what"])}</b>{note}</td>'
            f'<td style="{TDR}">{shares_words(t["shares"])}</td>'
            f'<td style="{TDR}">{price_words(t["price"])}</td>{pnl}</tr>')
    more = ""
    if len(trades) > MAX_TRADE_ROWS:
        more = f'<p style="{SMALL};margin:8px 0 0">and {len(trades) - MAX_TRADE_ROWS} more (all of them are in the text below).</p>'
    count = f"{len(trades)} trade{'s' if len(trades) != 1 else ''}"
    pnl_head = f'<th style="{THR}">P&amp;L</th>' if with_pnl else ""
    return (f'<div style="{CARD}"><div style="{H2}">Today\'s trades <span style="{SMALL};font-weight:400">'
            f'({count})</span></div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
            f'<tr><th style="{TH}">Strategy</th><th style="{TH}">Trade</th>'
            f'<th style="{THR}">Shares</th><th style="{THR}">Price</th>'
            f'{pnl_head}</tr>'
            f'{"".join(rows)}</table>{more}</div>')


# ---------------------------------------------------------------- 5. closed-trade scorecard

def score(pnls):
    n = len(pnls)
    if not n:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {"n": n, "win_rate": len(wins) / n, "total": sum(pnls),
            "avg_win": sum(wins) / len(wins) if wins else None,
            "avg_loss": sum(losses) / len(losses) if losses else None}


def scorecard(closed):
    groups = [
        ("Tracker stocks", score([t["pnl"] for t in closed if not is_option(t)])),
        ("Tracker options", score([t["pnl"] for t in closed if is_option(t)])),
        ("Strategy sleeves", score([t["pnl"] for t in sleeve_trades() if t["pnl"] and t["sleeve"] != "cash"])),
    ]
    rows = []
    for name, s in groups:
        if not s:
            continue
        rows.append(
            f'<tr><td style="{TD}">{esc(name)}</td><td style="{TDR}">{s["n"]}</td>'
            f'<td style="{TDR}">{s["win_rate"] * 100:.0f}%</td>'
            f'<td style="{TDR}">{colored(money(s["total"], True), s["total"], bold=True)}</td>'
            f'<td style="{TDR}">{colored(money(s["avg_win"], True), s["avg_win"]) if s["avg_win"] is not None else "&ndash;"}</td>'
            f'<td style="{TDR}">{colored(money(s["avg_loss"], True), s["avg_loss"]) if s["avg_loss"] is not None else "&ndash;"}</td></tr>')
    if not rows:
        table = '<p style="margin:0;font-size:13px;color:#374151">No closed trades yet.</p>'
    else:
        table = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
                 f'<tr><th style="{TH}">Group</th><th style="{THR}">Trades</th><th style="{THR}">Won</th>'
                 f'<th style="{THR}">Total</th><th style="{THR}">Avg win</th><th style="{THR}">Avg loss</th></tr>'
                 f'{"".join(rows)}</table>')
    recent = sorted(closed, key=lambda t: t["exit_date"])[-RECENT_CLOSED:][::-1]
    latest = ""
    if recent:
        lines = []
        for t in recent:
            exit_day = _day(t["exit_date"])
            about = ["Option" if is_option(t) else t["direction"].title(), t["exit_reason"],
                     f"{exit_day:%b} {exit_day.day}" if exit_day else ""]
            about = " &middot; ".join(esc(x) for x in about if x)
            held = f'{t["hold_days"]:.0f}d' if t["hold_days"] is not None else "&ndash;"
            ret = colored(pct(t["return_pct"], digits=1), t["return_pct"], 1e-4) if t["return_pct"] is not None else "&ndash;"
            lines.append(
                f'<tr><td style="{TD}"><b>{esc(t["symbol"])}</b><div style="{SMALL}">{about}</div></td>'
                f'<td style="{TDR}">{held}</td><td style="{TDR}">{ret}</td>'
                f'<td style="{TDR}">{colored(money(t["pnl"], True), t["pnl"], bold=True)}</td></tr>')
        latest = ('<div style="font-size:13px;font-weight:700;margin:16px 0 6px">Latest closed tracker trades</div>'
                  '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
                  f'<tr><th style="{TH}">Trade</th><th style="{THR}">Held</th><th style="{THR}">Return</th>'
                  f'<th style="{THR}">P&amp;L</th></tr>{"".join(lines)}</table>')
    return f'<div style="{CARD}"><div style="{H2}">Closed trades so far</div>{table}{latest}</div>'


# ---------------------------------------------------------------- the whole overview

def dashboard_button(url):
    """A button that opens the Lovable dashboard (every trade and the money made). "" when there is no link."""
    if not url or not str(url).startswith("https://"):
        return ""
    link = esc(url)
    return (f'<div style="{CARD};text-align:center">'
            f'<a href="{link}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;'
            'font-weight:700;font-size:15px;padding:12px 22px;border-radius:10px">Open your dashboard</a>'
            f'<div style="{SMALL};margin-top:8px">Every trade and the money made, updated after each run.<br>'
            f'<a href="{link}" style="color:{GRAY}">{link}</a></div></div>')


def build(day=None, tracker_report=None, sleeve_report=None, text_body="", mode="trade", notices=None,
          account_id=""):
    """Returns (html, images). images = {content id: PNG bytes} for the chart (empty when there is no chart).
    sleeve_report=None reads today's saved sleeves report, if the sleeves ran today."""
    day = day or date.today()
    if sleeve_report is None:
        sleeve_report = load_sleeve_report(day)
    points = equity_points()
    closed = closed_trades(tracker_report)
    images = {}
    try:
        png = chart_png(points)
    except Exception:                                  # a drawing problem only leaves the chart out
        png = None
    if png:
        images[CHART_ID] = png

    account_id = account_id or (tracker_report or {}).get("account_id") or (sleeve_report or {}).get("account_id") or ""
    equity = _f((tracker_report or {}).get("equity")) or (points[-1][1] if points else None)
    as_of = ""
    if points and points[-1][0] != day:
        as_of = f" (saved {day_words(points[-1][0])}; today's account value was not saved)"
        equity = points[-1][1]
    generated = (tracker_report or {}).get("generated") or (sleeve_report or {}).get("generated") or ""
    when = f" &middot; numbers from the {esc(generated[11:])} run" if len(generated) > 11 else ""
    who = f" &middot; paper account {esc(account_id)}" if account_id else ""

    parts = [f'<div style="background:#f3f4f6;padding:12px 6px;font-family:{FONT};color:{INK};line-height:1.4">'
             '<div style="max-width:640px;margin:0 auto">']
    if mode == "plan":
        parts.append('<div style="background:#e0e7ff;border:1px solid #a5b4fc;color:#312e81;border-radius:10px;'
                     'padding:10px 12px;margin:0 0 12px;font-size:13px;font-weight:600">PLAN ONLY. No orders were placed.</div>')
    for note in notices or []:
        first = str(note).strip().split("\n\n")[0][:400]
        if first:
            parts.append('<div style="background:#fef3c7;border:1px solid #f59e0b;color:#78350f;border-radius:10px;'
                         f'padding:10px 12px;margin:0 0 12px;font-size:13px">{esc(first)}</div>')
    parts.append(
        f'<div style="{CARD}"><div style="{SMALL};text-transform:uppercase;letter-spacing:.06em;font-weight:600">'
        f'Portfolio overview &middot; {esc(day.strftime("%a %b %d, %Y").replace(" 0", " "))}</div>'
        f'<div style="font-size:30px;font-weight:800;margin:4px 0 2px;letter-spacing:-.01em">{money(equity)}</div>'
        f'<div style="{SMALL};margin-bottom:14px">Account value{esc(as_of)}{who}{when}</div>'
        f'{period_cards(points)}</div>')
    parts.append(dashboard_button(getattr(C, "DASHBOARD_URL", "")))
    parts.append(chart_block(points, bool(png)))
    parts.append(strategy_table(strategy_rows(tracker_report, sleeve_report, closed), sleeve_report is not None))
    parts.append(trades_table(todays_trades(day, tracker_report, closed), mode))
    parts.append(scorecard(closed))
    if text_body:
        parts.append(f'<div style="{CARD}"><div style="{H2}">Full details</div>'
                     '<pre style="margin:0;white-space:pre-wrap;word-wrap:break-word;font:12px/1.45 Menlo,Consolas,'
                     f'monospace;color:#1f2937">{esc(text_body)}</pre></div>')
    parts.append(f'<p style="{SMALL};text-align:center;margin:4px 0 0;line-height:1.5">Paper account only. Prices are '
                 "IBKR's delayed quotes at the time of the run; commissions and slippage are not included. "
                 'The full dashboards are attached.</p></div></div>')
    return "".join(parts), images


def preview_page(page, images):
    """The overview as a stand-alone page, with the chart inside it (for the browser)."""
    for cid, data in images.items():
        page = page.replace(f"cid:{cid}", "data:image/png;base64," + base64.b64encode(data).decode())
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            f'initial-scale=1"><title>Email preview</title></head><body style="margin:0">{page}</body></html>')


def main():
    points = equity_points()
    day = points[-1][0] if points else date.today()
    page, images = build(day, None, load_sleeve_report(day), "", "trade")
    os.makedirs(C.DATA_DIR, exist_ok=True)
    path = _path("email_preview.html")
    with open(path, "w") as f:
        f.write(preview_page(page, images))
    print(f"Preview of the email overview for {day.isoformat()} saved to {path}")
    print("Double-click that file to open it in your browser.")


if __name__ == "__main__":
    main()
