"""
run_brain.py
Usage (from the project folder, with the virtual environment on):

  python tracker/run_brain.py fetch   downloads as many years of market history as IBKR gives (IB Gateway must be on,
                                      takes about 5 minutes, only needed once)
  python tracker/run_brain.py test    tests all three brain parts on that history and says which ones help
                                      (no IBKR needed, takes a few minutes)
  python tracker/run_brain.py show    today's market weather and what the brain would change (no IBKR needed)

Extra option:  --no-open   do not open the page in the browser
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C  # noqa: E402
import sleeve_config as SC  # noqa: E402


def open_file(path):
    try:
        subprocess.run(["open", path], check=False)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["fetch", "test", "show"])
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    os.makedirs(C.DATA_DIR, exist_ok=True)

    import market_brain as M
    import sleeve_engine as E
    today_iso = E.today_et().isoformat()

    if args.mode == "fetch":
        from broker_extra import IBAdapter
        from broker_ib import IBBroker
        C.IB_CLIENT_ID = SC.MANUAL_CLIENT_ID     # a different connection number, so it never blocks the scheduled run
        broker = IBBroker()
        broker.connect()
        try:
            print(f"Downloading market history for {len(M.symbols())} funds, as many years as IBKR gives.")
            print("Each one can take up to a minute. You can leave it running.\n")
            results = M.fetch_long(IBAdapter(broker), today_iso)
        finally:
            broker.disconnect()
        got = [r for r in results.values() if r]
        print(f"\nDone. {len(got)} of {len(results)} funds saved in {M.hist_dir()}")
        if results.get("SPY"):
            print(f"SPY history: {results['SPY']['first']} to {results['SPY']['last']} ({results['SPY']['years']:.1f} years)")
        print("Next: python tracker/run_brain.py test")
        return

    if args.mode == "test":
        import brain_test
        print("Testing the market brain. Nothing is traded.")
        out = brain_test.run()
        print()
        print(brain_test.format_text(out))
        brain_test.save_json(out, os.path.join(C.DATA_DIR, "brain_test.json"))
        path = brain_test.write_report(out, os.path.join(C.DATA_DIR, "brain_test.html"))
        print(f"\nSaved: {path}")
        if not args.no_open:
            open_file(path)
        return

    import allocator
    try:
        _, allocations = allocator.current(today_iso, save=False)
    except Exception:
        allocations = allocator.base_allocations()
    store = E.CacheOnlyStore(None, today_iso)
    b = M.today(store, today_iso, allocations, save=False)
    if not b:
        print("Every part of the market brain is switched off in brain_config.py.")
        return
    print("\n".join(M.summary_lines(b)))
    path = os.path.join(C.DATA_DIR, "brain.html")
    with open(path, "w") as f:
        f.write(M.page(b))
    print(f"\nSaved: {path}")
    if not args.no_open:
        open_file(path)


if __name__ == "__main__":
    main()
