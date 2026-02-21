"""Bot configuration loaded from environment variables.

Single source of truth for all tuneable parameters. Every field has a
sensible default so paper trading works out of the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    """Immutable configuration for one bot session."""

    # --- Polymarket credentials ---
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    signature_type: int = 0
    funder_address: str = ""

    # --- API endpoints ---
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polygon_rpc_url: str = ""  # Polygon RPC for on-chain redemption (fallbacks built-in)

    # --- Strategy parameters ---
    min_edge_cents: float = 2.0        # Minimum edge in cents (combined < $0.98)
    tick_size: float = 0.01            # 1¢ tick
    bid_improve_cents: float = 1.0     # How many cents above best bid
    default_size: float = 5.0          # Shares per leg
    max_size: float = 20.0             # Max shares per leg

    # --- Book quality thresholds ---
    min_combined_bids: float = 0.80    # Both sides' best bids must sum to ≥ this
    max_spread: float = 0.10           # Max bid-ask spread per side ($)
    min_bid_size: float = 10.0         # Min shares at best bid per side

    # --- Risk limits ---
    max_open_sets: int = 10
    max_daily_loss: float = 50.0       # USD
    max_total_exposure: float = 200.0  # USD total capital at risk
    one_leg_timeout_seconds: float = 180.0  # 3 minutes

    # --- Risk adjustment ---
    loss_streak_threshold: int = 3     # Consecutive losses before scaling down
    loss_streak_scale: float = 0.5     # Multiply size by this per streak beyond threshold
    min_risk_multiplier: float = 0.25  # Floor — never scale below 25% of default size

    # --- Redemption (redeem as soon as market ends to lock profit) ---
    redemption_check_interval: float = 1.0   # seconds between resolution checks (frequent = redeem ASAP)
    redemption_grace_seconds: float = 0.0   # Wait after end_time before attempting redeem (0 = try immediately)
    redemption_deadline_seconds: float = 600.0  # Max wait after end_time before flagging
    max_redemption_failures: int = 3   # Consecutive failures → suspected blacklist

    # --- Timing ---
    market_scan_interval: float = 2.0  # seconds
    fill_check_interval: float = 1.0   # seconds
    status_report_interval: float = 30.0  # seconds

    # --- Persistence ---
    log_dir: str = "bot/logs"
    trade_log_file: str = "bot/logs/trades.json"

    # --- Mode ---
    live: bool = False                 # Paper by default

    # --- Market filter ---
    market_keywords: str = "bitcoin,btc,ethereum,eth,solana,sol,crypto"
    price_keywords: str = "price,reach,hit,above,below,dip,up or down,market cap"


def load_config(env_path: str = ".env", live: bool = False) -> BotConfig:
    """Build a BotConfig from environment variables + overrides."""
    load_dotenv(env_path)

    return BotConfig(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        api_key=os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
        clob_url=os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"),
        gamma_url=os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"),
        ws_url=os.getenv(
            "POLYMARKET_WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        polygon_rpc_url=os.getenv("POLYGON_RPC_URL", ""),
        min_edge_cents=float(os.getenv("BOT_MIN_EDGE_CENTS", "2.0")),
        bid_improve_cents=float(os.getenv("BOT_BID_IMPROVE_CENTS", "1.0")),
        default_size=float(os.getenv("BOT_DEFAULT_SIZE", "5.0")),
        max_size=float(os.getenv("BOT_MAX_SIZE", "20.0")),
        min_combined_bids=float(os.getenv("BOT_MIN_COMBINED_BIDS", "0.80")),
        max_spread=float(os.getenv("BOT_MAX_SPREAD", "0.10")),
        min_bid_size=float(os.getenv("BOT_MIN_BID_SIZE", "10.0")),
        max_open_sets=int(os.getenv("BOT_MAX_OPEN_SETS", "10")),
        max_daily_loss=float(os.getenv("BOT_MAX_DAILY_LOSS", "50.0")),
        max_total_exposure=float(os.getenv("BOT_MAX_TOTAL_EXPOSURE", "200.0")),
        one_leg_timeout_seconds=float(os.getenv("BOT_ONE_LEG_TIMEOUT", "180.0")),
        loss_streak_threshold=int(os.getenv("BOT_LOSS_STREAK_THRESHOLD", "3")),
        loss_streak_scale=float(os.getenv("BOT_LOSS_STREAK_SCALE", "0.5")),
        min_risk_multiplier=float(os.getenv("BOT_MIN_RISK_MULTIPLIER", "0.25")),
        redemption_check_interval=float(os.getenv("BOT_REDEMPTION_CHECK_INTERVAL", "1.0")),
        redemption_grace_seconds=float(os.getenv("BOT_REDEMPTION_GRACE", "0.0")),
        redemption_deadline_seconds=float(os.getenv("BOT_REDEMPTION_DEADLINE", "600.0")),
        max_redemption_failures=int(os.getenv("BOT_MAX_REDEMPTION_FAILURES", "3")),
        log_dir=os.getenv("BOT_LOG_DIR", "bot/logs"),
        trade_log_file=os.getenv("BOT_TRADE_LOG", "bot/logs/trades.json"),
        live=live,
        market_keywords=os.getenv(
            "BOT_MARKET_KEYWORDS",
            "bitcoin,btc,ethereum,eth,solana,sol,crypto",
        ),
        price_keywords=os.getenv(
            "BOT_PRICE_KEYWORDS",
            "price,reach,hit,above,below,dip,up or down,market cap",
        ),
    )
