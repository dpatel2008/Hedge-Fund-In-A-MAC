"""
start_gateway.py
Opens IB Gateway and logs in to your PAPER account for you (using IBC, set up by ibc_setup.py).
Your phone gets an alert to approve the login, because IBKR always asks you to confirm it.

  python tracker/start_gateway.py
"""

import os
import socket
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C  # noqa: E402
import phone_alert  # noqa: E402

START_SCRIPT = os.path.expanduser("~/ibc/start_gateway_paper.sh")
PORT = 4002
WAIT_SECONDS = 600


def port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def log(text):
    os.makedirs(C.DATA_DIR, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line)
    with open(os.path.join(C.DATA_DIR, "gateway_start.log"), "a") as f:
        f.write(line + "\n")


def stay_awake():
    """Keeps the Mac awake while IB Gateway logs in (the Mac's built-in 'caffeinate' tool)."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main():
    stay_awake()
    if port_open():
        log("IB Gateway is already open and logged in. Nothing to do.")
        return
    if not os.path.exists(START_SCRIPT):
        log("The auto-login is not set up yet. Run: python tracker/ibc_setup.py")
        return

    log("Starting IB Gateway.")
    phone_alert.send(
        "Approve the IBKR login",
        "IB Gateway is starting. Open the IBKR Mobile app and approve the login now.",
        "high",
    )
    output = open(os.path.join(C.DATA_DIR, "gateway_output.log"), "a")
    subprocess.Popen(                         # -inline: run here in the background, no Terminal window needed
        ["/bin/bash", START_SCRIPT, "-inline"], stdout=output, stderr=output, start_new_session=True
    )

    waited = 0
    while waited < WAIT_SECONDS:
        time.sleep(5)
        waited += 5
        if port_open():
            log("IB Gateway is logged in.")
            phone_alert.send("Gateway ready", "IB Gateway is logged in. The tracker is good to go.")
            return
    log("IB Gateway did not finish logging in.")
    phone_alert.send(
        "Gateway NOT logged in",
        "Approve the login in IBKR Mobile, or open IB Gateway and log in by hand, "
        "or the tracker cannot trade today.",
        "urgent",
    )


if __name__ == "__main__":
    main()
