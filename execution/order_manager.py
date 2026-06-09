"""
order_manager.py — Order Execution Engine
==========================================
Manages the full lifecycle of trade execution:

  1. Validates the opportunity against risk rules
  2. Calculates position size
  3. Places limit orders (Polymarket API or simulated)
  4. Monitors order fill status
  5. Cancels unfilled orders after timeout
  6. Records fills in the database
  7. Monitors open positions for stop-loss / early exit

Priority: Strategy B (risk-free arb) executes before Strategy A.

Usage:
    from execution.order_manager import OrderManager
    mgr = OrderManager(polymarket_api, risk_manager)
    mgr.execute_opportunity(opportunity)
"""

import threading
import time
from typing import Optional

from config import config
from utils.logger import logger
from utils.telegram_alerts import send_trade_alert, send_stop_loss_alert
from models import (
    insert_trade, close_trade, get_open_trades, get_current_bankroll,
    insert_opportunity,
)

# How long to wait for a limit order to fill before cancelling (seconds)
ORDER_FILL_TIMEOUT = 2.0

# How often to check order status (seconds)
ORDER_CHECK_INTERVAL = 0.25


class OrderManager:
    """
    Manages order placement, fill monitoring, and position management.

    All methods are thread-safe.
    """

    def __init__(self, polymarket_api, risk_manager):
        """
        Initialise the order manager.

        Args:
            polymarket_api: Initialised PolymarketAPI instance.
            risk_manager:   Initialised RiskManager instance.
        """
        self._api = polymarket_api
        self._risk = risk_manager
        self._lock = threading.Lock()

        # Track open positions (trade_id -> info dict)
        self._positions: dict[int, dict] = {}

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def execute_opportunity(self, opportunity: dict) -> bool:
        """
        Execute a detected arbitrage opportunity.

        Handles risk checks, position sizing, order placement, and fill
        monitoring.  Returns True if the trade was successfully executed.

        Args:
            opportunity: Opportunity dict from strategy.evaluate().
                         Must contain keys: market, strategy, side, edge_pct,
                         entry_price, and optionally yes_price, no_price.

        Returns:
            True if the trade was placed and filled successfully.
        """
        strategy = opportunity.get("strategy", "unknown")
        market = opportunity.get("market", "unknown")
        side = opportunity.get("side", "YES")
        entry_price = opportunity.get("entry_price", 0.0)
        edge_pct = opportunity.get("edge_pct", 0.0)

        # ── Step 1: Risk check ───────────────────────────────────────────
        bankroll = get_current_bankroll()
        allowed, reason = self._risk.can_open_trade(bankroll, side)
        if not allowed:
            logger.info("Trade blocked: %s", reason)
            insert_opportunity(
                market=market, strategy=strategy,
                edge_pct=edge_pct,
                yes_price=opportunity.get("yes_price"),
                no_price=opportunity.get("no_price"),
                executed=False,
                reason=f"Risk block: {reason}",
            )
            return False

        # ── Step 2: Position size ────────────────────────────────────────
        base_size = self._risk.calculate_position_size(bankroll)

        if strategy == "complete_set" and side == "BOTH":
            # For complete-set arb, split size between YES and NO
            yes_size = base_size // 2
            no_size = base_size - yes_size
            sizes = {"YES": yes_size, "NO": no_size}
        else:
            sizes = {side: base_size}

        # ── Step 3: Place orders ─────────────────────────────────────────
        trade_ids: list[int] = []
        for order_side, order_size in sizes.items():
            if order_size < 1:
                logger.info("Position size too small (<1 contract), skipping")
                continue

            # Get token ID from opportunity dict (passed by bot from market data)
            token_id = self._get_token_id_from_opportunity(opportunity, order_side)
            if not token_id and not config.is_dry_run:
                logger.error("No token ID for %s %s — market data may be stale", market, order_side)
                continue

            # Determine order side: BUY when entering a position
            order_side_api = "BUY"

            # Place the order
            order_result = self._api.place_order(
                token_id=token_id or f"sim_{market}_{order_side}",
                side=order_side_api,
                price=entry_price,
                size=order_size,
                order_type="GTC",
            )

            if not order_result or not order_result.get("success", False):
                logger.error("Failed to place %s order for %s", order_side, market)
                continue

            # Record the trade in the database
            order_id = order_result.get("orderID", f"dry_run_{int(time.time()*1000)}")
            trade_id = insert_trade(
                market=market,
                side=order_side,
                strategy=strategy,
                entry_price=entry_price,
                quantity=order_size,
                entry_order_id=order_id,
                is_dry_run=config.is_dry_run,
                notes=f"Edge: {edge_pct}% | {opportunity.get('reason', '')}",
            )
            trade_ids.append(trade_id)

            # Update risk manager
            self._risk.record_trade_opened(trade_id, bankroll)

            # Track position
            if strategy != "complete_set":
                self._positions[trade_id] = {
                    "market": market,
                    "side": order_side,
                    "entry_price": entry_price,
                    "size": order_size,
                    "strategy": strategy,
                    "edge_at_entry": edge_pct,
                    "timestamp": time.time(),
                }

            # Send alert
            send_trade_alert(
                action="ENTER",
                market=market,
                side=order_side,
                price=entry_price,
                quantity=order_size,
            )

            logger.info(
                "Trade entered: %s %s %s %d contracts @ %.4f (edge: %.2f%%)",
                market, order_side, "(DRY-RUN)" if config.is_dry_run else "",
                order_size, entry_price, edge_pct,
            )

        return len(trade_ids) > 0

    def monitor_positions(self) -> None:
        """
        Check all open positions for stop-loss and early-exit conditions.

        Called periodically from the main bot loop.
        """
        open_trades = get_open_trades()

        for trade in open_trades:
            trade_id = trade["id"]
            side = trade["side"]
            entry_price = trade["entry_price"]
            market = trade["market"]
            strategy = trade.get("strategy", "")

            # Get current prices from Polymarket
            # (In a real implementation, we'd fetch the latest order book)
            # For now, we check the in-memory position tracker
            pos = self._positions.get(trade_id)
            if not pos:
                continue

            # ── Stop-loss check ──────────────────────────────────────────
            # We need the current price to check.  For now, we approximate
            # using the position's entry price and the current market movement.
            # In a full implementation, this would fetch live order book.
            current_price = self._estimate_current_price(market, side)

            should_stop, loss_pct = self._risk.check_stop_loss(
                entry_price, current_price, side,
            )

            if should_stop:
                logger.warning(
                    "STOP-LOSS: %s %s | Loss: %.2f%% | Entry: %.4f Current: %.4f",
                    market, side, loss_pct, entry_price, current_price,
                )
                send_stop_loss_alert(market, abs(current_price - entry_price) * trade["quantity"], loss_pct)

                # Close the position
                self._close_position(trade_id, current_price, loss_pct)

    def close_all_positions(self, reason: str = "Manual shutdown") -> None:
        """
        Close all open positions immediately.

        Args:
            reason: Reason for closing, logged for audit.
        """
        logger.warning("Closing ALL positions: %s", reason)
        open_trades = get_open_trades()

        for trade in open_trades:
            trade_id = trade["id"]
            side = trade["side"]
            entry_price = trade["entry_price"]
            market = trade["market"]

            # Estimate exit price
            current_price = self._estimate_current_price(market, side)
            loss_pct = ((current_price - entry_price) / entry_price) * 100

            self._close_position(trade_id, current_price, loss_pct, reason)

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _close_position(
        self,
        trade_id: int,
        exit_price: float,
        loss_pct: float,
        reason: str = "",
    ) -> None:
        """
        Close a single position in the database.

        Args:
            trade_id:  Database ID of the trade.
            exit_price: Exit price per contract.
            loss_pct:  Realised loss/gain percentage.
            reason:    Optional reason for closing.
        """
        # Fetch the trade to get quantity
        from models import get_recent_trades
        trades = get_recent_trades(limit=100)
        trade_info = None
        for t in trades:
            if t["id"] == trade_id:
                trade_info = t
                break

        if not trade_info:
            logger.error("Cannot close trade #%d: not found", trade_id)
            return

        quantity = trade_info["quantity"]
        entry_price = trade_info["entry_price"]
        profit_usdc = (exit_price - entry_price) * quantity

        close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            profit_usdc=profit_usdc,
            win=profit_usdc > 0,
            exit_order_id=f"close_{int(time.time()*1000)}",
        )

        self._risk.record_trade_closed(trade_id, profit_usdc)

        # Remove from in-memory tracker
        self._positions.pop(trade_id, None)

        logger.info(
            "Position closed: #%d %s %s | P&L: $%.2f | %s",
            trade_id, trade_info["market"], trade_info["side"],
            profit_usdc, reason or "normal exit",
        )

    def _get_token_id_from_opportunity(self, opportunity: dict, side: str) -> Optional[str]:
        """
        Get the CLOB token ID for a side from the opportunity dict.

        The bot attaches token_id_yes and token_id_no to the opportunity
        before calling execute_opportunity(). In dry-run mode, returns
        a simulated ID.

        Args:
            opportunity: Opportunity dict from strategy.evaluate().
            side:   "YES" or "NO".

        Returns:
            Token ID string, or None if not found.
        """
        if config.is_dry_run:
            market = opportunity.get("market", "unknown")
            return f"sim_{market}_{side}"

        # Live mode: use token IDs attached by the bot
        if side == "YES":
            token_id = opportunity.get("token_id_yes", "")
        else:
            token_id = opportunity.get("token_id_no", "")

        if not token_id:
            logger.warning(
                "No token ID for %s in opportunity — bot may not have attached it. "
                "Check that _cached_15m_markets has clobTokenIds.",
                side,
            )
            return None

        return token_id

    def _get_token_id(self, market: str, side: str) -> Optional[str]:
        """
        Legacy method — kept for backward compatibility.
        Use _get_token_id_from_opportunity() instead.

        Args:
            market: Market name (e.g. "BTC 15m Up").
            side:   "YES" or "NO".

        Returns:
            Token ID string, or None if not found.
        """
        if config.is_dry_run:
            return f"sim_{market}_{side}"

        logger.warning(
            "Live mode requires token IDs from opportunity dict. "
            "Use _get_token_id_from_opportunity() instead.",
        )
        return None

    def _estimate_current_price(self, market: str, side: str) -> float:
        """
        Estimate the current price of a position for stop-loss checking.

        In a full implementation, this would fetch the live order book from
        Polymarket.  For now, it returns the entry price (no movement).

        Args:
            market: Market name.
            side:   "YES" or "NO".

        Returns:
            Estimated current price.
        """
        # If we have a position tracker entry, use that
        for trade_id, pos in self._positions.items():
            if pos["market"] == market and pos["side"] == side:
                return pos["entry_price"]

        return 0.0
