"""
did_it_run.py
A watchdog that does not depend on the Mac. GitHub runs it on weekdays in the early afternoon (New York time), see
.github/workflows/tracker_check.yml. It reads results.json, which the Mac uploads after every run. If today is a
market day and today's results are not there, it writes a short message and exits with code 1, and the workflow
emails it to you. It never touches the Mac or the account.

  python tracker/did_it_run.py
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_days  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(PROJECT, "results.json")
MESSAGE_FILE = os.path.join(PROJECT, "tracker_alert.txt")

ADVICE = """What to check, in order:
1. Is the Mac on, awake and online, with the lid open? (A closed lid puts it to sleep and nothing runs.)
2. Is IB Gateway open and logged in? If not, double-click Start Gateway on the Desktop.
3. Double-click Run Tracker on the Desktop. It does nothing if today's run already finished.
4. If the morning email arrived but this alert came anyway, only the dashboard upload failed
   (the email says why, for example a GitHub sign-in that expired).
"""


def check(now=None, results_path=RESULTS):
    """Returns (ok, message)."""
    now = now or datetime.now(ZoneInfo("America/New_York"))
    today = now.date()
    if now.weekday() >= 5 or not market_days.is_trading_day(today):
        return True, f"The market is closed on {today.isoformat()}, so there is nothing to check."
    try:
        with open(results_path) as f:
            updated = str(json.load(f).get("updated") or "")
    except (OSError, ValueError, AttributeError):
        updated = ""
    if updated.startswith(today.isoformat()):
        return True, f"Today's results are in (uploaded {updated})."
    last = updated or "never"
    return False, (f"The tracker has not reported today ({today.strftime('%A %B %d, %Y')}). "
                   f"Its last upload was {last}.\n\n{ADVICE}")


def main():
    ok, message = check()
    print(message)
    if not ok:
        with open(MESSAGE_FILE, "w") as f:
            f.write(message + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
