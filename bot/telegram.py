"""Telegram notification service — send trade alerts instead of local logs.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
If not configured, notifications are silently skipped.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Fire-and-forget Telegram message sender."""

    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)
        self._client: Optional[httpx.AsyncClient] = None

        if not self._enabled:
            logger.info("Telegram not configured — notifications disabled")

    async def start(self) -> None:
        if self._enabled:
            self._client = httpx.AsyncClient(timeout=10.0)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, message: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to the configured Telegram chat.

        Returns True on success, False on failure (never raises).
        """
        if not self._enabled or not self._client:
            return False

        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code != 200:
                logger.debug("Telegram send failed: %s", resp.text[:200])
                return False
            return True
        except Exception:
            logger.debug("Telegram send exception", exc_info=True)
            return False

    # ── Convenience methods for common events ──────────────────────

    async def notify_quote(self, question: str, up_bid: float, down_bid: float,
                           edge: float, size: float) -> None:
        msg = (
            f"*NEW QUOTE*\n"
            f"`{question[:60]}`\n"
            f"UP ${up_bid:.2f} + DOWN ${down_bid:.2f} = ${up_bid + down_bid:.4f}\n"
            f"Edge: ${edge:.4f} | Size: {size:.0f} shares"
        )
        await self.send(msg)

    async def notify_fill(self, set_id: str, side: str, price: float,
                          size: float) -> None:
        msg = (
            f"*FILL* `{set_id}`\n"
            f"{side} leg filled @ ${price:.2f} x {size:.0f}"
        )
        await self.send(msg)

    async def notify_complete_set(self, set_id: str, question: str,
                                  combined_cost: float, edge: float) -> None:
        msg = (
            f"*COMPLETE SET* `{set_id}`\n"
            f"`{question[:60]}`\n"
            f"Cost: ${combined_cost:.4f} | Edge: ${edge:.4f}\n"
            f"Holding for resolution..."
        )
        await self.send(msg)

    async def notify_redeemed(self, set_id: str, pnl: float) -> None:
        msg = (
            f"*REDEEMED* `{set_id}`\n"
            f"PnL: *+${pnl:.4f}*"
        )
        await self.send(msg)

    async def notify_abandoned(self, set_id: str, loss: float) -> None:
        msg = (
            f"*ABANDONED* `{set_id}`\n"
            f"Loss: -${abs(loss):.4f}"
        )
        await self.send(msg)

    async def notify_error(self, context: str, error: str) -> None:
        msg = (
            f"*ERROR*\n"
            f"{context}\n"
            f"`{error[:200]}`"
        )
        await self.send(msg)

    async def notify_status(self, open_sets: int, total_pnl: float,
                            redeemed: int, abandoned: int) -> None:
        msg = (
            f"*STATUS*\n"
            f"Open: {open_sets} | PnL: ${total_pnl:.4f}\n"
            f"Redeemed: {redeemed} | Abandoned: {abandoned}"
        )
        await self.send(msg)
