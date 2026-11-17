"""CLI entrypoint for python3 -m agents_quota_ping."""

import sys

from agents_quota_ping.runner import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
