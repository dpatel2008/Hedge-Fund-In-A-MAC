"""
check_files.py
Checks that every tracker file on this Mac matches the tested version exactly, then runs a quick health check
(Python, ib_async, the tracker's memory files and the daily schedule). It changes nothing.
(brain_config.py, sleeve_config.py and config.py show DIFFERENT after you change a setting in them. That is expected.)

  python tracker/check_files.py
"""

import hashlib
import json
import os
import plistlib
import sys

FOLDER = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(FOLDER)
DATA = os.path.join(PROJECT, "tracker_data")
SETTINGS_FILES = ("config.py", "sleeve_config.py", "brain_config.py")
EXPECTED = {
    "allocator.py": "8db85614a251c913",
    "analytics.py": "f082ccf70445705b",
    "auto_run.py": "37322c6ba146c9c5",
    "backtest.py": "177b9d6ed00aeab2",
    "brain_config.py": "7f2910cb20735e74",
    "brain_test.py": "5069975f2f96a0e9",
    "broker_extra.py": "39e5a3c2793660ad",
    "broker_ib.py": "918bbc649faef303",
    "config.py": "ce74437fa6225838",
    "dashboard.py": "6449510eb16eb168",
    "email_overview.py": "722bf23c55d4333f",
    "engine.py": "5dde49234e3cc9a5",
    "ibc_setup.py": "076ac6f2b0c9f45e",
    "install_schedule.py": "7535a3f62c567147",
    "make_icons.py": "e9bf939d47ba5859",
    "market_brain.py": "cc658e55515bc8c7",
    "market_days.py": "6ca8d64feb82f4dd",
    "notify.py": "56b6e560faf51c3a",
    "phone_alert.py": "728231557b4749f9",
    "publish_results.py": "695db6ee2f57a8c6",
    "resume_summary.py": "00f8343c5a91cf46",
    "risk.py": "09d4d81ed0dd3154",
    "run_brain.py": "8ee19e6be6039b21",
    "run_sleeves.py": "1aa6124cf88dbc47",
    "run_tracker.py": "79feba5c2d8b0ad1",
    "setup_email.py": "37c262fa821611f8",
    "setup_phone.py": "555a1e59c2f9bd1f",
    "sleeve_config.py": "996f0c6ca0212982",
    "sleeve_engine.py": "f87d02ccfe451969",
    "sleeve_strategies.py": "9c4448dcdc2b641f",
    "start_gateway.py": "942bab0522791115",
}


def fingerprint(text):
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return hashlib.sha256("\n".join(lines).strip("\n").encode()).hexdigest()[:16]


def check_files():
    bad = 0
    for name, want in EXPECTED.items():
        path = os.path.join(FOLDER, name)
        if not os.path.exists(path):
            print(f"MISSING    {name}")
            bad += 1
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            got = fingerprint(f.read())
        if got == want:
            print(f"OK         {name}")
        elif name in SETTINGS_FILES:
            print(f"DIFFERENT  {name}   (fine if you changed a setting in it on purpose)")
        else:
            print(f"DIFFERENT  {name}   <- paste this file again")
            bad += 1
    return bad


def health():
    """Returns a list of problems found (empty = healthy). Never raises."""
    problems = []
    print()
    print("HEALTH CHECK")
    print(f"  Python {sys.version.split()[0]}")
    try:
        import ib_async
        print(f"  ib_async {getattr(ib_async, '__version__', '?')}")
    except Exception as e:
        problems.append(f"ib_async is not installed in this Python ({e}). Run: pip install ib_async")
    for module in ("numpy", "requests", "certifi"):
        try:
            __import__(module)
        except Exception:
            problems.append(f"{module} is not installed in this Python. Run: pip install {module}")

    for name in ("open_trades.json", "open_options.json", "sleeves_state.json", "allocation_state.json", "brain_state.json"):
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                json.load(f)
        except Exception:
            problems.append(f"tracker_data/{name} is damaged (it cannot be read). Send me a screenshot; do not delete it.")
    if os.path.exists(os.path.join(DATA, "PAUSE_SLEEVES")):
        print("  Note: the PAUSE_SLEEVES file exists, so the strategy sleeves only plan (no orders).")

    agents = os.path.expanduser("~/Library/LaunchAgents")
    if os.path.isdir(agents):
        for label, what, fix in (("com.dillen.sectracker", "daily run", "python tracker/install_schedule.py"),
                                 ("com.dillen.startgateway", "IB Gateway start", "python tracker/ibc_setup.py schedule")):
            path = os.path.join(agents, f"{label}.plist")
            if not os.path.exists(path):
                problems.append(f"The {what} schedule is not installed. Run: {fix}")
                continue
            try:
                with open(path, "rb") as f:
                    plist = plistlib.load(f)
                args = plist.get("ProgramArguments") or []
                program = args[0] if args else ""
                script = args[1] if len(args) > 1 else ""
                if not os.path.exists(program):
                    problems.append(f"The {what} schedule points to a Python that no longer exists ({program}). Run: {fix}")
                elif script and not os.path.exists(script):
                    problems.append(f"The {what} schedule points to a file that does not exist ({script}). Run: {fix}")
                else:
                    print(f"  {what} schedule: OK")
            except Exception:
                problems.append(f"The {what} schedule file could not be read. Run: {fix}")

    try:
        sys.path.insert(0, FOLDER)
        import market_days
        from datetime import date
        if not market_days.calendar_is_current(date.today()):
            problems.append("The market holiday list (market_days.py) has run out. Ask for an update.")
    except Exception:
        pass

    for p in problems:
        print(f"  PROBLEM: {p}")
    if not problems:
        print("  Everything looks healthy.")
    return problems


def main():
    bad = check_files()
    problems = health()
    print()
    if bad == 0 and not problems:
        print("Everything matches.")
    else:
        print(f"{bad + len(problems)} thing(s) need attention. Send me a screenshot.")


if __name__ == "__main__":
    main()
