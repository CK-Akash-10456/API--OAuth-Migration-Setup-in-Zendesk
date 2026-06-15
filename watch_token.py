"""
watch_token.py — Watch ZendeskTokenManager auto-refresh live.

Runs forever, printing the token and age every 2 seconds.
You'll see the token change when proactive refresh kicks in.

Usage:
    python watch_token.py
"""

import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import load_credentials


def main():
    mgr = load_credentials("credentials.json")

    print(f"  has_refresh: {mgr.has_refresh}")
    print(f"  TTL:         {mgr._ttl}s (refresh at {int(mgr._ttl * mgr.REFRESH_THRESHOLD)}s)")
    print()
    print(f"  {'Time':>8}  {'Age':>6}  {'Token (first 40 chars)':<42}  {'Event':<20}")
    print(f"  {'-'*8}  {'-'*6}  {'-'*42}  {'-'*20}")

    last = mgr.get_token()

    while True:
        token = mgr.get_token()
        age = mgr.age
        pct = age / mgr._ttl
        event = ""
        if token != last:
            event = "← REFRESHED"
            last = token

        ts = time.strftime("%H:%M:%S")
        print(f"  {ts:>8}  {age:>5.1f}s  {token[:40]:<42}  {event:<20}")

        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped.")
