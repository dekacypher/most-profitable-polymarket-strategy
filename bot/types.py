"""Core data structures for the complete-set maker bot.

Every type used across the bot is defined here — enums for state machines,
dataclasses for domain objects. Frozen where immutability matters.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class OrderState(Enum):
    """Lifecycle of a single leg order."""
    PENDING = auto()     # Order submitted, awaiting confirmation
    LIVE = auto()        # Resting on the book
    FILLED = auto()      # Fully filled
    CANCELLED = auto()   # We cancelled it
    REJECTED = auto()    # CLOB rejected it
    EXPIRED = auto()     # TTL expired without fill


class SetState(Enum):
    """Lifecycle of a complete-set pair (UP + DOWN)."""
    QUOTING = auto()              # Both legs posted, waiting for fills
    ONE_LEG_FILLED = auto()       # One leg filled, aggressively re-quoting the other
    COMPLETE = auto()             # Both legs filled — hold to resolution
    AWAITING_RESOLUTION = auto()  # Market window ended, waiting for on-chain resolution
    ABANDONED = auto()            # Gave up (one-leg timeout or risk breach)
    REDEEMED = auto()             # Resolved at $1.00, profit booked
    REDEMPTION_FAILED = auto()    # Redeem call failed (possible blacklist)


class TokenSide(Enum):
    """Which side of a binary market."""
    UP = "up"
    DOWN = "down"


@dataclass
class TopOfBook:
    """Best bid/ask for a single token."""
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class MarketWindow:
    """A single BTC 15-minute market window with both tokens."""
    condition_id: str               # On-chain CTF condition ID (for redeem)
    question: str
    up_token_id: str
    down_token_id: str
    end_time: str
    end_time_epoch: float = 0.0     # Parsed epoch for deadline comparisons
    slug: Optional[str] = None
    event_id: str = ""              # Gamma API event ID (for resolution checks)

    @property
    def window_id(self) -> str:
        return self.condition_id or self.event_id

    @property
    def is_past_end_time(self) -> bool:
        if self.end_time_epoch <= 0:
            return False
        return time.time() > self.end_time_epoch

    @property
    def seconds_since_end(self) -> float:
        if self.end_time_epoch <= 0:
            return 0.0
        return max(0.0, time.time() - self.end_time_epoch)


@dataclass
class LegOrder:
    """One leg of a complete-set pair."""
    order_id: str
    token_id: str
    side: TokenSide
    price: float
    size: float
    state: OrderState = OrderState.PENDING
    placed_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.placed_at


@dataclass
class CompleteSet:
    """A paired bid on UP + DOWN tokens for the same market window."""
    set_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    window: Optional[MarketWindow] = None
    up_leg: Optional[LegOrder] = None
    down_leg: Optional[LegOrder] = None
    state: SetState = SetState.QUOTING
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    pnl: Optional[float] = None
    redemption_attempts: int = 0
    last_redemption_error: Optional[str] = None

    @property
    def combined_cost(self) -> float:
        up_cost = self.up_leg.price * self.up_leg.size if self.up_leg else 0.0
        down_cost = self.down_leg.price * self.down_leg.size if self.down_leg else 0.0
        return up_cost + down_cost

    @property
    def edge_per_share(self) -> float:
        if not self.up_leg or not self.down_leg:
            return 0.0
        return 1.0 - (self.up_leg.price + self.down_leg.price)

    def filled_leg(self) -> Optional[LegOrder]:
        """Return the filled leg if exactly one is filled."""
        if self.up_leg and self.up_leg.state == OrderState.FILLED:
            if not self.down_leg or self.down_leg.state != OrderState.FILLED:
                return self.up_leg
        if self.down_leg and self.down_leg.state == OrderState.FILLED:
            if not self.up_leg or self.up_leg.state != OrderState.FILLED:
                return self.down_leg
        return None

    def unfilled_leg(self) -> Optional[LegOrder]:
        """Return the unfilled leg if exactly one is filled."""
        if self.up_leg and self.up_leg.state != OrderState.FILLED:
            if self.down_leg and self.down_leg.state == OrderState.FILLED:
                return self.up_leg
        if self.down_leg and self.down_leg.state != OrderState.FILLED:
            if self.up_leg and self.up_leg.state == OrderState.FILLED:
                return self.down_leg
        return None

    def to_dict(self) -> dict:
        """Serialize for JSON persistence."""
        return {
            "set_id": self.set_id,
            "window_id": self.window.window_id if self.window else None,
            "question": self.window.question if self.window else None,
            # Full window fields — required to restore state across restarts
            "condition_id": self.window.condition_id if self.window else "",
            "event_id": self.window.event_id if self.window else "",
            "end_time": self.window.end_time if self.window else "",
            "end_time_epoch": self.window.end_time_epoch if self.window else 0.0,
            "up_token_id": self.window.up_token_id if self.window else "",
            "down_token_id": self.window.down_token_id if self.window else "",
            "slug": self.window.slug if self.window else None,
            "state": self.state.name,
            "up_leg": _leg_to_dict(self.up_leg),
            "down_leg": _leg_to_dict(self.down_leg),
            "combined_cost": round(self.combined_cost, 4),
            "edge_per_share": round(self.edge_per_share, 4),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "pnl": self.pnl,
            "redemption_attempts": self.redemption_attempts,
            "last_redemption_error": self.last_redemption_error,
        }


@dataclass
class QuoteDecision:
    """Output of strategy evaluation — whether and how to quote a window."""
    should_quote: bool
    up_bid_price: float = 0.0
    down_bid_price: float = 0.0
    size: float = 0.0
    edge: float = 0.0
    reason: str = ""


def _leg_to_dict(leg: Optional[LegOrder]) -> Optional[dict]:
    if leg is None:
        return None
    return {
        "order_id": leg.order_id,
        "token_id": leg.token_id,
        "side": leg.side.value,
        "price": leg.price,
        "size": leg.size,
        "state": leg.state.name,
        "placed_at": leg.placed_at,
        "filled_at": leg.filled_at,
    }
