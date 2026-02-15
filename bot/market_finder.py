"""Discover active crypto price-prediction markets via the Gamma API.

Generates candidate slugs for up/down windows and queries the /events endpoint
to find active markets. This approach is necessary because the /markets listing
does not reliably include the fast 15-minute BTC/ETH up/down series.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from bot.config import BotConfig
from bot.types import MarketWindow

logger = logging.getLogger(__name__)

EVENTS_PATH = "/events"


class MarketFinder:
    """Finds active crypto price-prediction markets on Polymarket."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        # Note: market_keywords and price_keywords no longer needed
        # since we generate slugs directly for known market patterns

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._config.gamma_url,
            timeout=10.0,
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def find_active_windows(self) -> list[MarketWindow]:
        """Return all currently active crypto price market windows."""
        if not self._client:
            raise RuntimeError("MarketFinder not started — call start() first")

        try:
            # Generate candidate slugs
            candidates = self._generate_candidate_slugs()
            logger.debug("Checking %d candidate slugs", len(candidates))

            windows = []
            for slug in candidates:
                try:
                    window = await self._fetch_market_by_slug(slug)
                    if window:
                        windows.append(window)
                except httpx.HTTPError as exc:
                    logger.debug("Failed to fetch %s: %s", slug, exc)

            logger.info("Found %d active crypto price windows", len(windows))
            return windows
        except Exception as exc:
            logger.error("Market discovery failed: %s", exc)
            return []

    def _generate_candidate_slugs(self) -> list[str]:
        """Generate candidate slugs for current and upcoming windows."""
        now = datetime.now(timezone.utc)
        candidates = []

        # 15-minute BTC/ETH windows
        for asset in ["btc", "eth"]:
            candidates.extend(self._candidate_15m_slugs(asset, now))

        # 1-hour BTC/ETH windows
        for asset in ["bitcoin", "ethereum"]:
            candidates.extend(self._candidate_1h_slugs(asset, now))

        return candidates

    def _candidate_15m_slugs(self, asset: str, now: datetime) -> list[str]:
        """Generate slugs for 15-minute windows (current ± 30min, future + 15min)."""
        now_sec = int(now.timestamp())
        # From 30 min ago to 15 min ahead, aligned to 15-min boundaries
        from_sec = now_sec - 1800  # 30 minutes ago
        to_sec = now_sec + 900     # 15 minutes ahead

        # Align to 15-minute boundaries (900 seconds)
        start_from = (from_sec // 900) * 900
        start_to = (to_sec // 900) * 900

        slugs = []
        for start in range(start_from, start_to + 1, 900):
            slugs.append(f"{asset}-updown-15m-{start}")

        return slugs

    def _candidate_1h_slugs(self, asset: str, now: datetime) -> list[str]:
        """Generate slugs for 1-hour windows (current hour ± 2 hours)."""
        # Truncate to hour
        hour_start = now.replace(minute=0, second=0, microsecond=0)

        candidates = [
            hour_start - timedelta(hours=2),
            hour_start - timedelta(hours=1),
            hour_start,
            hour_start + timedelta(hours=1),
        ]

        slugs = []
        for dt in candidates:
            slugs.append(self._build_1h_slug(asset, dt))

        return slugs

    def _build_1h_slug(self, asset: str, dt: datetime) -> str:
        """Build slug for 1-hour window like 'bitcoin-up-or-down-february-9-10am-et'."""
        month = dt.strftime("%B").lower()
        day = dt.day
        hour24 = dt.hour
        hour12 = hour24 % 12
        if hour12 == 0:
            hour12 = 12
        ampm = "am" if hour24 < 12 else "pm"

        return f"{asset}-up-or-down-{month}-{day}-{hour12}{ampm}-et"

    async def _fetch_market_by_slug(self, slug: str) -> Optional[MarketWindow]:
        """Fetch a single market by slug and parse it."""
        response = await self._client.get(EVENTS_PATH, params={"slug": slug})
        response.raise_for_status()

        events = response.json()
        if not events or len(events) == 0:
            return None

        event = events[0]

        # Skip closed events
        if event.get("closed", False):
            return None

        # Extract market data
        markets = event.get("markets", [])
        if not markets or len(markets) == 0:
            return None

        market = markets[0]

        # Skip if not accepting orders
        if not market.get("acceptingOrders", False):
            return None

        question = market.get("question", "")

        # Parse token IDs
        clob_token_ids_str = market.get("clobTokenIds", "[]")
        token_ids = self._parse_token_ids(clob_token_ids_str)

        if len(token_ids) < 2:
            logger.debug("Market %s has fewer than 2 tokens: %s", slug, token_ids)
            return None

        # Parse end time
        end_date = event.get("endDate", "")

        return MarketWindow(
            condition_id=event.get("id", ""),
            question=question,
            up_token_id=token_ids[0],
            down_token_id=token_ids[1],
            end_time=end_date,
            end_time_epoch=self._parse_iso_to_epoch(end_date),
            slug=slug,
        )

    def _parse_token_ids(self, raw: str) -> list[str]:
        """Parse clobTokenIds — Gamma returns a JSON string, not a list."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def _parse_iso_to_epoch(self, iso_string: str) -> float:
        """Parse ISO 8601 timestamp to Unix epoch. Returns 0.0 on failure."""
        if not iso_string:
            return 0.0
        try:
            cleaned = iso_string.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            logger.debug("Could not parse end_time: %s", iso_string)
            return 0.0
