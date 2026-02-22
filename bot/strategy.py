"""Complete-set strategy — decides whether and how to quote a window.

The core insight: buy 1 share each of UP and DOWN for < $1.00 combined,
hold to resolution, redeem at $1.00. Profit = $1.00 - combined_cost.

This module evaluates each market window and produces a QuoteDecision.
Before pricing, it validates that the orderbook has REAL maker edge:
  - Both sides have active bids AND asks
  - Combined best bids sum to >= min_combined_bids (default $0.80)
  - Bid-ask spread on each side is <= max_spread (default $0.10)
  - Minimum depth at best bid on each side
"""

from __future__ import annotations

import logging

from bot.config import BotConfig
from bot.types import MarketWindow, QuoteDecision, TopOfBook

logger = logging.getLogger(__name__)


class CompleteSetStrategy:
    """Evaluates market windows and produces quote decisions."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config

    def evaluate_window(
        self,
        window: MarketWindow,
        up_tob: TopOfBook,
        down_tob: TopOfBook,
        risk_multiplier: float = 1.0,
    ) -> QuoteDecision:
        """Decide whether to quote this window and at what prices.

        First validates book quality, then calculates edge and sizing.
        """
        rejection = self._check_book_quality(up_tob, down_tob)
        if rejection:
            return QuoteDecision(should_quote=False, reason=rejection)

        up_bid = self._calculate_bid_price(up_tob)
        down_bid = self._calculate_bid_price(down_tob)

        combined = up_bid + down_bid
        edge_cents = (1.0 - combined) * 100.0

        if edge_cents < self._config.min_edge_cents:
            return QuoteDecision(
                should_quote=False,
                reason=f"Edge too thin: {edge_cents:.1f}¢ < {self._config.min_edge_cents}¢",
            )

        if up_bid <= 0 or down_bid <= 0:
            return QuoteDecision(
                should_quote=False,
                reason="Invalid bid price (zero or negative)",
            )

        if up_bid >= 1.0 or down_bid >= 1.0:
            return QuoteDecision(
                should_quote=False,
                reason=f"Bid exceeds $1: up={up_bid}, down={down_bid}",
            )

        base_size = self._calculate_size(edge_cents)
        adjusted_size = round(base_size * risk_multiplier, 1)
        adjusted_size = max(adjusted_size, 1.0)

        # Cap size at max_position_pct of bankroll
        max_exposure = getattr(self._config, "max_total_exposure", 200.0)
        max_position_pct = getattr(self._config, "max_position_pct", 0.10)
        if up_bid > 0 and down_bid > 0:
            max_position_value = max_exposure * max_position_pct
            avg_price = (up_bid + down_bid) / 2
            max_size_from_bankroll = max_position_value / avg_price
            adjusted_size = min(adjusted_size, max_size_from_bankroll)

        return QuoteDecision(
            should_quote=True,
            up_bid_price=up_bid,
            down_bid_price=down_bid,
            size=adjusted_size,
            edge=round(edge_cents / 100.0, 4),
            reason=(
                f"Edge {edge_cents:.1f}¢, combined ${combined:.4f}, "
                f"risk_mult={risk_multiplier:.2f}"
            ),
        )

    def _check_book_quality(
        self, up_tob: TopOfBook, down_tob: TopOfBook
    ) -> str:
        """Validate that both orderbooks have real two-sided activity.

        Returns empty string if healthy, or a rejection reason.
        """
        combined_bids = up_tob.best_bid + down_tob.best_bid
        if combined_bids < self._config.min_combined_bids:
            return (
                f"Thin books: Σbids=${combined_bids:.2f} "
                f"< ${self._config.min_combined_bids:.2f} "
                f"(UP bid=${up_tob.best_bid:.2f}, DOWN bid=${down_tob.best_bid:.2f})"
            )

        if up_tob.spread > self._config.max_spread:
            return (
                f"UP spread too wide: ${up_tob.spread:.2f} "
                f"> ${self._config.max_spread:.2f}"
            )

        if down_tob.spread > self._config.max_spread:
            return (
                f"DOWN spread too wide: ${down_tob.spread:.2f} "
                f"> ${self._config.max_spread:.2f}"
            )

        min_size = self._config.min_bid_size
        if up_tob.bid_size < min_size:
            return f"UP bid depth too thin: {up_tob.bid_size:.0f} < {min_size:.0f}"

        if down_tob.bid_size < min_size:
            return f"DOWN bid depth too thin: {down_tob.bid_size:.0f} < {min_size:.0f}"

        return ""

    def _calculate_bid_price(self, tob: TopOfBook) -> float:
        """Price our bid 1 tick above the current best bid.

        Capped at best_ask - 1 tick to avoid crossing the spread.
        """
        if tob.best_bid <= 0:
            return 0.0

        tick = self._config.tick_size
        improve = self._config.bid_improve_cents / 100.0
        our_bid = tob.best_bid + improve

        max_bid = tob.best_ask - tick
        our_bid = min(our_bid, max_bid)

        return _round_to_tick(our_bid, tick)

    def _calculate_size(self, edge_cents: float) -> float:
        """Scale position size with edge. More edge -> bigger size."""
        min_edge = self._config.min_edge_cents
        if edge_cents <= min_edge:
            return self._config.default_size

        scale = min(edge_cents / (min_edge * 3), 1.0)
        size = self._config.default_size + scale * (
            self._config.max_size - self._config.default_size
        )
        return round(size, 1)


def _round_to_tick(price: float, tick: float) -> float:
    """Round price down to the nearest tick."""
    return round(int(price / tick) * tick, 4)
