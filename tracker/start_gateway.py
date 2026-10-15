"""
start_gateway.py
Opens IB Gateway and logs in to your PAPER account for you (using IBC, set up by ibc_setup.py), then checks that
IBKR is really sending prices. If IB Gateway is open but IBKR sends nothing (after many dropped connections it can
stay "logged in" while every price request goes unanswered), it closes IB Gateway and opens it again.
Your phone gets an alert to approve the login if IBKR asks you to confirm it.

  python tracker/start_gateway.py            open it if needed, check the prices, restart it if they are missing
  python tracker/start_gateway.py restart    close IB Gateway and open it again, then check the prices
  python tracker/start_gateway.py check      only check the prices (changes nothing)
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
WAIT_SECONDS = 600            # how long a login may take (it includes approving it on your phone)
CHECK_CLIENT_ID = 31          # the price check's own connection number (the tracker uses 17, run_sleeves 18)
CHECK_SECONDS = 20            # each question in the price check gives up after this long (normally 1-2 seconds)
CHECK_TRIES = 3               # right after a login the data connections need a moment, so the check tries again
CHECK_PAUSE = 20
CHECK_SYMBOL = "SPY"
QUOTE_SECONDS = 15            # how long the check waits for a (delayed) price quote
GATEWAY_PROCESS = "ibcalpha.ibc.IbcGateway"    # appears only in the command line of the IB Gateway that IBC runs
STOP_SECONDS = 60


def port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def log(text):
    os.makedirs(C.DATA_DIR, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
    print(line, flush=True)
    with open(os.path.join(C.DATA_DIR, "gateway_start.log"), "a") as f:
        f.write(line + "\n")


def stay_awake():
    """Keeps the Mac awake while this runs (the Mac's built-in 'caffeinate' tool)."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ------------------------------------------------------------ the price check

def _positive(*values):
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0:
            return float(v)
    return None


def data_check(symbol=CHECK_SYMBOL, seconds=CHECK_SECONDS):
    """Asks IB Gateway for one stock's details, a few days of prices and a price quote: the three things the tracker
    needs. Returns (True, detail) when all three arrive, (False, what was missing) when they do not, and (None, why)
    when the check itself could not run (then nothing should be restarted because of it). Never raises."""
    try:
        from ib_async import IB, Stock
    except Exception as e:
        return None, f"the price check could not start ({type(e).__name__}: {e})"
    ib = IB()
    ib.RequestTimeout = seconds
    try:
        ib.connect(C.IB_HOST, PORT, clientId=CHECK_CLIENT_ID, timeout=seconds)
    except Exception as e:
        _disconnect(ib)
        return False, f"IB Gateway did not accept a connection ({type(e).__name__})"
    try:
        contract = Stock(symbol, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)
        except Exception:
            pass
        if not contract.conId:
            return False, f"IBKR did not answer a lookup for {symbol}"
        try:
            bars = ib.reqHistoricalData(contract, "", "5 D", "1 day", "TRADES", True, 1, timeout=seconds)
        except Exception:
            bars = []
        if not bars:
            return False, f"IBKR sent no price history for {symbol}"
        price = None
        try:
            ib.reqMarketDataType(C.IB_MARKET_DATA_TYPE)
            ticker = ib.reqMktData(contract, "", False, False)
            waited = 0.0
            while price is None and waited < QUOTE_SECONDS:
                ib.sleep(0.5)
                waited += 0.5
                price = _positive(ticker.marketPrice(), ticker.last, ticker.close, ticker.bid, ticker.ask)
            ib.cancelMktData(contract)
        except Exception:
            price = None
        if price is None:
            return False, f"IBKR sent no price quote for {symbol}"
        return True, f"{symbol} quote {price:g}, last close {bars[-1].close} on {bars[-1].date}"
    except Exception as e:
        return False, f"the price check failed ({type(e).__name__}: {e})"
    finally:
        _disconnect(ib)


def _disconnect(ib):
    try:
        ib.disconnect()
    except Exception:
        pass


def check_with_retries(tries=CHECK_TRIES, pause=CHECK_PAUSE):
    """data_check, tried a few times (the data connections can take a moment after a login)."""
    ok, detail = None, ""
    for attempt in range(tries):
        ok, detail = data_check()
        if ok or ok is None:
            return ok, detail
        if attempt + 1 < tries:
            time.sleep(pause)
    return ok, detail


# ------------------------------------------------------------ opening and closing IB Gateway

def gateway_running():
    try:
        return subprocess.run(["/usr/bin/pgrep", "-f", GATEWAY_PROCESS], capture_output=True).returncode == 0
    except OSError:
        return False


def stop_gateway(seconds=STOP_SECONDS):
    """Closes IB Gateway: a normal close first, a forced one if it is still open after half the time.
    Returns True when it is closed."""
    for signal_name in ("TERM", "KILL"):
        try:
            subprocess.run(["/usr/bin/pkill", f"-{signal_name}", "-f", GATEWAY_PROCESS], capture_output=True)
        except OSError:
            return False
        waited = 0
        while waited < seconds // 2:
            time.sleep(2)
            waited += 2
            if not gateway_running() and not port_open():
                time.sleep(3)                      # let IBC finish before the next start
                return True
    return not port_open()


def start(alert=True):
    """Opens IB Gateway with IBC and waits for the login. Returns True when it is logged in."""
    if port_open():
        return True
    if not os.path.exists(START_SCRIPT):
        log("The auto-login is not set up yet. Run: python tracker/ibc_setup.py")
        return False
    log("Starting IB Gateway.")
    if alert:
        phone_alert.send(
            "Approve the IBKR login",
            "IB Gateway is starting. If IBKR Mobile asks you to approve the login, approve it now.",
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
            return True
    log("IB Gateway did not finish logging in.")
    if alert:
        phone_alert.send(
            "Gateway NOT logged in",
            "Approve the login in IBKR Mobile, or double-click Start Gateway on your Desktop, "
            "or the tracker cannot trade today.",
            "urgent",
        )
    return False


def restart(alert=True):
    """Closes IB Gateway, opens it again and checks the prices. Returns True when prices arrive."""
    log("Closing IB Gateway so it can start fresh.")
    if not stop_gateway():
        log("Could not close IB Gateway. Quit it by hand (IbcGateway in the Dock), then double-click Start Gateway.")
        if alert:
            phone_alert.send("Gateway problem", "IBKR is not sending prices and IB Gateway would not close. Quit "
                             "it by hand, then double-click Start Gateway on your Desktop.", "urgent")
        return False
    if not start(alert):
        return False
    time.sleep(10)
    ok, detail = check_with_retries()
    if ok or ok is None:
        log(f"After the restart IBKR is sending prices ({detail}).")
        if alert:
            phone_alert.send("Gateway ready", "IB Gateway was restarted and IBKR is sending prices again.")
        return True
    log(f"Even after a restart, {detail}.")
    if alert:
        phone_alert.send("IBKR sends no prices", f"Even after restarting IB Gateway, {detail}. The tracker will "
                         "not be able to trade until this is fixed.", "urgent")
    return False


def ensure(alert=True):
    """Makes sure IB Gateway is open, logged in, and that IBKR is sending prices, restarting it once if they are
    missing. Returns True when prices arrive (or when they could not be checked). Never raises."""
    try:
        if not port_open():
            if not start(alert):
                return False
            time.sleep(10)
            ok, detail = check_with_retries()
            if ok is not False and alert:
                phone_alert.send("Gateway ready", "IB Gateway is logged in. The tracker is good to go.")
        else:
            ok, detail = data_check()
            if ok is False:
                time.sleep(5)                          # one quick second try before restarting anything
                ok, detail = data_check()
        if ok is None:
            log(f"IB Gateway is open and logged in. ({detail})")
            return True
        if ok:
            log(f"IB Gateway is logged in and IBKR is sending prices ({detail}).")
            return True
        log(f"IB Gateway is open, but {detail}. Restarting it.")
        return restart(alert)
    except Exception as e:                             # the check must never stop the tracker's run
        log(f"The IB Gateway check hit an error: {type(e).__name__}: {e}")
        return True


def main():
    stay_awake()
    command = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if command == "check":
        if not port_open():
            log("IB Gateway is not open.")
            return
        ok, detail = data_check()
        if ok is None:
            log(detail)
        else:
            log(("IBKR is sending prices: " if ok else "PROBLEM: ") + detail)
    elif command == "restart":
        restart()
    else:
        ensure()


if __name__ == "__main__":
    main()
