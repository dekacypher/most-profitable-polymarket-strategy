"""CLI entrypoint for the complete-set maker bot.

Usage:
    Paper mode:  python -m bot.main --env .env
    Live mode:   python -m bot.main --live --env .env

Override any config via environment or CLI flags.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from bot.config import load_config
from bot.engine import BotEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Complete-set maker bot for Polymarket BTC 15-min markets",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live trading (default: paper mode)",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=".env",
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="Override minimum edge in cents",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=None,
        help="Override default position size",
    )
    parser.add_argument(
        "--max-sets",
        type=int,
        default=None,
        help="Override max open sets",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )
    return parser


def configure_logging(verbose: bool, log_dir: str) -> None:
    level = logging.DEBUG if verbose else logging.INFO

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path / "bot.log"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # Silence noisy HTTP libraries — only show warnings/errors
    for noisy in ("httpcore", "httpx", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(env_path=args.env, live=args.live)

    # Apply CLI overrides via a new config (frozen dataclass, so recreate)
    overrides = {}
    if args.min_edge is not None:
        overrides["min_edge_cents"] = args.min_edge
    if args.size is not None:
        overrides["default_size"] = args.size
    if args.max_sets is not None:
        overrides["max_open_sets"] = args.max_sets

    if overrides:
        from dataclasses import asdict
        config_dict = asdict(config)
        config_dict.update(overrides)
        from bot.config import BotConfig
        config = BotConfig(**config_dict)

    configure_logging(args.verbose, config.log_dir)
    logger = logging.getLogger(__name__)

    mode = "LIVE" if config.live else "PAPER"
    logger.info("=" * 60)
    logger.info("Complete-Set Maker Bot — %s MODE", mode)
    logger.info("Min edge: %.1f¢  |  Size: %.1f  |  Max sets: %d",
                config.min_edge_cents, config.default_size, config.max_open_sets)
    logger.info("=" * 60)

    if config.live and not config.private_key:
        logger.error("Live mode requires POLYMARKET_PRIVATE_KEY in .env")
        sys.exit(1)

    engine = BotEngine(config)

    # Graceful shutdown on Ctrl+C
    loop = asyncio.new_event_loop()

    def handle_signal() -> None:
        logger.info("Received shutdown signal")
        loop.create_task(engine.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
        loop.run_until_complete(engine.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
