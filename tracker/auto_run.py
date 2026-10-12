"""
auto_run.py
The hands-free daily run. The schedule (install_schedule.py) starts this a few times each
weekday morning. It checks that the market is open and IB Gateway is logged in, runs the
tracker in trade mode (paper account only), and emails you a summary.

Test it any time, with no orders placed:   python tracker/auto_run.py --plan-only
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

try:                                   # these two are optional, so the tracker still runs if a file is missing
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

WINDOW_START = 9 * 60 + 35     # 9:35 AM Eastern
WINDOW_END = 15 * 60 + 45      # 3:45 PM Eastern
LAST_TRY = 10 * 60 + 55        # the last scheduled try; only then do we email "could not run"
STUCK_MINUTES = 70             # a normal run takes 5-25 minutes; after this long it is stopped so a backup try can run
WATCH_EVERY_SECONDS = 30


def log(text):
    os.makedirs(C.DATA_DIR, exist_ok=True)
    line = f"{datetime.now(engine.ET).strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line)
    with open(engine.data_path("auto_run.log"), "a") as f:
        f.write(line + "\n")


def stay_awake():
    """Keeps the Mac from going to sleep until this run ends (the Mac's built-in 'caffeinate' tool).
    It works while the Mac is plugged in with the lid open. A closed lid on battery still sleeps."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def start_watchdog(minutes=None):
    """If this run is still going after `minutes` of real clock time (time asleep counts), send an urgent phone
    alert and stop it WITHOUT marking the day as done, so the next scheduled try (10:15 or 10:55) runs it again."""
    minutes = STUCK_MINUTES if minutes is None else minutes
    started = time.time()

    def watch():
        while True:
            time.sleep(WATCH_EVERY_SECONDS)
            if time.time() - started > minutes * 60:
                try:
                    log(f"STUCK: still running after {minutes} minutes, so it was stopped.")
                    phone_alert.send(
                        "Tracker was STUCK",
                        f"Today's run was still going after {minutes} minutes, so it was stopped. The next "
                        "scheduled try (10:15 or 10:55) runs it again. If both have passed, run it by hand: "
                        "python tracker/auto_run.py",
                        "urgent",
                    )
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
    Returns (text for the email, page to attach or None, number of trades placed)."""
    if not SC.SLEEVES_ENABLED:
        return "", None, 0
    try:
        import sleeve_engine
        from broker_extra import IBAdapter
        report, path = sleeve_engine.run(mode, IBAdapter(broker), auto=True)
        return sleeve_engine.summary_text(report), path, len(report["events"])
    except SystemExit as e:
        log(f"Sleeves stopped: {e}")
        return f"STRATEGY SLEEVES\n  Stopped: {e}", None, 0
    except Exception:
        details = traceback.format_exc()
        log("SLEEVES ERROR:\n" + details)
        if mode == "trade":
            phone_alert.send("Sleeves ERROR", "The strategy sleeves hit an error. The tracker itself was not affected.", "high")
        return "STRATEGY SLEEVES\n  ERROR (the tracker itself was not affected):\n" + details[-800:], None, 0


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


def main():
    plan_only = "--plan-only" in sys.argv
    mode = "plan" if plan_only else "trade"
    now = datetime.now(engine.ET)
    minutes = now.hour * 60 + now.minute
    today = now.date()
    marker = engine.data_path(f"auto_done_{today.isoformat()}.txt")
    os.makedirs(C.DATA_DIR, exist_ok=True)

    if not plan_only:
        if now.weekday() >= 5:
            log("Weekend, nothing to do.")
            return
        if not (WINDOW_START <= minutes <= WINDOW_END):
            log("Outside the 9:35 AM to 3:45 PM Eastern window, nothing to do.")
            return
        if os.path.exists(marker):
            log("Already ran today, nothing to do.")
            return
        stay_awake()
        start_watchdog()

    broker = IBBroker()
    try:
        broker.connect()
    except SystemExit:
        log("IB Gateway is not running or not logged in to the paper account.")
        if not plan_only and minutes >= LAST_TRY:
            phone_alert.send(
                "Tracker did NOT run",
                "IB Gateway was not logged in, so no trades were placed. Log in, then double-click "
                "Run Tracker on your Desktop.",
                "urgent",
            )
            notify.send_email(
                f"Tracker {today.isoformat()}: did NOT run",
                "The tracker could not connect to IB Gateway, so no trades were placed today.\n\n"
                "Open IB Gateway, choose IB API and Paper Trading, log in, then double-click "
                "Run Tracker on your Desktop to run it by hand.",
            )
        elif plan_only:
            print("Open IB Gateway, log in to the paper account, then try again.")
        return

    try:
        report, path = engine.run(mode, broker)
        subject, body = summarize(report, mode)
        attachments = [path]
        sleeve_text, sleeve_path, sleeve_trades = run_sleeves(broker, mode)
        if sleeve_text:
            body = body + "\n\n" + sleeve_text
        if sleeve_path:
            attachments.append(sleeve_path)
        if "CAPITAL BY STRATEGY" not in body:
            capital = capital_summary(broker, SC.SLEEVES_ENABLED)
            if capital:
                body = body + "\n\n" + capital
        sent = notify.send_email(subject, body, attachments)
        log(f"Finished ({mode}). Email sent: {sent}. {subject}")
        if not plan_only:
            phone_alert.send("Tracker finished", f"{subject}; sleeves: {sleeve_trades} trade(s)")
            if SC.PUBLISH_RESULTS:
                try:
                    import publish_results
                    ok, message = publish_results.publish()
                    log(f"Publish results: {message}")
                except Exception:
                    log("Publish results failed:\n" + traceback.format_exc())
            plan = report["plan"]
            no_quote = sum(1 for r in plan if "no quote" in (r.get("decision_reason") or ""))
            if plan and no_quote >= max(3, len(plan) // 2):
                phone_alert.send(
                    "Tracker: quotes failed",
                    f"{no_quote} of {len(plan)} stocks had no price from IBKR, so few or no trades "
                    "were placed. Double-click Run Tracker on your Desktop in a few minutes.",
                    "high",
                )
    except SystemExit as e:
        log(f"Stopped: {e}")
        if not plan_only:
            phone_alert.send("Tracker stopped", str(e)[:300], "high")
            notify.send_email(f"Tracker {today.isoformat()}: stopped", f"The tracker stopped with this message:\n\n{e}")
    except Exception:
        details = traceback.format_exc()
        log("ERROR:\n" + details)
        if not plan_only:
            phone_alert.send(
                "Tracker ERROR",
                "The tracker hit an error. Some orders may have been placed. Open the dashboard "
                "before running anything again.",
                "urgent",
            )
            notify.send_email(
                f"Tracker {today.isoformat()}: ERROR",
                "The tracker hit an error. Some orders may have been placed before it stopped, so open "
                "the dashboard before running anything again.\n\n" + details[-1500:],
            )
    finally:
        broker.disconnect()
        if not plan_only:
            with open(marker, "w") as f:
                f.write("done\n")


if __name__ == "__main__":
    main()
