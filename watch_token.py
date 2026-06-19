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
    """Poll the token manager forever, printing token + age every 2 seconds.

    Loads credentials, prints the TTL/refresh threshold once, then loops:
    each tick calls :meth:`client.ZendeskTokenManager.get_token` (which triggers
    a proactive refresh once ~80% of the TTL has elapsed) and reads
    :attr:`client.ZendeskTokenManager.age`. When the returned token differs from
    the previous one, the row is flagged ``← REFRESHED`` so you can watch
    auto-refresh happen live.

    Reference:
        The script entry point, run under the ``__main__`` guard which traps
        ``KeyboardInterrupt`` for a clean exit. Reads ``credentials.json`` via
        :func:`client.load_credentials`, so :mod:`first_run` must have run first.
        Touches the private ``_ttl`` / ``REFRESH_THRESHOLD`` attributes purely to
        display the refresh point — production callers should not rely on those.
    """
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
