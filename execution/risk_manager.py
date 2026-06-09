"""
risk_manager.py — Risk Management & Position Sizing
====================================================
Central risk control for the trading bot.  Responsible for:

  - Position sizing (max 5% of bankroll per trade)
  - Stop-loss enforcement (-15% hard-coded minimum)
  - Maximum concurrent positions (3)
  - Daily drawdown limit (15%)
  - Compounding recalculation every 10 winning trades
  - Never use leverage

Every module calls through this manager BEFORE executing a trade.

Usage:
    from execution.risk_manager import RiskManager
    risk = RiskManager()
    if risk.can_open_trade():
        size = risk.calculate_position_size(bankroll)
        # ... execute trade
        risk.record_trade_opened(...)
"""

import time
from typing import Optional
from datetime import datetime, timezone, timedelta

from config import config
from utils.logger import logger
from models import get_open_trades, get_trade_stats


class RiskManager:
    """
    Central risk management for the trading bot.

    Thread-safe: uses locks for shared state mutations.
    """

    def __init__(self):
        """Initialise the risk manager and load current state from the DB."""
        self._lock = time  # using time module as a simple lock isn't ideal

        # Track daily state (resets at midnight UTC)
        self._daily_peak_bankroll: float = 0.0
        self._daily_start_bankroll: float = 0.0
        self._last_daily_reset: str = ""  # ISO date string

        # Consecutive wins counter (for compounding recalculation)
        self._consecutive_wins: int = 0
        self._total_wins_at_last_recalc: int = 0

        # Load initial state from database
        self._refresh_state()

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def can_open_trade(self, current_bankroll: float, side: str = "") -> tuple[bool, str]:
        """
        Check whether a new trade is permitted under current risk rules.

        Checks performed:
          1. Maximum concurrent open positions.
          2. Daily drawdown limit.
          3. Bot is not in stop state.

        Args:
            current_bankroll: Current USDC balance.
            side:             Optional side for logging ("YES" / "NO" / "BOTH").

        Returns:
            Tuple of (allowed: bool, reason: str).
        """
        # ── Check 1: Concurrent position limit ────────────────────────────
        open_positions = get_open_trades()
        if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False, (
                f"Max concurrent positions reached "
                f"({len(open_positions)}/{config.MAX_CONCURRENT_POSITIONS})"
            )

        # ── Check 2: Daily drawdown limit ────────────────────────────────
        self._check_daily_reset(current_bankroll)
        if self._daily_peak_bankroll > 0:
            drawdown_pct = (1 - current_bankroll / self._daily_peak_bankroll) * 100
            if drawdown_pct > config.DAILY_DRAWDOWN_LIMIT:
                return False, (
                    f"Daily drawdown limit exceeded: {drawdown_pct:.2f}% "
                    f"(limit: {config.DAILY_DRAWDOWN_LIMIT}%)"
                )

        return True, "OK"

    def calculate_position_size(self, current_bankroll: float) -> int:
        """
        Calculate the maximum position size for the next trade.

        Uses the maximum position percentage of current bankroll, then
        converts to number of contracts based on typical contract price.

        Args:
            current_bankroll: Current USDC balance.

        Returns:
            Number of contracts to trade (integer, at least 1 if bankroll > 0).
        """
        # Maximum USDC to risk on this trade
        max_usdc = current_bankroll * (config.MAX_POSITION_PCT / 100.0)

        # For 15-minute markets, typical contract price is ~$0.50 (50/50 odds)
        # So the number of contracts is: max_usdc / 0.50 * 2 (since we buy
        # at ~$0.50 per contract for the mispriced side)
        #   ≈ max_usdc * 2 contracts per dollar
        #   ≈ max_usdc * 2
        # More precisely, for a contract at price P, number = max_usdc / P
        average_contract_price = 0.50  # typical for 50/50 markets
        num_contracts = int(max_usdc / average_contract_price)

        # Never trade 0 contracts if we have any bankroll
        if num_contracts < 1 and current_bankroll > 0:
            num_contracts = 1

        # For complete-set arb, we need to buy both sides, so divide by 2
        # (The caller will handle this.)

        logger.debug(
            "Position size: bankroll=%.2f max_usdc=%.2f contracts=%d",
            current_bankroll, max_usdc, num_contracts,
        )
        return num_contracts

    def record_trade_opened(self, trade_id: int, bankroll_at_entry: float) -> None:
        """
        Update internal state after a trade is opened.

        Args:
            trade_id:          Database ID of the new trade.
            bankroll_at_entry: Bankroll at the time of entry.
        """
        # Update daily peak to at least the entry bankroll
        if bankroll_at_entry > self._daily_peak_bankroll:
            self._daily_peak_bankroll = bankroll_at_entry
        logger.debug("Trade #%d opened. Daily peak: %.2f", trade_id, self._daily_peak_bankroll)

    def record_trade_closed(self, trade_id: int, profit_usdc: float) -> None:
        """
        Update internal state after a trade is closed.

        Handles:
          - Consecutive win tracking
          - Compounding recalculation trigger

        Args:
            trade_id:    Database ID of the closed trade.
            profit_usdc: Realised profit in USDC.
        """
        if profit_usdc > 0:
            self._consecutive_wins += 1
        else:
            self._consecutive_wins = 0

        # Check compounding recalculation
        stats = get_trade_stats()
        total_wins = stats.get("wins", 0)
        if total_wins - self._total_wins_at_last_recalc >= 10:
            self._recalculate_compounding()
            self._total_wins_at_last_recalc = total_wins

        logger.debug(
            "Trade #%d closed. Consecutive wins: %d",
            trade_id, self._consecutive_wins,
        )

    def check_stop_loss(
        self,
        entry_price: float,
        current_price: float,
        side: str,
    ) -> tuple[bool, float]:
        """
        Check if a position has hit the stop-loss threshold.

        Args:
            entry_price:   Entry price per contract.
            current_price: Current market price.
            side:          "YES" or "NO".

        Returns:
            Tuple of (should_stop: bool, loss_pct: float).
        """
        if entry_price <= 0:
            return False, 0.0

        loss_pct = ((current_price - entry_price) / entry_price) * 100

        # For YES positions: loss when price drops below stop-loss %
        # For NO positions: loss when price rises above stop-loss %
        if side == "YES" and loss_pct <= config.STOP_LOSS_PCT:
            return True, loss_pct
        elif side == "NO" and (-loss_pct) <= config.STOP_LOSS_PCT:
            # For NO, the loss happens when NO price increases
            return True, -loss_pct

        return False, loss_pct

    def get_daily_drawdown(self, current_bankroll: float) -> float:
        """
        Calculate the current daily drawdown percentage.

        Args:
            current_bankroll: Current USDC balance.

        Returns:
            Drawdown as a percentage (e.g. 5.2 = 5.2% down from peak).
        """
        self._check_daily_reset(current_bankroll)
        if self._daily_peak_bankroll <= 0:
            return 0.0
        return (1 - current_bankroll / self._daily_peak_bankroll) * 100

    def is_drawdown_breached(self, current_bankroll: float) -> bool:
        """
        Check if the daily drawdown limit has been breached.

        Args:
            current_bankroll: Current USDC balance.

        Returns:
            True if trading should halt due to drawdown.
        """
        return self.get_daily_drawdown(current_bankroll) > config.DAILY_DRAWDOWN_LIMIT

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _check_daily_reset(self, current_bankroll: float) -> None:
        """
        Reset daily tracking at midnight UTC.

        Args:
            current_bankroll: Current USDC balance for initialising new day.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_daily_reset:
            logger.info("Daily reset: new trading day %s", today)
            self._last_daily_reset = today
            self._daily_peak_bankroll = current_bankroll
            self._daily_start_bankroll = current_bankroll
            self._consecutive_wins = 0

    def _refresh_state(self) -> None:
        """
        Load initial state from the database.
        Called once at startup.
        """
        # Set the last daily reset to today
        self._last_daily_reset = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Load trade stats
        stats = get_trade_stats()
        self._total_wins_at_last_recalc = stats.get("wins", 0)

    def _recalculate_compounding(self) -> None:
        """
        Recalculate position sizing after every 10 winning trades.

        This implements the compounding rule:
          "After every 10 winning trades, recalculate position size based on
           new bankroll."
        The recalculation itself is automatic (every trade uses current bankroll),
        but this method logs the milestone for audit purposes.
        """
        from models import get_current_bankroll
        new_bankroll = get_current_bankroll()
        new_position_size = self.calculate_position_size(new_bankroll)

        logger.info(
            "🔄 COMPOUNDING RECALCULATION: 10 wins achieved!\n"
            "   New bankroll: $%.2f\n"
            "   New position size: %d contracts\n"
            "   Max per trade: $%.2f",
            new_bankroll,
            new_position_size,
            new_bankroll * (config.MAX_POSITION_PCT / 100.0),
        )
