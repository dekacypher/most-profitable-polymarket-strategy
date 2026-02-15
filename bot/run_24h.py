"""24-hour paper trading run with real edge validation.

Scans every 30 seconds (not 2) to avoid hammering APIs when idle.
Status reports every 5 minutes. Logs to bot/logs/bot_24h.log.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from bot.config import BotConfig, load_config
from bot.engine import BotEngine

DURATION_HOURS = 24
SCAN_INTERVAL = 30.0     # seconds between market scans
STATUS_INTERVAL = 300.0   # 5-minute status reports


def configure_logging(log_dir: str) -> None:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path / "bot_24h.log"),
    ]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    config = load_config(".env", live=False)

    config_dict = asdict(config)
    config_dict["market_scan_interval"] = SCAN_INTERVAL
    config_dict["status_report_interval"] = STATUS_INTERVAL
    config = BotConfig(**config_dict)

    configure_logging(config.log_dir)
    logger = logging.getLogger("runner")

    logger.info("=" * 60)
    logger.info("24-HOUR PAPER TRADING — REAL EDGE ONLY")
    logger.info("Scan interval: %.0fs | Status interval: %.0fs",
                SCAN_INTERVAL, STATUS_INTERVAL)
    logger.info("Book quality: combined bids >= $%.2f, spread <= $%.2f, depth >= %.0f",
                config.min_combined_bids, config.max_spread, config.min_bid_size)
    logger.info("Keywords: %s", config.market_keywords)
    logger.info("=" * 60)

    engine = BotEngine(config)
    duration = DURATION_HOURS * 3600

    loop = asyncio.new_event_loop()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        loop.create_task(engine.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def run() -> None:
        try:
            await asyncio.wait_for(engine.run(), timeout=duration)
        except asyncio.TimeoutError:
            logger.info("24-hour run complete")
        finally:
            await engine.shutdown()

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
        loop.run_until_complete(engine.shutdown())
    finally:
        loop.close()
        logger.info("Done.")


if __name__ == "__main__":
    main()
