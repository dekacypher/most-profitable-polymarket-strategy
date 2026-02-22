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
from bot.types import CompleteSet, LegOrder, MarketWindow, OrderState, SetState, TokenSide

logger = logging.getLogger(__name__)


class PositionTracker:
    """Manages the full lifecycle of complete-set positions."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._active: list[CompleteSet] = []
        self._completed: list[CompleteSet] = []
        self._log_path = Path(config.trade_log_file)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

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
        """Transition to AWAITING_RESOLUTION when window ends.

        Works for both COMPLETE sets (both legs filled) and
        ONE_LEG_FILLED sets (holding single leg for redemption).
        """
        target = self._find_active(set_id)
        if not target:
            return
        if target.state in (SetState.COMPLETE, SetState.ONE_LEG_FILLED):
            target.state = SetState.AWAITING_RESOLUTION
            if not target.completed_at:
                target.completed_at = time.time()
            logger.info("Set %s now awaiting resolution", set_id)

    def mark_redeemed(self, set_id: str) -> None:
        """Mark a complete set as redeemed at $1.00."""
        target = self._find_active(set_id)
        if not target:
            return

        target.state = SetState.REDEEMED
        target.completed_at = time.time()
        edge_per_share = 1.0 - (target.up_leg.price + target.down_leg.price)
        target.pnl = round(edge_per_share * target.up_leg.size, 4)
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

    def load(self) -> None:
        """Restore active sets from the trade log on startup.

        Rebuilds COMPLETE, ONE_LEG_FILLED, and AWAITING_RESOLUTION sets so the
        redemption monitor picks them up immediately after a restart. Completed
        sets (REDEEMED, ABANDONED, REDEMPTION_FAILED) are loaded into history.
        """
        if not self._log_path.exists():
            return

        try:
            records = json.loads(self._log_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read %s — starting fresh", self._log_path)
            return

        active_states = {
            SetState.COMPLETE,
            SetState.ONE_LEG_FILLED,
            SetState.AWAITING_RESOLUTION,
        }
        restored = 0

        for rec in records:
            try:
                state_name = rec.get("state", "")
                try:
                    state = SetState[state_name]
                except KeyError:
                    continue

                # Reconstruct window — condition_id is the critical field
                condition_id = rec.get("condition_id") or rec.get("window_id", "")
                window = MarketWindow(
                    condition_id=condition_id,
                    question=rec.get("question", ""),
                    up_token_id=rec.get("up_token_id", ""),
                    down_token_id=rec.get("down_token_id", ""),
                    end_time=rec.get("end_time", ""),
                    end_time_epoch=float(rec.get("end_time_epoch", 0.0)),
                    slug=rec.get("slug"),
                    event_id=rec.get("event_id", ""),
                )

                up_leg = _leg_from_dict(rec.get("up_leg"))
                down_leg = _leg_from_dict(rec.get("down_leg"))

                cs = CompleteSet(
                    set_id=rec["set_id"],
                    window=window,
                    up_leg=up_leg,
                    down_leg=down_leg,
                    state=state,
                    created_at=float(rec.get("created_at", time.time())),
                    completed_at=rec.get("completed_at"),
                    pnl=rec.get("pnl"),
                    redemption_attempts=int(rec.get("redemption_attempts", 0)),
                    last_redemption_error=rec.get("last_redemption_error"),
                )

                if state in active_states:
                    self._active.append(cs)
                    restored += 1
                else:
                    self._completed.append(cs)

            except Exception:
                logger.exception("Failed to restore set %s — skipping", rec.get("set_id"))

        if restored:
            logger.info(
                "Restored %d active set(s) from %s — redemption will resume",
                restored, self._log_path,
            )

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


def _leg_from_dict(d: dict | None) -> LegOrder | None:
    """Reconstruct a LegOrder from a persisted dict."""
    if not d:
        return None
    try:
        return LegOrder(
            order_id=d["order_id"],
            token_id=d["token_id"],
            side=TokenSide(d["side"]),
            price=float(d["price"]),
            size=float(d["size"]),
            state=OrderState[d["state"]],
            placed_at=float(d.get("placed_at", time.time())),
            filled_at=d.get("filled_at"),
        )
    except Exception:
        logger.warning("Could not reconstruct leg from %s", d)
        return None
