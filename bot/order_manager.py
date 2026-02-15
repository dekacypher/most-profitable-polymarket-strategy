"""Order manager — place, cancel, and track orders via py-clob-client.

In paper mode: generates fake order IDs, simulates fills with 15% probability.
In live mode: wraps the synchronous py-clob-client in asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Optional

import httpx

from bot.config import BotConfig
from bot.types import LegOrder, OrderState, TokenSide

logger = logging.getLogger(__name__)


class OrderManager:
    """Places and monitors maker bids on Polymarket."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._clob_client: Optional[object] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """Initialize the CLOB client for live trading."""
        if not self._config.live:
            logger.info("Paper mode — no CLOB client needed")
            return

        self._clob_client = await asyncio.to_thread(self._build_clob_client)
        self._http_client = httpx.AsyncClient(
            base_url=self._config.gamma_url,
            timeout=10.0,
        )
        logger.info("CLOB client initialized for live trading")

    async def stop(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
        self._clob_client = None
        self._http_client = None

    async def place_maker_bid(
        self,
        token_id: str,
        side: TokenSide,
        price: float,
        size: float,
    ) -> LegOrder:
        """Post a GTC limit bid. Returns a LegOrder with state PENDING or LIVE."""
        if self._config.live:
            return await self._place_live_order(token_id, side, price, size)
        return self._place_paper_order(token_id, side, price, size)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Returns True if successfully cancelled."""
        if self._config.live:
            return await self._cancel_live_order(order_id)
        logger.info("Paper cancel: %s", order_id)
        return True

    async def check_order_status(self, leg: LegOrder) -> OrderState:
        """Check if an order has been filled. Returns updated state."""
        if self._config.live:
            return await self._check_live_status(leg)
        return self._check_paper_status(leg)

    async def check_market_resolved(self, condition_id: str) -> bool:
        """Check if a market has resolved (on-chain settlement done)."""
        if not self._config.live:
            return True  # Paper mode: instant resolution
        return await self._check_live_resolution(condition_id)

    async def redeem_complete_set(self, condition_id: str) -> tuple[bool, str]:
        """Attempt to redeem a complete set at $1.00.

        Returns (success, error_message). On success error_message is empty.
        """
        if not self._config.live:
            logger.info("Paper redeem: condition %s", condition_id[:8])
            return True, ""
        return await self._redeem_live(condition_id)

    # ── Paper trading ──────────────────────────────────────────────

    def _place_paper_order(
        self,
        token_id: str,
        side: TokenSide,
        price: float,
        size: float,
    ) -> LegOrder:
        order_id = f"paper-{uuid.uuid4().hex[:8]}"
        logger.info(
            "Paper bid: %s %s @ $%.2f x %.1f [%s]",
            side.value, token_id[:8], price, size, order_id,
        )
        return LegOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            state=OrderState.LIVE,
        )

    def _check_paper_status(self, leg: LegOrder) -> OrderState:
        """Simulate fills: 15% chance per check once order is > 2s old."""
        if leg.state != OrderState.LIVE:
            return leg.state
        if leg.age_seconds < 2.0:
            return OrderState.LIVE
        if random.random() < 0.15:
            return OrderState.FILLED
        return OrderState.LIVE

    # ── Live trading ───────────────────────────────────────────────

    async def _place_live_order(
        self,
        token_id: str,
        side: TokenSide,
        price: float,
        size: float,
    ) -> LegOrder:
        """Place a real GTC limit order via py-clob-client."""
        try:
            from py_clob_client.clob_types import OrderArgs

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )

            order = await asyncio.to_thread(
                self._clob_client.create_and_post_order,
                order_args,
            )
            order_id = order.get("orderID", order.get("id", "unknown"))
            logger.info(
                "Live bid: %s %s @ $%.2f x %.1f [%s]",
                side.value, token_id[:8], price, size, order_id,
            )
            return LegOrder(
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                state=OrderState.LIVE,
            )
        except Exception:
            logger.exception("Failed to place live order")
            return LegOrder(
                order_id=f"failed-{uuid.uuid4().hex[:8]}",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                state=OrderState.REJECTED,
            )

    async def _cancel_live_order(self, order_id: str) -> bool:
        try:
            await asyncio.to_thread(self._clob_client.cancel, order_id)
            logger.info("Live cancel: %s", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def _check_live_status(self, leg: LegOrder) -> OrderState:
        try:
            result = await asyncio.to_thread(
                self._clob_client.get_order, leg.order_id
            )
            status = result.get("status", "").upper()
            return _map_clob_status(status)
        except Exception:
            logger.warning("Status check failed for %s", leg.order_id)
            return leg.state

    async def _check_live_resolution(self, condition_id: str) -> bool:
        """Query Gamma API to check if market/event has resolved."""
        try:
            # Use Gamma API directly to check event status
            # The condition_id is actually the event ID
            response = await self._http_client.get(f"/events?id={condition_id}")
            response.raise_for_status()
            events = response.json()

            if not events or len(events) == 0:
                logger.debug("Event %s not found", condition_id[:8])
                return False

            event = events[0]
            # Check if event is closed (resolved)
            is_closed = event.get("closed", False)
            if is_closed:
                logger.info("Event %s (%s) resolved/closed",
                           condition_id[:8], event.get("slug", ""))

            return bool(is_closed)
        except Exception as exc:
            logger.debug("Resolution check failed for %s: %s", condition_id[:8], str(exc))
            return False

    async def _redeem_live(self, condition_id: str) -> tuple[bool, str]:
        """Call the CLOB/CTF redeem endpoint for a resolved market.

        On Polymarket, redeeming a complete set on a resolved binary
        market returns $1.00 per share. If the account is blacklisted
        (e.g., OFAC sanctions), this call will fail.
        """
        try:
            result = await asyncio.to_thread(
                self._clob_client.redeem, condition_id
            )
            success = result.get("success", False)
            if success:
                logger.info("Redeemed condition %s", condition_id[:8])
                return True, ""
            error = result.get("error", "Unknown redemption error")
            logger.warning("Redeem failed for %s: %s", condition_id[:8], error)
            return False, str(error)
        except Exception as exc:
            error_msg = str(exc)
            logger.exception("Redeem exception for %s", condition_id[:8])
            return False, error_msg

    def _build_clob_client(self) -> object:
        """Construct a py-clob-client ClobClient instance."""
        from py_clob_client.client import ClobClient

        host = self._config.clob_url
        key = self._config.private_key
        chain_id = 137  # Polygon mainnet

        client = ClobClient(
            host,
            key=key,
            chain_id=chain_id,
            signature_type=self._config.signature_type,
            funder=self._config.funder_address or None,
        )

        creds = client.derive_api_key()
        client.set_api_creds(
            client.create_or_derive_api_creds()
        )

        return client


def _map_clob_status(status: str) -> OrderState:
    """Map CLOB API status string to our OrderState enum."""
    mapping = {
        "LIVE": OrderState.LIVE,
        "ACTIVE": OrderState.LIVE,
        "MATCHED": OrderState.FILLED,
        "FILLED": OrderState.FILLED,
        "CANCELLED": OrderState.CANCELLED,
        "CANCELED": OrderState.CANCELLED,
        "EXPIRED": OrderState.EXPIRED,
    }
    return mapping.get(status, OrderState.PENDING)
