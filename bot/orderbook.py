"""Orderbook monitor — top-of-book data via REST polling with WebSocket upgrade.

Provides TopOfBook snapshots for any token. Uses REST polling as the
reliable primary path; WebSocket upgrade is a future optimization.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from bot.config import BotConfig
from bot.types import TopOfBook

logger = logging.getLogger(__name__)

BOOK_PATH = "/book"


class OrderbookMonitor:
    """Fetches top-of-book data for Polymarket tokens."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: dict[str, TopOfBook] = {}

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._config.clob_url,
            timeout=5.0,
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_top_of_book(self, token_id: str) -> Optional[TopOfBook]:
        """Fetch current best bid/ask for a token via REST."""
        if not self._client:
            raise RuntimeError("OrderbookMonitor not started")

        try:
            response = await self._client.get(
                BOOK_PATH,
                params={"token_id": token_id},
            )
            response.raise_for_status()
            data = response.json()
            tob = _parse_book_response(token_id, data)
            if tob:
                self._cache[token_id] = tob
            return tob
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # 404 = market expired/closed — expected, not a real error
                logger.debug("Book 404 for %s (expired market)", token_id[:8])
            else:
                logger.warning("Book fetch failed for %s: %s", token_id[:8], exc)
            return None
        except httpx.HTTPError as exc:
            logger.warning("Book fetch failed for %s: %s", token_id[:8], exc)
            return self._cache.get(token_id)

    def get_cached(self, token_id: str) -> Optional[TopOfBook]:
        """Return last known top-of-book without a network call."""
        return self._cache.get(token_id)


def _parse_book_response(token_id: str, data: dict) -> Optional[TopOfBook]:
    """Extract best bid/ask from CLOB book response.

    Response format:
      {"market": token_id, "bids": [{"price": "0.45", "size": "100"}, ...],
       "asks": [{"price": "0.47", "size": "50"}, ...]}
    """
    bids = data.get("bids", [])
    asks = data.get("asks", [])

    if not bids or not asks:
        return None

    # Polymarket CLOB returns bids ASCENDING (lowest price first) and
    # asks DESCENDING (highest price first). Best bid = bids[-1] (highest);
    # best ask = asks[-1] (lowest). Using bids[0]/asks[0] gives the worst
    # prices at the bottom of the book, causing all quotes to be far off-market.
    best_bid_entry = bids[-1]
    best_ask_entry = asks[-1]

    best_bid = float(best_bid_entry.get("price", 0))
    bid_size = float(best_bid_entry.get("size", 0))
    best_ask = float(best_ask_entry.get("price", 0))
    ask_size = float(best_ask_entry.get("size", 0))

    if best_bid <= 0 or best_ask <= 0:
        return None

    return TopOfBook(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )
