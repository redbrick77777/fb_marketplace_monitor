#!/usr/bin/env python3
"""Convenience entrypoint so cron (or you) can just run `python run.py`."""
import sys

from fb_marketplace_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
