"""
install_schedule.py
Turns the daily automatic run on or off for this Mac.

  python tracker/install_schedule.py          turn it on (9:40, 10:15, 10:55 AM, 12:30 and 3:00 PM, Monday to Friday)
  python tracker/install_schedule.py remove   turn it off

The Mac's clock must be set to Eastern Time. The tracker only trades if IB Gateway is open
and logged in to the paper account. The main run happens once a day; once it is done, the 12:30 and
3:00 PM tries are afternoon top-ups that only add new trades in free slots (auto_run.py).
"""

import os
import plistlib
import subprocess
import sys

LABEL = "com.dillen.sectracker"
TRIES = [(9, 40), (10, 15), (10, 55), (12, 30), (15, 0)]

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
DOMAIN = f"gui/{os.getuid()}"


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "install"

    run("launchctl", "bootout", f"{DOMAIN}/{LABEL}")
    if action == "remove":
        if os.path.exists(PLIST):
            os.remove(PLIST)
        print("The automatic daily run is turned off.")
        return

    if "/Desktop/" in PROJECT or "/Documents/" in PROJECT or "/Downloads/" in PROJECT:
        print("Warning: this project is inside Desktop, Documents or Downloads. macOS blocks")
        print("background jobs from those folders. Move the project to your home folder first.")

    data_dir = os.path.join(PROJECT, "tracker_data")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [os.path.abspath(sys.executable), os.path.join(PROJECT, "tracker", "auto_run.py")],
        "WorkingDirectory": PROJECT,
        "StartCalendarInterval": [
            {"Weekday": day, "Hour": hour, "Minute": minute}
            for day in range(1, 6) for hour, minute in TRIES
        ],
        "StandardOutPath": os.path.join(data_dir, "auto_run_output.log"),
        "StandardErrorPath": os.path.join(data_dir, "auto_run_output.log"),
    }
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)

    result = run("launchctl", "bootstrap", DOMAIN, PLIST)
    if result.returncode != 0:
        print("Could not turn it on:", (result.stderr or result.stdout).strip())
        return
    print("The automatic daily run is ON.")
    print("It tries at 9:40, 10:15 and 10:55 AM Eastern, Monday to Friday (the main run, once a day),")
    print("and at 12:30 and 3:00 PM for the afternoon top-ups (new trades in free slots only).")
    print("Each morning, open IB Gateway and log in to the paper account before 9:40.")


if __name__ == "__main__":
    main()
