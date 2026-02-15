"""Risk management — guards every order placement decision.

Tracks daily PnL, open position count, total exposure, win/loss streaks,
and provides a kill switch. Also detects suspected blacklisting from
consecutive redemption failures.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bot.config import BotConfig
from bot.types import CompleteSet, SetState

logger = logging.getLogger(__name__)


@dataclass
class RiskSnapshot:
    """Point-in-time risk state for logging / status display."""
    open_sets: int
    daily_pnl: float
    total_exposure: float
    kill_switch_active: bool
    can_trade: bool
    risk_multiplier: float
    consecutive_losses: int
    consecutive_redemption_failures: int


class RiskManager:
    """Enforces position limits, streak-based sizing, and blacklist detection."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._daily_pnl: float = 0.0
        self._day_start: float = time.time()
        self._kill_switch: bool = False
        self._consecutive_losses: int = 0
        self._consecutive_redemption_failures: int = 0
        self._total_wins: int = 0
        self._total_losses: int = 0

    # ── Gate checks ────────────────────────────────────────────────

    def can_open_new_set(self, active_sets: list[CompleteSet]) -> bool:
        """Return True if risk limits allow opening another complete set."""
        if self._kill_switch:
            logger.warning("Kill switch active — blocking new set")
            return False

        self._maybe_reset_daily()

        if self._daily_pnl <= -self._config.max_daily_loss:
            logger.warning(
                "Daily loss limit hit: $%.2f (limit $%.2f)",
                self._daily_pnl,
                self._config.max_daily_loss,
            )
            return False

        open_count = _count_open(active_sets)
        if open_count >= self._config.max_open_sets:
            logger.info("Max open sets reached: %d", open_count)
            return False

        exposure = _total_exposure(active_sets)
        if exposure >= self._config.max_total_exposure:
            logger.info("Max exposure reached: $%.2f", exposure)
            return False

        return True

    # ── Risk multiplier for position sizing ────────────────────────

    def risk_multiplier(self, active_sets: list[CompleteSet]) -> float:
        """Calculate a 0.25–1.0 multiplier that scales down after losses.

        Two independent adjustments, combined multiplicatively:
          1. Streak: consecutive losses beyond threshold → scale * 0.5 each
          2. Exposure: linear scale-down as exposure approaches the limit
        """
        streak_mult = self._streak_multiplier()
        exposure_mult = self._exposure_multiplier(active_sets)
        combined = streak_mult * exposure_mult
        return max(combined, self._config.min_risk_multiplier)

    def _streak_multiplier(self) -> float:
        """Scale down after consecutive losses."""
        threshold = self._config.loss_streak_threshold
        if self._consecutive_losses <= threshold:
            return 1.0
        overshoot = self._consecutive_losses - threshold
        scale = self._config.loss_streak_scale ** overshoot
        return max(scale, self._config.min_risk_multiplier)

    def _exposure_multiplier(self, active_sets: list[CompleteSet]) -> float:
        """Linearly scale down as exposure approaches the limit."""
        exposure = _total_exposure(active_sets)
        limit = self._config.max_total_exposure
        if limit <= 0:
            return 1.0
        ratio = exposure / limit
        if ratio < 0.5:
            return 1.0
        # Linear from 1.0 at 50% → 0.25 at 100%
        return max(1.0 - 1.5 * (ratio - 0.5), self._config.min_risk_multiplier)

    # ── PnL tracking ──────────────────────────────────────────────

    def record_pnl(self, amount: float) -> None:
        """Record realized PnL and update streak counters."""
        self._daily_pnl += amount
        logger.info("PnL recorded: $%.4f  (daily: $%.4f)", amount, self._daily_pnl)

        if amount >= 0:
            self._consecutive_losses = 0
            self._total_wins += 1
        else:
            self._consecutive_losses += 1
            self._total_losses += 1
            logger.info(
                "Consecutive losses: %d (threshold: %d)",
                self._consecutive_losses,
                self._config.loss_streak_threshold,
            )

        if self._daily_pnl <= -self._config.max_daily_loss:
            logger.warning("Daily loss limit breached — activating kill switch")
            self._kill_switch = True

    # ── Redemption failure / blacklist detection ───────────────────

    def record_redemption_failure(self) -> None:
        """Track a failed redemption attempt. Triggers kill switch if repeated."""
        self._consecutive_redemption_failures += 1
        logger.warning(
            "Redemption failure #%d (max before kill: %d)",
            self._consecutive_redemption_failures,
            self._config.max_redemption_failures,
        )
        if self._consecutive_redemption_failures >= self._config.max_redemption_failures:
            logger.critical(
                "SUSPECTED BLACKLIST — %d consecutive redemption failures. "
                "Kill switch activated. Check account status manually.",
                self._consecutive_redemption_failures,
            )
            self._kill_switch = True

    def record_redemption_success(self) -> None:
        """Reset the redemption failure counter on success."""
        if self._consecutive_redemption_failures > 0:
            logger.info(
                "Redemption succeeded — clearing %d failure(s)",
                self._consecutive_redemption_failures,
            )
        self._consecutive_redemption_failures = 0

    @property
    def suspected_blacklist(self) -> bool:
        return (
            self._consecutive_redemption_failures
            >= self._config.max_redemption_failures
        )

    # ── Kill switch ────────────────────────────────────────────────

    def activate_kill_switch(self) -> None:
        """Emergency stop — no new orders until manually reset."""
        self._kill_switch = True
        logger.critical("KILL SWITCH ACTIVATED")

    def deactivate_kill_switch(self) -> None:
        self._kill_switch = False
        self._consecutive_redemption_failures = 0
        logger.info("Kill switch deactivated")

    # ── Snapshot ───────────────────────────────────────────────────

    def snapshot(self, active_sets: list[CompleteSet]) -> RiskSnapshot:
        self._maybe_reset_daily()
        return RiskSnapshot(
            open_sets=_count_open(active_sets),
            daily_pnl=round(self._daily_pnl, 4),
            total_exposure=round(_total_exposure(active_sets), 4),
            kill_switch_active=self._kill_switch,
            can_trade=self.can_open_new_set(active_sets),
            risk_multiplier=round(self.risk_multiplier(active_sets), 4),
            consecutive_losses=self._consecutive_losses,
            consecutive_redemption_failures=self._consecutive_redemption_failures,
        )

    # ── Internal ───────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        """Reset daily PnL if a new UTC day has started."""
        elapsed = time.time() - self._day_start
        if elapsed >= 86400:
            logger.info(
                "Daily reset: PnL was $%.4f, resetting", self._daily_pnl
            )
            self._daily_pnl = 0.0
            self._day_start = time.time()
            self._consecutive_losses = 0


def _count_open(active_sets: list[CompleteSet]) -> int:
    return sum(
        1 for s in active_sets
        if s.state in (SetState.QUOTING, SetState.ONE_LEG_FILLED)
    )


def _total_exposure(active_sets: list[CompleteSet]) -> float:
    """Sum of capital at risk across all non-terminal sets."""
    return sum(
        s.combined_cost
        for s in active_sets
        if s.state in (
            SetState.QUOTING,
            SetState.ONE_LEG_FILLED,
            SetState.COMPLETE,
            SetState.AWAITING_RESOLUTION,
        )
    )
