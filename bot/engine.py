"""Bot engine — the main async loop orchestrating all components.

Runs five concurrent tasks:
  1. Market scan:       discover new windows, evaluate, post quotes
  2. Fill monitor:      check order statuses, update position tracker
  3. One-leg manager:   handle partially filled sets (repost or abandon)
  4. Redemption monitor: track resolution, redeem, detect blacklisting
  5. Status reporter:   periodic PnL and risk summary to logs
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.config import BotConfig
from bot.market_finder import MarketFinder
from bot.order_manager import OrderManager
from bot.orderbook import OrderbookMonitor
from bot.position_tracker import PositionTracker
from bot.risk import RiskManager
from bot.strategy import CompleteSetStrategy
from bot.telegram import TelegramNotifier
from bot.types import (
    CompleteSet,
    LegOrder,
    OrderState,
    SetState,
    TokenSide,
)

logger = logging.getLogger(__name__)


class BotEngine:
    """Main orchestrator for the complete-set maker bot."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._market_finder = MarketFinder(config)
        self._orderbook = OrderbookMonitor(config)
        self._strategy = CompleteSetStrategy(config)
        self._order_manager = OrderManager(config)
        self._risk = RiskManager(config)
        self._tracker = PositionTracker(config)
        self._telegram = TelegramNotifier()
        self._quoted_windows: set[str] = set()
        self._running = False
        self._last_scan_log: float = 0
        # Per-condition_id timestamps of last payoutDenominator check.
        # Prevents hammering the RPC every second for unresolved conditions.
        self._redemption_check_times: dict[str, float] = {}

    async def run(self) -> None:
        """Start all components and run the main loops."""
        mode = "LIVE" if self._config.live else "PAPER"
        logger.info("Starting bot engine in %s mode", mode)

        await self._start_components()
        await self._telegram.start()
        self._risk.deactivate_kill_switch()
        self._running = True
        await self._telegram.send(f"*BOT STARTED* ({mode} mode)")

        try:
            await asyncio.gather(
                self._market_scan_loop(),
                self._fill_monitor_loop(),
                self._one_leg_manager_loop(),
                self._redemption_monitor_loop(),
                self._status_report_loop(),
            )
        except asyncio.CancelledError:
            logger.info("Bot engine shutting down")
        finally:
            self._running = False
            await self._telegram.send("*BOT STOPPED*")
            await self._telegram.stop()
            await self._stop_components()
            self._tracker.persist()
            logger.info("Bot engine stopped")

    async def shutdown(self) -> None:
        """Graceful shutdown — cancel all active orders first."""
        logger.info("Initiating graceful shutdown")
        self._running = False
        await self._cancel_all_active_orders()
        self._tracker.persist()

    # ── Main loops ─────────────────────────────────────────────────

    async def _market_scan_loop(self) -> None:
        """Discover windows, evaluate edge, post bids."""
        while self._running:
            try:
                await self._scan_and_quote()
            except Exception:
                logger.exception("Error in market scan loop")
            await asyncio.sleep(self._config.market_scan_interval)

    async def _fill_monitor_loop(self) -> None:
        """Check order fill status for all active sets."""
        while self._running:
            try:
                await self._check_fills()
            except Exception:
                logger.exception("Error in fill monitor loop")
            await asyncio.sleep(self._config.fill_check_interval)

    async def _one_leg_manager_loop(self) -> None:
        """Handle sets where only one leg has filled."""
        while self._running:
            try:
                await self._manage_one_leg_sets()
            except Exception:
                logger.exception("Error in one-leg manager loop")
            await asyncio.sleep(self._config.fill_check_interval)

    async def _redemption_monitor_loop(self) -> None:
        """Monitor COMPLETE/AWAITING_RESOLUTION sets for redemption."""
        while self._running:
            try:
                await self._process_redemptions()
            except Exception:
                logger.exception("Error in redemption monitor loop")
            await asyncio.sleep(self._config.redemption_check_interval)

    async def _status_report_loop(self) -> None:
        """Log periodic status summaries."""
        while self._running:
            await asyncio.sleep(self._config.status_report_interval)
            try:
                await self._log_status()
            except Exception:
                logger.exception("Error in status report")

    # ── Scan and quote ─────────────────────────────────────────────

    async def _scan_and_quote(self) -> None:
        windows = await self._market_finder.find_active_windows()

        skipped_no_book = 0
        skipped_quality = 0
        skipped_edge = 0

        for window in windows:
            if window.window_id in self._quoted_windows:
                continue

            if not self._risk.can_open_new_set(self._tracker.active_sets):
                break

            up_tob = await self._orderbook.get_top_of_book(window.up_token_id)
            down_tob = await self._orderbook.get_top_of_book(window.down_token_id)

            if not up_tob or not down_tob:
                skipped_no_book += 1
                continue

            risk_mult = self._risk.risk_multiplier(self._tracker.active_sets)
            decision = self._strategy.evaluate_window(
                window, up_tob, down_tob, risk_multiplier=risk_mult
            )

            if not decision.should_quote:
                if "Thin books" in decision.reason or "spread" in decision.reason:
                    skipped_quality += 1
                else:
                    skipped_edge += 1
                logger.debug(
                    "Skip %s: %s", window.question[:40], decision.reason
                )
                continue

            await self._post_complete_set(window, decision)

        # Log scan summary at most once per 5 minutes when idle
        now = time.time()
        should_log = (now - self._last_scan_log) >= 300
        if should_log and not self._tracker.active_sets:
            self._last_scan_log = now
            logger.info(
                "Scan: %d crypto windows | "
                "Rejected: %d no-book, %d thin/wide, %d no-edge | "
                "Waiting for tight spreads...",
                len(windows),
                skipped_no_book, skipped_quality, skipped_edge,
            )

    async def _post_complete_set(self, window, decision) -> None:
        """Post bids on both tokens and register the complete set."""
        up_leg = await self._order_manager.place_maker_bid(
            token_id=window.up_token_id,
            side=TokenSide.UP,
            price=decision.up_bid_price,
            size=decision.size,
        )

        down_leg = await self._order_manager.place_maker_bid(
            token_id=window.down_token_id,
            side=TokenSide.DOWN,
            price=decision.down_bid_price,
            size=decision.size,
        )

        if up_leg.state == OrderState.REJECTED or down_leg.state == OrderState.REJECTED:
            logger.warning("Order rejected for window %s", window.window_id[:8])
            if up_leg.state != OrderState.REJECTED:
                await self._order_manager.cancel_order(up_leg.order_id)
            if down_leg.state != OrderState.REJECTED:
                await self._order_manager.cancel_order(down_leg.order_id)
            return

        complete_set = CompleteSet(
            window=window,
            up_leg=up_leg,
            down_leg=down_leg,
            state=SetState.QUOTING,
        )

        self._tracker.add_set(complete_set)
        self._quoted_windows.add(window.window_id)

        logger.info(
            "Quoted %s: UP $%.2f + DOWN $%.2f = $%.4f (edge $%.4f)",
            window.question[:40],
            decision.up_bid_price,
            decision.down_bid_price,
            decision.up_bid_price + decision.down_bid_price,
            decision.edge,
        )
        await self._telegram.notify_quote(
            window.question, decision.up_bid_price, decision.down_bid_price,
            decision.edge, decision.size,
        )

    # ── Fill monitoring ────────────────────────────────────────────

    async def _check_fills(self) -> None:
        for cs in self._tracker.active_sets:
            if cs.state not in (SetState.QUOTING, SetState.ONE_LEG_FILLED):
                continue

            for leg in (cs.up_leg, cs.down_leg):
                if not leg or leg.state != OrderState.LIVE:
                    continue

                new_state = await self._order_manager.check_order_status(leg)
                if new_state != leg.state:
                    self._tracker.update_leg_state(
                        cs.set_id, leg.token_id, new_state
                    )

            # Both legs filled → COMPLETE (but do NOT redeem yet)
            refreshed = self._find_set(cs.set_id)
            if refreshed and refreshed.state == SetState.COMPLETE:
                logger.info(
                    "Both legs filled for set %s — holding for resolution",
                    cs.set_id,
                )
                await self._telegram.notify_complete_set(
                    cs.set_id,
                    cs.window.question if cs.window else "unknown",
                    cs.combined_cost,
                    cs.edge_per_share,
                )

    # ── Redemption monitoring ──────────────────────────────────────

    async def _process_redemptions(self) -> None:
        """Walk through sets that need redemption attention.

        State machine:
          COMPLETE → check if past end_time → AWAITING_RESOLUTION
          AWAITING_RESOLUTION → check if resolved → attempt redeem
            → success: REDEEMED
            → failure: increment attempts, check blacklist threshold
            → deadline exceeded: flag for manual review
        """
        for cs in self._tracker.active_sets:
            if cs.state in (SetState.COMPLETE, SetState.ONE_LEG_FILLED):
                await self._check_transition_to_awaiting(cs)

            elif cs.state == SetState.AWAITING_RESOLUTION:
                logger.debug("Checking redemption for set %s", cs.set_id)
                try:
                    await self._attempt_redemption(cs)
                except Exception:
                    logger.exception("Error attempting redemption for set %s", cs.set_id)

    async def _check_transition_to_awaiting(self, cs: CompleteSet) -> None:
        """Transition COMPLETE → AWAITING_RESOLUTION as soon as window ends (redeem ASAP)."""
        if not cs.window:
            return

        grace = self._config.redemption_grace_seconds

        # Normal path: end_time is known and has passed
        if cs.window.is_past_end_time and cs.window.seconds_since_end >= grace:
            self._tracker.mark_awaiting_resolution(cs.set_id)
            logger.info(
                "Set %s past end_time (%.0fs ago) — attempting redemption as soon as resolved",
                cs.set_id, cs.window.seconds_since_end,
            )
            return

        # Fallback: if end_time_epoch is 0 (parsing failed), force transition
        # after the set has been COMPLETE for > 20 minutes (15-min window + buffer)
        if cs.window.end_time_epoch <= 0 and cs.completed_at:
            age = time.time() - cs.completed_at
            if age > 1200:  # 20 minutes
                self._tracker.mark_awaiting_resolution(cs.set_id)
                logger.warning(
                    "Set %s has no parseable end_time but completed %.0fs ago — "
                    "forcing transition to AWAITING_RESOLUTION",
                    cs.set_id, age,
                )

    async def _attempt_redemption(self, cs: CompleteSet) -> None:
        """Try to redeem a set that's awaiting resolution."""
        if not cs.window:
            return

        # Both resolution check and redemption use the CTF condition_id (bytes32).
        # event_id is a Gamma integer and cannot be used with payoutDenominator.
        condition_id = cs.window.condition_id
        if not condition_id:
            logger.error(
                "Set %s has no CTF condition_id — cannot redeem!", cs.set_id
            )
            return

        # Rate-limit per-set RPC checks: the oracle typically resolves minutes
        # after market close, so hammering payoutDenominator every second wastes
        # RPC quota and spams logs. Check at most once per 30 seconds per set.
        now = time.time()
        _RECHECK_INTERVAL = 30.0
        last_check = self._redemption_check_times.get(condition_id, 0.0)
        if now - last_check < _RECHECK_INTERVAL:
            return
        self._redemption_check_times[condition_id] = now

        # Check deadline — flag if waiting too long
        deadline = self._config.redemption_deadline_seconds
        if cs.window.seconds_since_end > deadline:
            logger.warning(
                "Set %s has waited %.0fs past end_time (deadline: %.0fs)",
                cs.set_id,
                cs.window.seconds_since_end,
                deadline,
            )

        resolved = await self._order_manager.check_market_resolved(condition_id)
        if not resolved:
            return

        logger.info("Attempting redemption for set %s", cs.set_id)
        success, error = await self._order_manager.redeem_complete_set(condition_id)

        if success:
            self._risk.record_redemption_success()
            pnl = self._calculate_redemption_pnl(cs)
            self._tracker.mark_redeemed(cs.set_id)
            self._risk.record_pnl(pnl)
            self._redemption_check_times.pop(condition_id, None)
            logger.info(
                "REDEEMED set %s — PnL $%.4f", cs.set_id, pnl,
            )
            await self._telegram.notify_redeemed(cs.set_id, pnl)
        else:
            # "no tokens redeemed" = already redeemed or wrong collection (non-fatal).
            # Don't count toward kill switch — only real errors (reverts, RPC down) do.
            # Note: error text is "may already be redeemed" (not "already redeemed" directly).
            already_redeemed = (
                "no tokens redeemed" in error.lower()
                or "already redeemed" in error.lower()
                or "already be redeemed" in error.lower()
                or "no positions found" in error.lower()
            )
            if already_redeemed:
                filled = cs.filled_leg()
                if filled:
                    # One-leg hold — token paid out $0 (losing side or already worthless).
                    # Record the actual capital spent as a loss.
                    loss = -(filled.price * filled.size)
                    logger.warning(
                        "Set %s — no USDC returned for one-leg hold (%s). "
                        "Recording loss $%.4f.",
                        cs.set_id, error, abs(loss),
                    )
                    self._tracker.mark_abandoned(cs.set_id, loss)
                else:
                    logger.warning(
                        "Set %s — redeemPositions returned no tokens (%s). "
                        "Marking redeemed to avoid re-attempting.",
                        cs.set_id, error,
                    )
                    self._tracker.mark_redeemed(cs.set_id)
                self._redemption_check_times.pop(condition_id, None)
                return

            self._tracker.mark_redemption_failed(cs.set_id, error)
            self._risk.record_redemption_failure()
            await self._telegram.notify_error(
                f"Redemption failed for set {cs.set_id}", error
            )

            if self._risk.suspected_blacklist:
                logger.critical(
                    "BLACKLIST SUSPECTED — marking set %s as permanently failed",
                    cs.set_id,
                )
                self._tracker.mark_permanently_failed(cs.set_id)
                loss = -(cs.combined_cost)
                self._risk.record_pnl(loss)

    # ── One-leg management ─────────────────────────────────────────

    async def _manage_one_leg_sets(self) -> None:
        """For sets with one leg filled: keep re-quoting, never abandon.

        Instead of abandoning after a timeout, we hold the filled leg
        through resolution. A single filled leg at $0.02 is worth $1.00
        if the market resolves in its favour — abandoning throws that away.
        When the timeout expires we cancel the unfilled leg and transition
        the set straight to AWAITING_RESOLUTION so the redemption monitor
        picks it up.
        """
        for cs in self._tracker.active_sets:
            if cs.state != SetState.ONE_LEG_FILLED:
                continue

            unfilled = cs.unfilled_leg()
            if not unfilled:
                continue

            timeout = self._config.one_leg_timeout_seconds
            elapsed = time.time() - (cs.filled_leg().filled_at or cs.created_at)

            if elapsed > timeout:
                await self._hold_filled_leg(cs)
                continue

            # Re-quote the unfilled leg more aggressively
            if unfilled.state == OrderState.LIVE and unfilled.age_seconds > 10:
                await self._repost_unfilled_leg(cs, unfilled)

    async def _repost_unfilled_leg(
        self, cs: CompleteSet, unfilled: LegOrder
    ) -> None:
        """Cancel and repost the unfilled leg at a more aggressive price."""
        await self._order_manager.cancel_order(unfilled.order_id)

        tob = await self._orderbook.get_top_of_book(unfilled.token_id)
        if not tob:
            return

        tick = self._config.tick_size
        aggressive_price = min(tob.best_bid + 2 * tick, tob.best_ask - tick)
        aggressive_price = round(int(aggressive_price / tick) * tick, 4)

        # Hard ceiling: combined cost must stay below $1.00 - min_edge.
        # Without this cap, chasing a rising unfilled leg turns a profitable
        # complete set into a guaranteed loss (e.g. UP@0.45 + DN@0.58 = $1.03).
        filled = cs.filled_leg()
        if filled:
            min_edge = self._config.min_edge_cents / 100.0
            max_price = round(
                int((1.0 - filled.price - min_edge) / tick) * tick, 4
            )
            if aggressive_price > max_price:
                if max_price <= 0:
                    # No profitable price exists — stop chasing, hold for resolution
                    logger.info(
                        "Set %s: unfilled %s leg too expensive to repost profitably "
                        "(filled=%s@%.2f, market=%.2f) — holding for resolution",
                        cs.set_id, unfilled.side.value,
                        filled.side.value, filled.price, tob.best_bid,
                    )
                    await self._hold_filled_leg(cs)
                    return
                logger.info(
                    "Set %s: capping %s repost at %.2f (market %.2f would make combined >$%.2f)",
                    cs.set_id, unfilled.side.value, max_price, aggressive_price,
                    filled.price + aggressive_price,
                )
                aggressive_price = max_price

        new_leg = await self._order_manager.place_maker_bid(
            token_id=unfilled.token_id,
            side=unfilled.side,
            price=aggressive_price,
            size=unfilled.size,
        )

        # Replace the leg in the set
        if unfilled.side == TokenSide.UP:
            cs.up_leg = new_leg
        else:
            cs.down_leg = new_leg

        logger.info(
            "Reposted %s leg at $%.2f for set %s",
            unfilled.side.value, aggressive_price, cs.set_id,
        )

    async def _hold_filled_leg(self, cs: CompleteSet) -> None:
        """Cancel the unfilled leg and hold the filled leg for redemption.

        The filled token is still in the wallet — if the market resolves
        in its favour, it pays $1.00 per share.  We transition the set to
        AWAITING_RESOLUTION so the redemption loop picks it up.
        """
        unfilled = cs.unfilled_leg()
        if unfilled and unfilled.state == OrderState.LIVE:
            await self._order_manager.cancel_order(unfilled.order_id)

        filled = cs.filled_leg()
        cost = filled.price * filled.size if filled else 0.0

        self._tracker.mark_awaiting_resolution(cs.set_id)
        logger.info(
            "Holding filled %s leg for set %s (cost $%.4f) — awaiting resolution",
            filled.side.value if filled else "?",
            cs.set_id,
            cost,
        )
        await self._telegram.send(
            f"Holding filled leg for set {cs.set_id} — awaiting resolution"
        )

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _calculate_redemption_pnl(cs: CompleteSet) -> float:
        """Calculate PnL for a redeemed set.

        Complete set (both legs): redemption pays $1.00 per share,
        cost is combined price of both legs.
        Single-leg hold: the winning token pays $1.00 per share,
        cost is just the filled leg's price.
        """
        both_filled = (
            cs.up_leg and cs.up_leg.state == OrderState.FILLED
            and cs.down_leg and cs.down_leg.state == OrderState.FILLED
        )
        if both_filled:
            cost_per_share = cs.up_leg.price + cs.down_leg.price
            return (1.0 - cost_per_share) * cs.up_leg.size

        filled = cs.filled_leg()
        if filled:
            return (1.0 - filled.price) * filled.size
        return 0.0

    def _find_set(self, set_id: str) -> CompleteSet | None:
        for cs in self._tracker.active_sets:
            if cs.set_id == set_id:
                return cs
        return None

    async def _cancel_all_active_orders(self) -> None:
        """Cancel every resting order on shutdown."""
        for cs in self._tracker.active_sets:
            for leg in (cs.up_leg, cs.down_leg):
                if leg and leg.state == OrderState.LIVE:
                    await self._order_manager.cancel_order(leg.order_id)

    async def _start_components(self) -> None:
        await self._market_finder.start()
        await self._orderbook.start()
        await self._order_manager.start()

    async def _stop_components(self) -> None:
        await self._order_manager.stop()
        await self._orderbook.stop()
        await self._market_finder.stop()

    async def _log_status(self) -> None:
        risk = self._risk.snapshot(self._tracker.active_sets)
        pnl = self._tracker.pnl_summary()

        logger.info(
            "STATUS | Open: %d | PnL: $%.4f | Exposure: $%.2f | "
            "Risk×: %.2f | Streak: %d | "
            "Redeemed: %d | Abandoned: %d | Failed: %d | "
            "Awaiting: %d | Quoted: %d | Kill: %s",
            risk.open_sets,
            pnl["total_pnl"],
            risk.total_exposure,
            risk.risk_multiplier,
            risk.consecutive_losses,
            pnl["sets_redeemed"],
            pnl["sets_abandoned"],
            pnl["sets_redemption_failed"],
            pnl["sets_awaiting_resolution"],
            len(self._quoted_windows),
            "ON" if risk.kill_switch_active else "off",
        )
        await self._telegram.notify_status(
            risk.open_sets, pnl["total_pnl"],
            pnl["sets_redeemed"], pnl["sets_abandoned"],
        )
