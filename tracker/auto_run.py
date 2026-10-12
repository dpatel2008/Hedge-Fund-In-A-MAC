"""
auto_run.py
The hands-free daily run. The schedule (install_schedule.py) starts this a few times each
weekday morning. It checks that the market is open and IB Gateway is logged in, runs the
tracker in trade mode (paper account only), runs the strategy sleeves, and emails you a summary.

Test it any time, with no orders placed:   python tracker/auto_run.py --plan-only
Run today's run by hand (the Run Tracker icon does this):   python tracker/auto_run.py --manual

Safety rules:
  * It runs once a day. A finished run leaves a marker file, so later tries that day do nothing.
  * If the connection to IB Gateway is lost, or IBKR's data has not loaded, NO marker is left, so the next
    scheduled try (10:15 or 10:55) runs it again. A second try never doubles the day's new trades.
  * If a run makes no progress for 20 minutes (or runs for 2.5 hours), it is stopped, you get a phone alert,
    and the next scheduled try runs it again.
"""

import os
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C  # noqa: E402
import engine  # noqa: E402
import notify  # noqa: E402
from broker_ib import IBBroker  # noqa: E402

try:                                   # these are optional, so the tracker still runs if a file is missing
    import phone_alert  # noqa: E402
except ImportError:
    class phone_alert:                 # noqa: N801
        @staticmethod
        def send(*args, **kwargs):
            return False
try:
    import sleeve_config as SC  # noqa: E402
except ImportError:
    class SC:                          # noqa: N801
        SLEEVES_ENABLED = False
        PUBLISH_RESULTS = False
try:
    import market_days  # noqa: E402
except ImportError:
    market_days = None

WINDOW_START = 9 * 60 + 35     # 9:35 AM Eastern
STOP_BEFORE_CLOSE = 15         # no new run in the last 15 minutes before the close (3:45 PM on a normal day)
LAST_TRY = 10 * 60 + 55        # the last scheduled try; only then do we email "could not run"
STALL_MINUTES = 20             # no progress at all for this long = stuck (the longest normal quiet spell is ~11 minutes)
MAX_MINUTES = 150              # a run is stopped after this long no matter what
WATCH_EVERY_SECONDS = 30

_beat = [time.time()]          # the last time the run showed a sign of life (printed anything)


def beat():
    _beat[0] = time.time()


class _Heartbeat:
    """Wraps the screen output: every line the run prints counts as a sign of life for the watchdog."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        beat()
        return self._stream.write(text)

    def flush(self):
        try:
            return self._stream.flush()
        except Exception:
            return None

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_heartbeat():
    """Prints show up in the log file right away (not in big delayed chunks), and count as signs of life."""
    originals = (sys.stdout, sys.stderr)
    for stream in originals:
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    sys.stdout, sys.stderr = _Heartbeat(sys.stdout), _Heartbeat(sys.stderr)
    return originals


def log(text):
    beat()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    line = f"{datetime.now(engine.ET).strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line)
    try:
        with open(engine.data_path("auto_run.log"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def clock_words(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def now_minutes():
    now = datetime.now(engine.ET)
    return now.hour * 60 + now.minute


def next_try_words(minutes):
    if minutes < LAST_TRY:
        return "The next scheduled try (10:15 or 10:55) runs it again by itself."
    return "There are no more scheduled tries today. Double-click Run Tracker on your Desktop to run it by hand."


def stay_awake():
    """Keeps the Mac from going to sleep until this run ends (the Mac's built-in 'caffeinate' tool).
    It works while the Mac is plugged in with the lid open. A closed lid on battery still sleeps."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def start_watchdog(stall_minutes=None, max_minutes=None, stop=None):
    """Stops a stuck run WITHOUT marking the day as done, so the next scheduled try runs it again.
    Stuck = nothing printed for `stall_minutes`, or still going after `max_minutes` (real clock time, so
    time asleep counts too). `stop` (a threading.Event) ends the watch early."""
    stall = STALL_MINUTES if stall_minutes is None else stall_minutes
    limit = MAX_MINUTES if max_minutes is None else max_minutes
    started = time.time()
    beat()

    def watch():
        while stop is None or not stop.is_set():
            time.sleep(WATCH_EVERY_SECONDS)
            if stop is not None and stop.is_set():
                return
            now = time.time()
            quiet, total = now - _beat[0], now - started
            if quiet <= stall * 60 and total <= limit * 60:
                continue
            why = (f"no progress for {quiet / 60:.0f} minutes" if quiet > stall * 60
                   else f"still running after {total / 60:.0f} minutes")
            try:
                log(f"STUCK: {why}, so it was stopped.")
                phone_alert.send("Tracker was STUCK", f"Today's run was stopped ({why}). {next_try_words(now_minutes())}",
                                 "urgent")
            finally:
                os._exit(3)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def summarize(report, mode):
    plan = report["plan"]
    new_trades = sum(1 for r in plan if r.get("decision") == "FILLED")
    closed_count = sum(1 for line in report["events"] if line.startswith("Closed"))
    stats = report["trade_stats"]
    open_pnl = sum(o["pnl"] for o in report["open_rows"])
    option_pnl = sum(o["pnl"] for o in report["option_rows"] if o["pnl"] is not None)

    lines = []
    if mode == "plan":
        lines.append("PLAN ONLY. No orders were placed.")
        lines.append("")
    lines.append("WHAT HAPPENED")
    if report["events"]:
        lines += [f"  - {e}" for e in report["events"]]
    else:
        lines.append("  - No orders were placed.")

    planned = [r for r in plan if r.get("decision") in ("TRADE", "FILLED", "NOT FILLED")]
    skipped = [r for r in plan if r.get("decision") == "SKIP"]
    lines += ["", f"PLAN: {len(planned)} trade(s), {len(skipped)} skipped"]
    reasons = Counter(r.get("decision_reason", "") for r in skipped)
    for reason, count in reasons.most_common(4):
        lines.append(f"  - {count} skipped: {reason}")

    lines += ["", "OPEN STOCK TRADES"]
    if report["open_rows"]:
        for o in report["open_rows"]:
            lines.append(
                f"  - {o['symbol']} {o['direction']} {o['qty']} sh, in at {o['entry_price']:.2f}, "
                f"now {o['price']:.2f}, P&L ${o['pnl']:,.0f}, exit by {o['exit_by']}"
            )
    else:
        lines.append("  - none")

    lines += ["", "OPEN OPTIONS"]
    if report["option_rows"]:
        for o in report["option_rows"]:
            pnl = f"${o['pnl']:,.0f}" if o["pnl"] is not None else "no price"
            lines.append(f"  - {o['symbol']} {o['legs']} exp {o['expiry']} x{o['qty']}, P&L {pnl}")
    else:
        lines.append("  - none")

    expo = report["exposure"]
    lines += [
        "",
        "ACCOUNT",
        f"  Account value: ${report['equity']:,.0f}",
        f"  Open P&L: stocks ${open_pnl:,.0f}, options ${option_pnl:,.0f}",
        f"  Closed P&L so far: ${stats['total_pnl']:,.0f} over {stats['n']} trade(s)"
        + (f", hit rate {stats['hit_rate'] * 100:.0f}%" if stats["hit_rate"] is not None else ""),
        f"  Gross exposure ${expo['gross']:,.0f}, net ${expo['net']:,.0f}, "
        f"beta-adjusted net after hedge ${expo['net_beta_after_hedge']:,.0f}",
        "",
        "The full dashboard is attached. Open it in a browser.",
        "Paper account only. Results use delayed quotes and ignore commissions and slippage.",
    ]
    subject = (
        f"Tracker {report['generated'][:10]}: {new_trades} new trade(s), {closed_count} closed, "
        f"account ${report['equity']:,.0f}"
    )
    if mode == "plan":
        subject += " (plan only)"
    return subject, "\n".join(lines)


def run_sleeves(broker, mode):
    """Runs the strategy sleeves after the tracker. Never lets a sleeve problem stop the tracker.
    Returns (text for the email, page to attach or None, number of trades placed, True if it should be tried again)."""
    if not SC.SLEEVES_ENABLED:
        return "", None, 0, False
    try:
        import sleeve_engine
        from broker_extra import IBAdapter
        report, path = sleeve_engine.run(mode, IBAdapter(broker), auto=True)
        return sleeve_engine.summary_text(report), path, len(report["events"]), False
    except SystemExit as e:
        log(f"Sleeves stopped: {e}")
        return f"STRATEGY SLEEVES\n  Stopped: {e}", None, 0, False
    except Exception as e:
        if type(e).__name__ == "RetryLater":
            log(f"Sleeves waited: {e}")
            return f"STRATEGY SLEEVES\n  Waited: {e}", None, 0, True
        if hasattr(broker, "is_connected") and not broker.is_connected():
            log(f"Sleeves stopped: lost the connection to IB Gateway ({type(e).__name__}: {e})")
            return ("STRATEGY SLEEVES\n  Stopped part way: the connection to IB Gateway was lost. Anything already "
                    "traded was saved, and the next try carries on from there."), None, 0, True
        details = traceback.format_exc()
        log("SLEEVES ERROR:\n" + details)
        if mode == "trade":
            phone_alert.send("Sleeves ERROR", "The strategy sleeves hit an error. The tracker itself was not affected.", "high")
        return "STRATEGY SLEEVES\n  ERROR (the tracker itself was not affected):\n" + details[-800:], None, 0, False


def capital_summary(broker, sleeves_on):
    """The "capital by strategy" table for the email. Never stops the tracker."""
    try:
        import allocator
        from broker_extra import IBAdapter
        today = datetime.now(engine.ET).date().isoformat()
        rows, totals, state = allocator.report(IBAdapter(broker), today, compute=sleeves_on)
        return allocator.capital_text(rows, totals, state)
    except (Exception, SystemExit) as e:
        log(f"Capital table skipped: {e}")
        return ""


def take_lock():
    """Only one run at a time (the schedule and the Run Tracker icon could overlap). Returns the lock file's path,
    or None if another run is going right now. A lock left by a run that was stopped is taken over."""
    path = engine.data_path("auto_running.lock")
    try:
        with open(path) as f:
            parts = f.read().split()
        pid = int(parts[0]) if parts else 0
        started = datetime.fromisoformat(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError):
        pid, started = 0, None
    try:
        too_old = started is not None and (datetime.now(engine.ET) - started).total_seconds() > (MAX_MINUTES + 30) * 60
    except (TypeError, ValueError):
        too_old = False
    if pid and pid != os.getpid() and not too_old:
        try:
            os.kill(pid, 0)                      # is that run still alive?
            return None
        except ProcessLookupError:
            pass                                 # it is gone: take over
        except PermissionError:
            return None
        except OSError:
            pass
    with open(path, "w") as f:
        f.write(f"{os.getpid()} {datetime.now(engine.ET).isoformat()}\n")
    return path


def release_lock(path):
    if not path:
        return
    try:
        with open(path) as f:
            pid = int((f.read().split() or ["0"])[0])
        if pid == os.getpid():
            os.remove(path)
    except (OSError, ValueError):
        pass


def send_email(subject, body, attachments=None):
    try:
        return notify.send_email(subject, body, attachments)
    except Exception as e:                       # an email problem must never stop the run
        log(f"Email failed: {e}")
        return False


def main():
    originals = install_heartbeat()
    try:
        _main()
    finally:
        sys.stdout, sys.stderr = originals


def _main():
    plan_only = "--plan-only" in sys.argv
    manual = "--manual" in sys.argv
    mode = "plan" if plan_only else "trade"
    now = datetime.now(engine.ET)
    minutes = now.hour * 60 + now.minute
    today = now.date()
    marker = engine.data_path(f"auto_done_{today.isoformat()}.txt")
    os.makedirs(C.DATA_DIR, exist_ok=True)

    if not plan_only:
        close = market_days.close_minutes(today) if market_days else 16 * 60
        if now.weekday() >= 5:
            log("Weekend, nothing to do.")
            return
        if market_days and not market_days.is_trading_day(today):
            log("The stock market is closed today (holiday), nothing to do.")
            return
        if not (WINDOW_START <= minutes <= close - STOP_BEFORE_CLOSE):
            log(f"Outside the 9:35 AM to {clock_words(close - STOP_BEFORE_CLOSE)} Eastern window, nothing to do.")
            return
        if os.path.exists(marker):
            log("Already ran today, nothing to do." + (" (Today's email has the results.)" if manual else ""))
            return
    lock = take_lock()
    if lock is None:
        log("Another run is going right now, so this one did nothing. Wait for its email.")
        return
    try:
        _run_locked(plan_only, manual, mode, minutes, today, marker)
    finally:
        release_lock(lock)


def _run_locked(plan_only, manual, mode, minutes, today, marker):
    if not plan_only:
        stay_awake()
        start_watchdog()
        if manual:
            log("Started by hand (Run Tracker icon).")
        if market_days and not market_days.calendar_is_current(today):
            log("Note: the holiday list in market_days.py has run out. Ask for an update.")

    broker = IBBroker()
    try:
        broker.connect()
    except (SystemExit, Exception) as e:
        log(f"IB Gateway is not running, not logged in to the paper account, or not answering. {e}")
        if not plan_only and (minutes >= LAST_TRY or manual):
            phone_alert.send(
                "Tracker did NOT run",
                "IB Gateway was not logged in or not answering, so no trades were placed. Log in, then "
                "double-click Run Tracker on your Desktop.",
                "urgent",
            )
            send_email(
                f"Tracker {today.isoformat()}: did NOT run",
                "The tracker could not connect to IB Gateway, so no trades were placed today.\n\n"
                "Open IB Gateway, choose IB API and Paper Trading, log in, then double-click "
                f"Run Tracker on your Desktop to run it by hand.\n\nDetails: {e}",
            )
        elif plan_only:
            print("Open IB Gateway, log in to the paper account, then try again.")
        return

    retry = False
    try:
        if not plan_only:
            leftover = broker.cancel_leftover_orders() if hasattr(broker, "cancel_leftover_orders") else 0
            if leftover:
                log(f"Cancelled {leftover} order(s) left waiting by an earlier try today.")

        # 1. the SEC-filing tracker
        report, path, problem, problem_kind = None, None, "", ""
        try:
            report, path = engine.run(mode, broker)
        except engine.RetryLater as e:
            problem, problem_kind, retry = f"The tracker waited: {e}", "waited", True
            log(problem)
        except SystemExit as e:
            problem, problem_kind = f"The tracker stopped: {e}", "stopped"
            log(problem)
        except Exception:
            details = traceback.format_exc()
            log("ERROR:\n" + details)
            problem, problem_kind = ("The tracker hit an error. Some orders may have been placed before it stopped, "
                                     "so open the dashboard before running anything again.\n\n" + details[-1500:]), "error"
        connected = broker.is_connected() if hasattr(broker, "is_connected") else True
        if not connected:
            retry = True
            if problem_kind in ("error", ""):
                problem_kind = "lost"
                problem = ("Lost the connection to IB Gateway part way through the tracker. Anything already "
                           "traded was saved, and the next try carries on from there.\n\n" + problem).strip()

        # 2. the strategy sleeves (they run even if the tracker part had a problem)
        sleeve_text, sleeve_path, sleeve_trades = "", None, 0
        if connected:
            sleeve_text, sleeve_path, sleeve_trades, sleeve_retry = run_sleeves(broker, mode)
            retry = retry or sleeve_retry
            if hasattr(broker, "is_connected") and not broker.is_connected():
                retry = True
        elif SC.SLEEVES_ENABLED:
            sleeve_text = "STRATEGY SLEEVES\n  Skipped: the connection to IB Gateway was lost."

        # 3. the email and phone alerts
        if report is not None:
            subject, body = summarize(report, mode)
            if problem:
                body = problem + "\n\n" + body
        else:
            subject = f"Tracker {today.isoformat()}: tracker part {problem_kind.upper() or 'DID NOT FINISH'}"
            if SC.SLEEVES_ENABLED:
                subject += f", sleeves {sleeve_trades} trade(s)"
            body = problem or "The tracker part did not finish."
        end_minutes = now_minutes()
        if retry and not plan_only:
            body = (f"The run did not fully finish (lost the connection or IBKR's data was not ready). "
                    f"{next_try_words(end_minutes)}\n\n" + body)
            subject += " - will try again" if end_minutes < LAST_TRY else " - did NOT finish"
        if sleeve_text:
            body = body + "\n\n" + sleeve_text
        still_connected = broker.is_connected() if hasattr(broker, "is_connected") else True
        if "CAPITAL BY STRATEGY" not in body and still_connected:
            capital = capital_summary(broker, SC.SLEEVES_ENABLED)
            if capital:
                body = body + "\n\n" + capital
        attachments = [p for p in (path, sleeve_path) if p]
        sent = send_email(subject, body, attachments)
        log(f"Finished ({mode}). Email sent: {sent}. {subject}")

        if not plan_only:
            if problem_kind == "error":
                phone_alert.send("Tracker ERROR", "The tracker hit an error. Some orders may have been placed. Open "
                                 "the dashboard before running anything again.", "urgent")
            elif problem_kind == "stopped":
                phone_alert.send("Tracker stopped", problem[:300], "high")
            if retry:
                if end_minutes < LAST_TRY:
                    phone_alert.send("Tracker will try again", next_try_words(end_minutes), "high")
                else:
                    phone_alert.send("Tracker did NOT finish", next_try_words(end_minutes), "urgent")
            elif report is not None:
                phone_alert.send("Tracker finished", f"{subject}; sleeves: {sleeve_trades} trade(s)")
            if report is not None and SC.PUBLISH_RESULTS:
                try:
                    import publish_results
                    ok, message = publish_results.publish()
                    log(f"Publish results: {message}")
                except Exception:
                    log("Publish results failed:\n" + traceback.format_exc())
            if report is not None:
                plan = report["plan"]
                no_quote = sum(1 for r in plan if "no quote" in (r.get("decision_reason") or ""))
                if plan and no_quote >= max(3, len(plan) // 2):
                    phone_alert.send(
                        "Tracker: quotes failed",
                        f"{no_quote} of {len(plan)} stocks had no price from IBKR, so few or no trades "
                        "were placed. Double-click Run Tracker on your Desktop in a few minutes.",
                        "high",
                    )
    finally:
        broker.disconnect()
        if not plan_only and not retry:
            with open(marker, "w") as f:
                f.write("done\n")


if __name__ == "__main__":
    main()
