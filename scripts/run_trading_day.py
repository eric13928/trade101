"""
Full-session orchestrator: runs premarket_scan.py while premarket, then
automatically switches to pole_watch.py the moment regular hours begin --
so a single "start" covers the whole day without needing to be told again
when the market opens.

Confirmed live: moomoo's market_us state reads "AFTERNOON" for the entire
regular session (no separate "MORNING" state for US, unlike HK) -- so
AFTERNOON is what this treats as "regular hours, switch to pole_watch."

Unlike the other scanner scripts, this one loops forever on its own (no
outer bash "while true" wrapper needed) -- it just needs to be started
once via the Monitor tool.
"""
import subprocess
import sys
import time

from moomoo import OpenQuoteContext, RET_OK

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CHUNK_SECONDS = 120
POLL_IDLE_SECONDS = 60  # how often to recheck market state when there's nothing to run (premarket not yet started, or closed)


def get_market_state(ctx):
    ret, data = ctx.get_global_state()
    if ret != RET_OK:
        return None
    return data.get("market_us")


def main(market="US"):
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        while True:
            state = get_market_state(ctx)
            if state == "PRE_MARKET_BEGIN":
                print(f"[orchestrator] {market} premarket -- running premarket_scan.py")
                subprocess.run([sys.executable, "-u", "premarket_scan.py",
                                "--market", market, "--duration-seconds", str(CHUNK_SECONDS)])
            elif state == "AFTERNOON":
                print(f"[orchestrator] {market} regular hours -- running pole_watch.py")
                subprocess.run([sys.executable, "-u", "pole_watch.py",
                                "--market", market, "--duration-seconds", str(CHUNK_SECONDS)])
            else:
                print(f"[orchestrator] {market} market_us={state} -- nothing to run, "
                      f"checking again in {POLL_IDLE_SECONDS}s")
                time.sleep(POLL_IDLE_SECONDS)
    finally:
        ctx.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Auto-switching premarket -> regular-hours orchestrator")
    parser.add_argument("--market", default="US", choices=["US"])
    args = parser.parse_args()
    main(args.market)
