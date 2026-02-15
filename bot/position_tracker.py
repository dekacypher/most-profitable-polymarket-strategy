"""Position tracker — lifecycle management and JSON persistence.

Tracks every CompleteSet from creation through resolution, persists
trade history to disk, and provides PnL summaries.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from bot.config import BotConfig
from bot.types import CompleteSet, OrderState, SetState

logger = logging.getLogger(__name__)


class PositionTracker:
    """Manages the full lifecycle of complete-set positions."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._active: list[CompleteSet] = []
        self._completed: list[CompleteSet] = []
        self._log_path = Path(config.trade_log_file)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def active_sets(self) -> list[CompleteSet]:
        return list(self._active)

    @property
    def completed_sets(self) -> list[CompleteSet]:
        return list(self._completed)

    def add_set(self, complete_set: CompleteSet) -> None:
        """Register a new complete set being quoted."""
        self._active.append(complete_set)
        logger.info(
            "Tracking new set %s: %s",
            complete_set.set_id,
            complete_set.window.question if complete_set.window else "unknown",
        )

    def update_leg_state(
        self, set_id: str, token_id: str, new_state: OrderState
    ) -> None:
        """Update the state of a specific leg within a set."""
        target = self._find_active(set_id)
        if not target:
            return

        leg = self._find_leg_by_token(target, token_id)
        if not leg:
            return

        old_state = leg.state
        leg.state = new_state

        if new_state == OrderState.FILLED and not leg.filled_at:
            leg.filled_at = time.time()

        logger.info(
            "Set %s leg %s: %s → %s",
            set_id, token_id[:8], old_state.name, new_state.name,
        )

        self._update_set_state(target)

    def mark_abandoned(self, set_id: str, realized_loss: float) -> None:
        """Mark a set as abandoned (one-leg timeout or risk breach)."""
        target = self._find_active(set_id)
        if not target:
            return

        target.state = SetState.ABANDONED
        target.completed_at = time.time()
        target.pnl = realized_loss
        self._finalize(target)

    def mark_awaiting_resolution(self, set_id: str) -> None:
        """Transition COMPLETE → AWAITING_RESOLUTION when window ends."""
        target = self._find_active(set_id)
        if not target:
            return
        if target.state == SetState.COMPLETE:
            target.state = SetState.AWAITING_RESOLUTION
            logger.info("Set %s now awaiting resolution", set_id)

    def mark_redeemed(self, set_id: str) -> None:
        """Mark a complete set as redeemed at $1.00."""
        target = self._find_active(set_id)
        if not target:
            return

        target.state = SetState.REDEEMED
        target.completed_at = time.time()
        target.pnl = round(1.0 - (target.up_leg.price + target.down_leg.price), 4)
        self._finalize(target)

    def mark_redemption_failed(self, set_id: str, error: str) -> None:
        """Record a failed redemption attempt on a set."""
        target = self._find_active(set_id)
        if not target:
            return

        target.redemption_attempts += 1
        target.last_redemption_error = error
        logger.warning(
            "Set %s redemption attempt #%d failed: %s",
            set_id, target.redemption_attempts, error,
        )

    def mark_permanently_failed(self, set_id: str) -> None:
        """Mark a set as permanently unredeemable (suspected blacklist)."""
        target = self._find_active(set_id)
        if not target:
            return

        target.state = SetState.REDEMPTION_FAILED
        target.completed_at = time.time()
        target.pnl = -target.combined_cost
        self._finalize(target)
        logger.critical(
            "Set %s marked REDEMPTION_FAILED — total loss $%.4f",
            set_id, abs(target.pnl or 0.0),
        )

    def pnl_summary(self) -> dict:
        """Aggregate PnL across all completed sets."""
        total = sum(s.pnl for s in self._completed if s.pnl is not None)
        redeemed = sum(1 for s in self._completed if s.state == SetState.REDEEMED)
        abandoned = sum(1 for s in self._completed if s.state == SetState.ABANDONED)
        failed = sum(
            1 for s in self._completed if s.state == SetState.REDEMPTION_FAILED
        )
        awaiting = sum(
            1 for s in self._active
            if s.state in (SetState.COMPLETE, SetState.AWAITING_RESOLUTION)
        )

        return {
            "total_pnl": round(total, 4),
            "sets_redeemed": redeemed,
            "sets_abandoned": abandoned,
            "sets_redemption_failed": failed,
            "sets_awaiting_resolution": awaiting,
            "active_sets": len(self._active),
            "avg_edge": round(
                total / redeemed if redeemed > 0 else 0.0, 4
            ),
        }

    def persist(self) -> None:
        """Write all trade records to JSON."""
        all_sets = self._completed + self._active
        records = [s.to_dict() for s in all_sets]

        self._log_path.write_text(
            json.dumps(records, indent=2, default=str),
            encoding="utf-8",
        )
        logger.debug("Persisted %d records to %s", len(records), self._log_path)

    def _update_set_state(self, target: CompleteSet) -> None:
        """Derive set state from leg states."""
        up_filled = (
            target.up_leg and target.up_leg.state == OrderState.FILLED
        )
        down_filled = (
            target.down_leg and target.down_leg.state == OrderState.FILLED
        )

        if up_filled and down_filled:
            target.state = SetState.COMPLETE
            target.completed_at = time.time()
            logger.info(
                "SET COMPLETE %s — edge $%.4f",
                target.set_id, target.edge_per_share,
            )
        elif up_filled or down_filled:
            if target.state != SetState.ONE_LEG_FILLED:
                target.state = SetState.ONE_LEG_FILLED
                logger.info("One leg filled for set %s", target.set_id)

    def _finalize(self, target: CompleteSet) -> None:
        """Move a set from active to completed."""
        self._active = [s for s in self._active if s.set_id != target.set_id]
        self._completed.append(target)
        self.persist()
        logger.info(
            "Set %s finalized: %s, PnL=$%.4f",
            target.set_id, target.state.name, target.pnl or 0.0,
        )

    def _find_active(self, set_id: str) -> CompleteSet | None:
        for s in self._active:
            if s.set_id == set_id:
                return s
        return None

    @staticmethod
    def _find_leg_by_token(target: CompleteSet, token_id: str):
        if target.up_leg and target.up_leg.token_id == token_id:
            return target.up_leg
        if target.down_leg and target.down_leg.token_id == token_id:
            return target.down_leg
        return None
