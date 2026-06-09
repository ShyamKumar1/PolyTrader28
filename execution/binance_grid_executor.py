"""
binance_grid_executor.py — Binance Grid Order Execution
========================================================
Executes grid trading orders on Binance spot market.

Handles:
  - Placing limit buy/sell orders
  - Monitoring order fills
  - Cancelling stale orders
  - Tracking positions and P&L per grid level

In dry-run mode, all orders are simulated (logged, not placed).

Usage:
    from execution.binance_grid_executor import BinanceGridExecutor
    executor = BinanceGridExecutor()
    executor.place_grid_orders(actions, current_price)
    fills = executor.check_fills()
"""

import math
import time
import threading
from decimal import Decimal
from typing import Optional

from config import config as app_config
from utils.logger import logger
from models import insert_trade, close_trade, get_current_bankroll


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum order values on Binance spot
MIN_NOTIONAL_USDC = 10.0  # Binance minimum order value
ORDER_FILL_TIMEOUT = 30.0  # seconds to wait for a limit order to fill
ORDER_CHECK_INTERVAL = 1.0  # seconds between fill checks


class BinanceGridExecutor:
    """
    Executes grid trading orders on Binance spot market.
    
    In dry-run mode, all orders are simulated — the executor logs what
    would happen but doesn't place real orders.
    
    In live mode, uses the python-binance library to place and monitor
    actual spot orders.
    """

    def __init__(self):
        """Initialise the executor. Doesn't connect to Binance yet."""
        self._lock = threading.Lock()
        
        # Track open orders per grid level
        # {level_index: {order_id, side, price, size, timestamp}}
        self._open_orders: dict[int, dict] = {}
        
        # Track filled orders for profit calculation
        # {level_index: {buy_price, sell_price, profit, timestamp}}
        self._fill_history: list[dict] = []
        
        # Binance client (lazy initialised)
        self._client = None
        self._client_init_error: Optional[str] = None
        
        # Current positions: side -> amount
        self._positions: dict[str, float] = {"BUY": 0.0, "SELL": 0.0}
        
        # Statistics
        self._total_fills = 0
        self._total_profit = 0.0
        self._total_volume = 0.0

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def initialise(self) -> bool:
        """
        Initialise the Binance client.
        
        In dry-run mode, just returns True.
        In live mode, connects to Binance and checks account.
        
        Returns:
            True if ready to trade (or in dry-run).
        """
        if app_config.is_dry_run:
            logger.info("BinanceGridExecutor: DRY RUN mode — orders are simulated")
            return True
        
        try:
            from binance.client import Client
            from binance.exceptions import BinanceAPIException
            
            self._client = Client(
                app_config.BINANCE_API_KEY,
                app_config.BINANCE_API_SECRET,
            )
            
            # Test connection
            status = self._client.get_system_status()
            if status.get("status") == 0:  # 0 = normal
                logger.info("Binance Grid Executor: connected successfully")
                return True
            else:
                logger.warning("Binance system status: %s", status)
                return True
                
        except Exception as exc:
            self._client_init_error = str(exc)
            logger.error("Binance Grid Executor init failed: %s", exc)
            return False

    def place_grid_orders(
        self,
        actions: list[dict],
        current_price: float,
    ) -> list[dict]:
        """
        Place orders for grid actions.
        
        Args:
            actions: List of action dicts from GridTradingStrategy.check_grid().
            current_price: Current market price for validation.
        
        Returns:
            List of result dicts with keys: level_index, success, order_id.
        """
        results: list[dict] = []
        
        with self._lock:
            for action in actions:
                action_type = action.get("action")
                if hasattr(action_type, "value"):
                    action_str = action_type.value
                else:
                    action_str = str(action_type)
                
                level_idx = action.get("level_index", -1)
                price = action.get("price", 0.0)
                side = action.get("side", "BUY")
                reason = action.get("reason", "")
                
                if level_idx < 0 or price <= 0:
                    # Non-level action (e.g., REBALANCE)
                    if action_str == "CANCEL" and "REBALANCE" in side:
                        logger.info("Grid rebalance needed: %s", reason)
                        results.append({
                            "level_index": level_idx,
                            "success": True,
                            "order_id": "",
                            "action": "REBALANCE",
                        })
                    continue
                
                # Calculate order size
                bankroll = get_current_bankroll()
                grid_investment = bankroll * (app_config.GRID_INVESTMENT_PCT / 100.0)
                total_levels = app_config.GRID_COUNT
                size_per_level_usdc = grid_investment / max(total_levels, 1)
                
                # Convert USDC amount to asset quantity
                quantity = size_per_level_usdc / max(price, 0.01)
                
                # Round down to valid lot size
                if app_config.GRID_SYMBOL == "BTC":
                    quantity = self._round_btc(quantity)
                else:
                    quantity = self._round_eth(quantity)
                
                if quantity <= 0:
                    logger.debug(
                        "Grid[%s] level %d: quantity too small (%.8f), skipping",
                        app_config.GRID_SYMBOL, level_idx, quantity,
                    )
                    continue
                
                # Check minimum notional
                notional = quantity * price
                if notional < MIN_NOTIONAL_USDC and not app_config.is_dry_run:
                    logger.debug(
                        "Grid level %d: notional $%.2f < min $%.2f, skipping",
                        level_idx, notional, MIN_NOTIONAL_USDC,
                    )
                    continue
                
                # Place the order
                if app_config.is_dry_run:
                    # Simulated order
                    order_id = f"grid_sim_{app_config.GRID_SYMBOL}_{level_idx}_{int(time.time()*1000)}"
                    logger.info(
                        "GRID[%s] DRY-RUN: %s %.4f @ %.2f (level %d) | %s",
                        app_config.GRID_SYMBOL, action_str, quantity, price,
                        level_idx, reason,
                    )
                    
                    # Record as a simulated trade
                    insert_trade(
                        market=f"GRID:{app_config.GRID_SYMBOL}/USDT",
                        side=side,
                        strategy=f"grid_{app_config.GRID_SYMBOL}",
                        entry_price=price,
                        quantity=int(quantity * 100000) if app_config.GRID_SYMBOL == "BTC" else int(quantity * 1000),
                        entry_order_id=order_id,
                        is_dry_run=True,
                        notes=f"Grid level {level_idx}: {reason}",
                    )
                    
                    results.append({
                        "level_index": level_idx,
                        "success": True,
                        "order_id": order_id,
                        "action": action_str,
                        "price": price,
                        "quantity": quantity,
                    })
                    
                    # Track the order
                    self._open_orders[level_idx] = {
                        "order_id": order_id,
                        "side": action_str,
                        "price": price,
                        "quantity": quantity,
                        "timestamp": time.time(),
                    }
                    
                else:
                    # Live order on Binance
                    try:
                        symbol = f"{app_config.GRID_SYMBOL}USDT"
                        side_binance = "BUY" if action_str == "BUY" else "SELL"
                        
                        order = self._client.create_order(
                            symbol=symbol,
                            side=side_binance,
                            type="LIMIT",
                            timeInForce="GTC",
                            quantity=quantity,
                            price=self._round_price(price),
                        )
                        
                        order_id = order.get("orderId", f"unknown_{int(time.time()*1000)}")
                        logger.info(
                            "GRID[%s] LIVE: %s %.4f @ %.2f | order=%s",
                            app_config.GRID_SYMBOL, action_str, quantity, price, order_id,
                        )
                        
                        results.append({
                            "level_index": level_idx,
                            "success": True,
                            "order_id": str(order_id),
                            "action": action_str,
                            "price": price,
                            "quantity": quantity,
                        })
                        
                        self._open_orders[level_idx] = {
                            "order_id": str(order_id),
                            "side": action_str,
                            "price": price,
                            "quantity": quantity,
                            "timestamp": time.time(),
                        }
                        
                    except Exception as exc:
                        logger.error("Binance order failed: %s", exc)
                        results.append({
                            "level_index": level_idx,
                            "success": False,
                            "order_id": "",
                            "action": action_str,
                            "error": str(exc),
                        })
        
        return results

    def check_fills(self) -> dict[int, float]:
        """
        Check which open orders have been filled.
        
        In dry-run mode, simulates fills based on price movement
        crossing order prices.
        
        Returns:
            Dict mapping level_index -> fill_price for newly filled orders.
        """
        fills: dict[int, float] = {}
        
        with self._lock:
            if not self._open_orders:
                return fills
            
            if app_config.is_dry_run:
                # In dry-run, we'd check if current price crossed order prices
                # This is handled by the grid strategy's check_grid() method
                # which tells us which levels are filled
                return fills
            
            # Live mode: check Binance for filled orders
            try:
                for level_idx, order_info in list(self._open_orders.items()):
                    order_id = order_info["order_id"]
                    
                    try:
                        symbol = f"{app_config.GRID_SYMBOL}USDT"
                        order_status = self._client.get_order(
                            symbol=symbol,
                            orderId=order_id,
                        )
                        
                        if order_status.get("status") == "FILLED":
                            fill_price = float(order_status.get("price", 0))
                            fills[level_idx] = fill_price
                            
                            # Update position tracking
                            side = order_info["side"]
                            qty = order_info["quantity"]
                            if side == "BUY":
                                self._positions["BUY"] += qty
                            else:
                                self._positions["SELL"] += qty
                            
                            self._total_fills += 1
                            self._total_volume += fill_price * qty
                            
                            # Record fill
                            self._fill_history.append({
                                "level": level_idx,
                                "side": side,
                                "price": fill_price,
                                "quantity": qty,
                                "timestamp": time.time(),
                            })
                            
                            # Remove from open orders
                            del self._open_orders[level_idx]
                            
                            logger.info(
                                "GRID FILL: level %d %s %.4f @ %.2f",
                                level_idx, side, qty, fill_price,
                            )
                            
                        elif order_status.get("status") == "CANCELED":
                            # Order was cancelled externally
                            del self._open_orders[level_idx]
                            
                    except Exception as exc:
                        logger.debug("Error checking order %s: %s", order_id, exc)
                        
            except Exception as exc:
                logger.error("Error checking fills: %s", exc)
        
        return fills

    def cancel_order(self, level_index: int) -> bool:
        """
        Cancel an open order at a grid level.
        
        Args:
            level_index: Grid level index.
        
        Returns:
            True if cancelled successfully (or no order to cancel).
        """
        with self._lock:
            if level_index not in self._open_orders:
                return True
            
            order_info = self._open_orders[level_index]
            order_id = order_info["order_id"]
            
            if app_config.is_dry_run:
                logger.info("DRY-RUN: Cancelled grid order %s (level %d)", order_id, level_index)
                del self._open_orders[level_index]
                return True
            
            try:
                symbol = f"{app_config.GRID_SYMBOL}USDT"
                self._client.cancel_order(symbol=symbol, orderId=order_id)
                logger.info("Cancelled order %s (level %d)", order_id, level_index)
                del self._open_orders[level_index]
                return True
            except Exception as exc:
                logger.error("Failed to cancel order %s: %s", order_id, exc)
                return False

    def cancel_all_orders(self) -> None:
        """Cancel all open grid orders."""
        for level_idx in list(self._open_orders.keys()):
            self.cancel_order(level_idx)
        logger.info("All grid orders cancelled")

    def get_grid_pnl(self) -> dict:
        """
        Get P&L summary for the grid execution.
        
        Returns:
            Dict with total_fills, total_profit, total_volume, positions.
        """
        # Calculate P&L from fill history
        total_profit = 0.0
        buys: list[dict] = []
        sells: list[dict] = []
        
        for fill in self._fill_history:
            if fill["side"] == "BUY":
                buys.append(fill)
            else:
                sells.append(fill)
        
        # Match buys with sells
        for sell in sells:
            if buys:
                buy = buys.pop(0)
                profit = (sell["price"] - buy["price"]) * sell["quantity"]
                total_profit += profit
        
        self._total_profit = total_profit
        
        return {
            "total_fills": self._total_fills,
            "total_profit": round(self._total_profit, 2),
            "total_volume": round(self._total_volume, 2),
            "open_orders": len(self._open_orders),
            "positions": dict(self._positions),
        }

    def shutdown(self) -> None:
        """Clean shutdown — cancel orders."""
        self.cancel_all_orders()
        logger.info("BinanceGridExecutor shutdown")

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _round_btc(quantity: float) -> float:
        """Round BTC quantity to valid lot size (0.00001 BTC)."""
        return math.floor(quantity * 100000) / 100000

    @staticmethod
    def _round_eth(quantity: float) -> float:
        """Round ETH quantity to valid lot size (0.0001 ETH)."""
        return math.floor(quantity * 10000) / 10000

    @staticmethod
    def _round_price(price: float) -> float:
        """Round price to valid tick size ($0.01 for BTC/USDT)."""
        return round(price, 2)



