"""Order manager — place, cancel, and track orders via py-clob-client.

In paper mode: generates fake order IDs, simulates fills with 15% probability.
In live mode: wraps the synchronous py-clob-client in asyncio.to_thread().

Redemption uses direct on-chain CTF contract calls (not the CLOB API, which
has no redeem endpoint).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Optional

import httpx
from eth_account import Account
from web3 import Web3

from bot.config import BotConfig
from bot.types import LegOrder, OrderState, TokenSide

logger = logging.getLogger(__name__)

# ── On-chain constants for Polygon mainnet ──────────────────────────
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Native USDC (Polymarket current, post-2024)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"        # Bridged USDC.e (legacy pre-2024 markets)
USDC_COLLATERALS = [USDC_NATIVE, USDC_E]                      # Try native first
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

POLYGON_RPCS = [
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]

CT_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class OrderManager:
    """Places and monitors maker bids on Polymarket."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._clob_client: Optional[object] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        # Web3 / on-chain redemption
        self._w3: Optional[Web3] = None
        self._account = None
        self._ct_contract = None

    async def start(self) -> None:
        """Initialize the CLOB client and Web3 for live trading."""
        if not self._config.live:
            logger.info("Paper mode — no CLOB client needed")
            return

        self._clob_client = await asyncio.to_thread(self._build_clob_client)
        self._http_client = httpx.AsyncClient(
            base_url=self._config.gamma_url,
            timeout=10.0,
        )
        logger.info("CLOB client initialized for live trading")

        # Set up Web3 for on-chain redemption
        self._setup_web3()

    def _setup_web3(self) -> None:
        """Connect to Polygon RPC and prepare CTF contract for redemption."""
        pk = self._config.private_key
        if not pk:
            logger.warning("No private key — on-chain redemption disabled")
            return

        if not pk.startswith("0x"):
            pk = "0x" + pk
        self._account = Account.from_key(pk)

        # Use reliable public RPCs first, then configured RPC as extra fallback
        rpcs = list(POLYGON_RPCS)
        if self._config.polygon_rpc_url:
            rpcs.append(self._config.polygon_rpc_url)

        for rpc in rpcs:
            if not rpc:
                continue
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
                if w3.is_connected():
                    self._w3 = w3
                    self._ct_contract = w3.eth.contract(
                        address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                        abi=CT_ABI,
                    )
                    logger.info(
                        "Web3 connected to %s — wallet %s",
                        rpc[:40], self._account.address,
                    )
                    return
            except Exception:
                continue

        logger.error("Could not connect to any Polygon RPC — redemption disabled!")

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
        """Check on-chain if the CTF oracle has settled this condition.

        Uses payoutDenominator > 0 as the authoritative signal — this is
        the only safe gate before calling redeemPositions.
        """
        return await asyncio.to_thread(self._check_payouts_set, condition_id)

    async def _redeem_live(self, condition_id: str) -> tuple[bool, str]:
        """Redeem winning tokens on-chain via the CTF contract.

        Calls redeemPositions on the Conditional Tokens contract directly.
        Index sets [1, 2] covers both outcomes — the contract only burns
        tokens the wallet actually holds.
        """
        if not self._w3 or not self._account or not self._ct_contract:
            return False, "Web3 not initialized — cannot redeem on-chain"

        try:
            return await asyncio.to_thread(
                self._redeem_onchain_sync, condition_id
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.exception("Redeem exception for %s", condition_id[:8])
            return False, error_msg

    def _get_working_w3(self) -> tuple[Web3, object]:
        """Return a connected Web3 instance + CT contract, with RPC fallback."""
        if self._w3 and self._w3.is_connected():
            return self._w3, self._ct_contract

        logger.warning("Primary RPC disconnected — trying fallbacks")
        rpcs = list(POLYGON_RPCS)
        if self._config.polygon_rpc_url:
            rpcs.append(self._config.polygon_rpc_url)

        for rpc in rpcs:
            if not rpc:
                continue
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
                if w3.is_connected():
                    ct = w3.eth.contract(
                        address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
                        abi=CT_ABI,
                    )
                    self._w3 = w3
                    self._ct_contract = ct
                    logger.info("Switched RPC to %s", rpc[:40])
                    return w3, ct
            except Exception:
                continue

        raise RuntimeError("All Polygon RPCs are down")

    def _verify_receipt_has_payout(self, receipt: dict) -> bool:
        """Check that the TX receipt contains an actual USDC.e transfer.

        CRITICAL: A no-op redeemPositions can confirm with status=1 and
        emit log events (ERC-1155 token burn + Polygon MATIC fee) but
        transfer ZERO USDC.e when payouts haven't been set by the oracle.

        We specifically look for a log from the USDC.e contract address
        which indicates an actual collateral payout occurred.
        """
        logs = receipt.get("logs", [])
        if len(logs) == 0:
            return False

        usdc_addresses = {addr.lower() for addr in USDC_COLLATERALS}
        for log in logs:
            log_addr = getattr(log, "address", "") or log.get("address", "")
            if log_addr.lower() in usdc_addresses:
                return True

        logger.warning(
            "TX confirmed with %d logs but NO USDC transfer — "
            "checked native USDC and USDC.e; wrong collection or no tokens held",
            len(logs),
        )
        return False

    def _check_payouts_set(self, condition_id: str) -> bool:
        """Check on-chain if the UMA oracle has reported payouts for this condition.

        CRITICAL SAFETY CHECK: If payoutDenominator == 0, the market has NOT
        been resolved. Calling redeemPositions in this state burns tokens
        with ZERO payout. We MUST wait until payoutDenominator > 0.
        """
        try:
            w3, ct = self._get_working_w3()
            condition_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            denominator = ct.functions.payoutDenominator(condition_id_bytes).call()
            if denominator == 0:
                logger.info(
                    "Condition %s: payoutDenominator=0 — NOT resolved on-chain yet",
                    condition_id[:12],
                )
                return False
            logger.info(
                "Condition %s: payoutDenominator=%d — resolved on-chain!",
                condition_id[:12], denominator,
            )
            return True
        except Exception as e:
            logger.warning("Failed to check payoutDenominator for %s: %s", condition_id[:12], e)
            return False

    def _redeem_onchain_sync(self, condition_id: str) -> tuple[bool, str]:
        """Synchronous on-chain redemption — tries native USDC then USDC.e (legacy).

        Polymarket migrated from USDC.e to native USDC in 2024.  All current
        markets use native USDC as collateral, so we try that first.  We fall
        back to USDC.e for any positions minted before the migration.
        """
        # CRITICAL: Check if payouts are set BEFORE attempting redemption
        if not self._check_payouts_set(condition_id):
            return False, "Payouts not set on-chain yet — oracle has not resolved"

        for usdc_addr in USDC_COLLATERALS:
            success, err = self._submit_redeem_tx(condition_id, usdc_addr)
            if success:
                return True, ""
            if "no tokens redeemed" in err:
                logger.info(
                    "No positions for %s in %s collection — trying next collateral",
                    condition_id[:12], usdc_addr[:10],
                )
                continue
            # Real error (reverted, RPC down, etc.) — don't try next address
            return False, err

        return False, "No positions found in native USDC or USDC.e collection — may already be redeemed"

    def _submit_redeem_tx(self, condition_id: str, usdc_addr: str) -> tuple[bool, str]:
        """Submit a single redeemPositions TX for the given collateral address."""
        wallet = self._account.address
        condition_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        parent_collection = bytes(32)
        index_sets = [1, 2]

        max_rpc_attempts = 3
        for rpc_attempt in range(max_rpc_attempts):
            try:
                w3, ct = self._get_working_w3()

                nonce = w3.eth.get_transaction_count(wallet, "pending")
                base_gas_price = w3.eth.gas_price
                gas_price = int(base_gas_price * 1.2)

                tx = ct.functions.redeemPositions(
                    Web3.to_checksum_address(usdc_addr),
                    parent_collection,
                    condition_id_bytes,
                    index_sets,
                ).build_transaction({
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 300_000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                })

                signed = self._account.sign_transaction(tx)

                max_retries = 3
                for retry in range(max_retries):
                    try:
                        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                        logger.info(
                            "Redeem TX sent for %s [%s] — hash %s (attempt %d)",
                            condition_id[:12], usdc_addr[:10], tx_hash.hex(), retry + 1,
                        )

                        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

                        if receipt["status"] == 1:
                            if self._verify_receipt_has_payout(receipt):
                                logger.info(
                                    "Redeemed condition %s (collateral %s) — TX confirmed with %d log events",
                                    condition_id[:12], usdc_addr[:10], len(receipt.get("logs", [])),
                                )
                                return True, ""
                            else:
                                return False, "TX confirmed but no tokens redeemed (no-op)"
                        else:
                            return False, "Transaction reverted on-chain"

                    except Exception as send_error:
                        err = str(send_error).lower()
                        if "replacement transaction underpriced" in err:
                            gas_price = int(gas_price * 1.5)
                            tx["gasPrice"] = gas_price
                            signed = self._account.sign_transaction(tx)
                            logger.warning(
                                "Pending TX conflict — bumping gas to %.1f gwei",
                                gas_price / 1e9,
                            )
                            time.sleep(2)
                            continue
                        elif "nonce too low" in err:
                            return False, "Nonce too low — TX may already be processed"
                        elif "already known" in err:
                            logger.info(
                                "TX already in mempool for %s — waiting for receipt",
                                condition_id[:12],
                            )
                            try:
                                receipt = w3.eth.wait_for_transaction_receipt(
                                    w3.keccak(signed.raw_transaction), timeout=90
                                )
                                if receipt["status"] == 1:
                                    if self._verify_receipt_has_payout(receipt):
                                        return True, ""
                                    else:
                                        return False, "TX confirmed but no tokens redeemed (no-op)"
                                else:
                                    return False, "Transaction reverted on-chain"
                            except Exception:
                                return False, "already known — receipt wait timed out"
                        else:
                            raise

                return False, "Max retries exceeded"

            except Exception as rpc_error:
                err_msg = str(rpc_error)
                is_rpc_down = any(p in err_msg.lower() for p in [
                    "503", "502", "server error", "service unavailable",
                    "connection", "timeout", "eof", "reset by peer",
                ])
                if is_rpc_down and rpc_attempt < max_rpc_attempts - 1:
                    logger.warning(
                        "RPC error on attempt %d: %s — switching RPC",
                        rpc_attempt + 1, err_msg[:80],
                    )
                    self._w3 = None  # force reconnect on next _get_working_w3
                    time.sleep(1)
                    continue
                else:
                    raise

        return False, "All RPC attempts failed"

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
    """Map CLOB API status string to our OrderState enum.

    Polymarket CLOB statuses include: LIVE, MATCHED, CLOSED, CANCELLED, EXPIRED.
    CLOSED means the order is done — could be fully matched or expired.
    """
    mapping = {
        "LIVE": OrderState.LIVE,
        "OPEN": OrderState.LIVE,
        "ACTIVE": OrderState.LIVE,
        "MATCHED": OrderState.FILLED,
        "FILLED": OrderState.FILLED,
        "CLOSED": OrderState.FILLED,       # CLOSED = done; check size_matched to confirm
        "CANCELLED": OrderState.CANCELLED,
        "CANCELED": OrderState.CANCELLED,
        "EXPIRED": OrderState.EXPIRED,
    }
    return mapping.get(status, OrderState.PENDING)
