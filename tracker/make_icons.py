"""
make_icons.py
Run once:  python tracker/make_icons.py
Puts five double-click icons on your Desktop:

  Run Tracker.command      runs today's full run now, the same as the scheduled morning run (tracker, strategy
                           sleeves, email). Use it when an alert says the run did not happen. It does nothing
                           if today's run already finished.
  Plan Only.command        looks at today's plan and places NO orders
  Open Dashboard.command   opens the latest dashboard
  Capital Report.command   shows how much money each strategy is using (places NO orders)
  Strategies Plan.command  shows what the strategy sleeves would do today (places NO orders)
"""

import os
import stat
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.abspath(sys.executable)
DESKTOP = os.path.expanduser("~/Desktop")


def script(lines):
    return "#!/bin/bash\n" + "\n".join(lines) + "\n"


def main():
    cd = f'cd "{PROJECT}" || exit 1'
    run = f'"{PYTHON}" tracker/run_tracker.py'
    icons = {
        "Run Tracker.command": script([
            cd,
            f'"{PYTHON}" tracker/auto_run.py --manual',
            'echo ""',
            'echo "Done. You can close this window."',
        ]),
        "Plan Only.command": script([
            cd,
            f"{run} plan",
            'echo ""',
            'echo "Plan only. No orders were placed. You can close this window."',
        ]),
        "Open Dashboard.command": script([
            cd,
            'open "tracker_data/tracker_dashboard.html"',
        ]),
        "Capital Report.command": script([
            cd,
            f'"{PYTHON}" tracker/run_sleeves.py capital',
            'echo ""',
            'echo "No orders were placed. You can close this window."',
        ]),
        "Strategies Plan.command": script([
            cd,
            f'"{PYTHON}" tracker/run_sleeves.py plan',
            'echo ""',
            'echo "Plan only. No orders were placed. You can close this window."',
        ]),
    }
    os.makedirs(DESKTOP, exist_ok=True)
    for name, text in icons.items():
        path = os.path.join(DESKTOP, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print("Made:", path)
    print()
    print("Double-click any of them on your Desktop. If your Mac asks, right-click the icon and choose Open.")


if __name__ == "__main__":
    main()
