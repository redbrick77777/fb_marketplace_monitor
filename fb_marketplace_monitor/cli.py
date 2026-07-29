"""Command-line entrypoint.

Typical cron usage (every 30 minutes):
    */30 * * * * cd /path/to/app && /usr/bin/python3 run.py --config config.yaml >> logs/cron.log 2>&1

Typical manual test run:
    python run.py --config config.yaml --watch tires_285_70_18_jax --dry-run --verbose
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .config import load_config
from .monitor import run_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FB Marketplace watcher -> Google Sheets")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML file")
    parser.add_argument("--watch", default=None, help="Only run the watch with this name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and filter as usual, but don't write anything to Google Sheets",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    return parser


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(args.verbose, config.log_file)
    logger = logging.getLogger(__name__)

    try:
        total_new = run_all(config, only_watch=args.watch, dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Run failed: %s", exc)
        return 1

    logger.info("Done. %d new listing(s) added.", total_new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
