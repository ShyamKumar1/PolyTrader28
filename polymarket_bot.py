#!/usr/bin/env python3
"""
polymarket_bot.py — PolyTrader28 Main Bot Orchestrator
========================================================
The central entry point for the Polymarket 15-minute BTC & ETH arbitrage bot.

This script:
  1. Initialises all subsystems (config, database, data streams, strategies,
     execution engine).
  2. Launches the Binance WebSocket price stream in a background thread.
  3. Launches the Flask dashboard in a background thread.
  4. Runs the main trading loop:
       a. Fetches active 15-minute markets from Polymarket (every 60s).
       b. Fetches order books for each market (every 500ms).
       c. Evaluates Strategy B (complete-set arb) — highest priority.
       d. Evaluates Strategy A (price-lag arb) for BTC and ETH.
       e. Executes any detected opportunities.
       f. Monitors open positions for stop-loss / early exit.
       g. Logs health status every 60s.
  5. Handles graceful shutdown on SIGINT/SIGTERM.

Usage:
    python polymarket_bot.py                  # Run in dry-run mode (default)
    TRADING_MODE=live python polymarket_bot.py  # Run live (requires .env config)

Takes a --dry-run flag or uses .env TRADING_MODE setting.
"""

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from config import config
from utils.logger import logger
from utils.telegram_alerts import (
    send_alert, send_startup_alert, send_daily_summary, send_drawdown_alert,
)
from models import (
    init_db, insert_equity_snapshot, get_current_bankroll,
    get_trade_stats, get_open_trades, get_recent_trades,
)
from data.binance_stream import BinanceStream
from data.polymarket_api import PolymarketAPI
from strategy.price_lag import PriceLagStrategy
from strategy.complete_set import CompleteSetStrategy
from strategy.grid_trading import GridTradingStrategy, GridConfig
from execution.risk_manager import RiskManager
from execution.order_manager import OrderManager
from execution.binance_grid_executor import BinanceGridExecutor
from dashboard import start_dashboard_thread, set_bot_instance


# ---------------------------------------------------------------------------
# Global variables (used by signal handlers and the dashboard)
# ---------------------------------------------------------------------------
_bot_instance: Optional["PolyTraderBot"] = None
"""Global reference to the running bot instance for signal handling."""


# ---------------------------------------------------------------------------
# Main Bot Class
# ---------------------------------------------------------------------------

class PolyTraderBot:
    """
    PolyTrader28 — Main bot orchestrator.

    Coordinates all subsystems: data feeds, strategy evaluation, order
    execution, risk management, and health monitoring.

    Runs in a main loop that can be stopped gracefully via the stop() method
    or by sending SIGINT (Ctrl+C).
    """

    def __init__(self):
        """Initialise all subsystems. No network activity yet."""
        logger.info("=" * 60)
        logger.info("PolyTrader28 — Initialising...")
        logger.info("Mode: %s", "LIVE" if config.is_live else "DRY RUN")
        logger.info("=" * 60)

        # ── Initialise database ──────────────────────────────────────────
        init_db()
        logger.info("Database initialised at data/polytrader.db")

        # ── Subsystems (initialised but not started) ─────────────────────
        self.binance_stream = BinanceStream()
        self.polymarket_api = PolymarketAPI()
        self.strategy_a = PriceLagStrategy()
        self.strategy_b = CompleteSetStrategy()
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager(self.polymarket_api, self.risk_manager)

        # ── Grid Trading (Binance) ───────────────────────────────────────
        self.grid_enabled = config.GRID_ENABLED
        if self.grid_enabled:
            grid_cfg = GridConfig(
                symbol=config.GRID_SYMBOL,
                grid_range_pct=config.GRID_RANGE_PCT,
                grid_count=config.GRID_COUNT,
                investment_pct=config.GRID_INVESTMENT_PCT,
                rebalance_interval=config.GRID_REBALANCE_INTERVAL,
            )
            self.grid_strategy = GridTradingStrategy(grid_cfg)
            self.grid_executor = BinanceGridExecutor()
            self._grid_initialised = False
            logger.info(
                "Grid trading enabled: %s range=±%.1f%% levels=%d",
                config.GRID_SYMBOL, config.GRID_RANGE_PCT / 2, config.GRID_COUNT,
            )
        else:
            self.grid_strategy = None
            self.grid_executor = None
            self._grid_initialised = False

        # ── State ────────────────────────────────────────────────────────
        self._stop_flag = threading.Event()
        """When set, the main loop will stop and the bot shuts down."""

        self._pause_flag = threading.Event()
        """When set, the bot pauses trading but continues monitoring."""

        self._is_running = False
        """True while the main loop is active."""

        # Health tracking
        self._start_time: float = 0.0
        self._last_health_log: float = 0.0
        self._last_equity_snapshot: float = 0.0
        self._last_market_refresh: float = 0.0
        self._daily_summary_sent: bool = False
        self._last_daily_summary_date: str = ""

        # Live market data cache
        self._cached_15m_markets: list[dict] = []
        self._market_prices: dict[str, dict] = {}
        """Cache of market midpoint prices keyed by market+side."""

        logger.info("All subsystems initialised.")

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the bot: launch threads, then enter the main trading loop.

        This method blocks until the bot is stopped.
        """
        self._start_time = time.time()

        # ── Launch Binance price stream ──────────────────────────────────
        self.binance_stream.start()
        logger.info("Binance price stream starting...")

        # ── Launch Flask dashboard ───────────────────────────────────────
        set_bot_instance(self)
        start_dashboard_thread()

        # ── Send startup alert ────────────────────────────────────────────
        try:
            send_startup_alert()
        except Exception as exc:
            logger.warning("Failed to send startup alert: %s", exc)

        # ── Main loop ────────────────────────────────────────────────────
        self._is_running = True
        logger.info("Bot is now RUNNING. Press Ctrl+C to stop.")

        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
        finally:
            self._is_running = False
            self._shutdown()

    def stop(self) -> None:
        """
        Signal the bot to stop gracefully.

        Sets the stop flag, which the main loop checks on every iteration.
        """
        logger.info("Stop signal received — bot will shut down...")
        self._stop_flag.set()

    def pause(self) -> None:
        """
        Pause trading activity.

        The bot continues monitoring markets but does not enter new trades.
        """
        logger.info("Trading paused")
        self._pause_flag.set()

    def resume(self) -> None:
        """Resume trading activity after a pause."""
        logger.info("Trading resumed")
        self._pause_flag.clear()

    @property
    def is_running(self) -> bool:
        """Check if the main loop is active."""
        return self._is_running and not self._stop_flag.is_set()

    @property
    def is_paused(self) -> bool:
        """Check if trading is paused."""
        return self._pause_flag.is_set()

    # ──────────────────────────────────────────────────────────────────────
    # Dashboard-facing state accessors
    # ──────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """
        Return the current bot status as a dictionary.

        Used by the Flask dashboard API (/api/status).

        Returns:
            Status dict with keys: bankroll_usdc, open_positions, win_rate,
            total_trades, daily_pnl_usdc, is_running, last_update.
        """
        bankroll = get_current_bankroll()
        stats = get_trade_stats()
        open_trades = get_open_trades()

        # Build open positions list
        positions = []
        for t in open_trades:
            # Estimate current price (approximate)
            entry = t["entry_price"]
            current = entry  # In a real build, fetch live price
            pnl_pct = 0.0
            if entry > 0:
                pnl_pct = round(((current - entry) / entry) * 100, 2)
            positions.append({
                "market": t["market"],
                "side": t["side"],
                "entry": entry,
                "current": current,
                "pnl_pct": pnl_pct,
            })

        # Calculate daily P&L
        daily_pnl = self._calculate_daily_pnl()

        # Grid stats
        grid_pnl = self.grid_executor.get_grid_pnl() if self.grid_executor else None
        grid_state = self.grid_strategy.state if self.grid_strategy else None

        return {
            "bankroll_usdc": round(bankroll, 2),
            "open_positions": positions,
            "win_rate": stats.get("win_rate") or 0.0,
            "total_trades": stats.get("total_trades") or 0,
            "daily_pnl_usdc": round(daily_pnl, 2),
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "mode": "live" if config.is_live else "dry_run",
            "uptime_seconds": int(time.time() - self._start_time) if self._start_time else 0,
            "grid": {
                "enabled": self.grid_enabled,
                "active": self._grid_initialised,
                "symbol": config.GRID_SYMBOL,
                "total_profit": round(grid_pnl["total_profit"], 2) if grid_pnl else 0,
                "total_fills": grid_pnl["total_fills"] if grid_pnl else 0,
                "cycle_count": grid_state.cycle_count if grid_state else 0,
                "center_price": round(grid_state.center_price, 2) if grid_state and grid_state.center_price else 0,
                "lower_bound": round(grid_state.lower_bound, 2) if grid_state and grid_state.lower_bound else 0,
                "upper_bound": round(grid_state.upper_bound, 2) if grid_state and grid_state.upper_bound else 0,
            } if grid_pnl else {"enabled": self.grid_enabled, "active": False},
        }

    # ──────────────────────────────────────────────────────────────────────
    # Internal: main loop
    # ──────────────────────────────────────────────────────────────────────

    def _main_loop(self) -> None:
        """
        The core trading loop.

        Iteration speed: controlled by time.sleep(0.5) at the end.
        Each iteration:
          1. Refreshes market list (every 60s).
          2. Fetches order books for all tracked markets.
          3. Evaluates Strategy B (complete-set arb).
          4. Evaluates Strategy A (price-lag arb).
          5. Executes opportunities.
          6. Monitors open positions.
          7. Logs health status (every 60s).
          8. Records equity snapshot (every 5 min).
          9. Checks daily summary (midnight UTC).
        """
        while not self._stop_flag.is_set():
            try:
                now = time.time()

                # ── Step 1: Refresh market list ──────────────────────────
                if now - self._last_market_refresh > 60:
                    self._refresh_markets()
                    self._last_market_refresh = now

                # ── Step 2: Fetch order books ────────────────────────────
                self._fetch_market_prices()

                # ── Step 3: Evaluate Strategy B (priority: high) ─────────
                if not self._pause_flag.is_set():
                    self._evaluate_strategy_b()

                # ── Step 4: Evaluate Strategy A ──────────────────────────
                if not self._pause_flag.is_set():
                    self._evaluate_strategy_a("BTC")
                    self._evaluate_strategy_a("ETH")

                # ── Step 5: Grid Trading (if enabled) ──────────────────────
                if self.grid_enabled and not self._pause_flag.is_set():
                    self._run_grid_trading()

                # ── Step 6: Monitor open positions ────────────────────────
                self.order_manager.monitor_positions()

                # ── Step 6: Health log (every 60s) ───────────────────────
                if now - self._last_health_log > 60:
                    self._log_health_status()
                    self._last_health_log = now

                # ── Step 7: Equity snapshot (every 5 min) ────────────────
                if now - self._last_equity_snapshot > 300:
                    bankroll = get_current_bankroll()
                    insert_equity_snapshot(bankroll)
                    self._last_equity_snapshot = now

                # ── Step 8: Daily summary (midnight UTC) ─────────────────
                self._check_daily_summary()

                # ── Throttle loop ─────────────────────────────────────────
                time.sleep(0.5)

            except Exception as exc:
                logger.error("Unhandled error in main loop: %s", exc, exc_info=True)
                time.sleep(1.0)  # avoid tight error loop

        logger.info("Main loop exited.")

    # ──────────────────────────────────────────────────────────────────────
    # Internal: trading logic
    # ──────────────────────────────────────────────────────────────────────

    def _refresh_markets(self) -> None:
        """
        Fetch the latest active 15-minute BTC and ETH markets from Polymarket.
        """
        logger.debug("Refreshing market list from Polymarket...")
        try:
            markets = self.polymarket_api.get_active_15m_markets()
            self._cached_15m_markets = markets
            if markets:
                logger.info(
                    "Tracking %d active 15-minute markets",
                    len(markets),
                )
            else:
                logger.info(
                    "No 15-minute markets found. Make sure Polymarket "
                    "has active 15-minute BTC/ETH contracts."
                )
        except Exception as exc:
            logger.error("Failed to refresh markets: %s", exc)

    def _find_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """
        Find a market dict from the cached 15-minute markets by condition ID.

        Args:
            condition_id: The market's condition ID.

        Returns:
            Market dict if found, else None.
        """
        for market in self._cached_15m_markets:
            if market.get("conditionId") == condition_id:
                return market
        return None

    def _fetch_market_prices(self) -> None:
        """
        Fetch midpoint prices for all tracked 15-minute markets.
        """
        for market in self._cached_15m_markets:
            try:
                clob_ids = market.get("clobTokenIds", [])
                if len(clob_ids) < 2:
                    continue

                token_id_yes = clob_ids[0]
                token_id_no = clob_ids[1]

                prices = self.polymarket_api.get_midpoint_prices(
                    token_id_yes, token_id_no,
                )

                # Store by condition ID for lookup
                condition_id = market.get("conditionId", "")
                self._market_prices[condition_id] = {
                    "yes_price": prices.get("yes_price", 0.0),
                    "no_price": prices.get("no_price", 0.0),
                    "spread": prices.get("spread", 0.0),
                    "yes_bid": prices.get("yes_bid", 0.0),
                    "yes_ask": prices.get("yes_ask", 0.0),
                    "no_bid": prices.get("no_bid", 0.0),
                    "no_ask": prices.get("no_ask", 0.0),
                    "question": market.get("question", ""),
                    "tick_size": market.get("minimumTickSize", "0.01"),
                    "neg_risk": market.get("negRisk", False),
                }
            except Exception as exc:
                logger.warning("Failed to fetch prices for market: %s", exc)

    def _evaluate_strategy_b(self) -> None:
        """
        Check all tracked markets for complete-set arbitrage opportunities.

        Strategy B has highest priority — risk-free profit.
        """
        for condition_id, prices in self._market_prices.items():
            yes_price = prices.get("yes_price", 0.0)
            no_price = prices.get("no_price", 0.0)
            question = prices.get("question", "Unknown market")

            if yes_price <= 0 or no_price <= 0:
                continue

            # Check for complete-set arb
            opportunity = self.strategy_b.evaluate(
                market_label=question,
                yes_price=yes_price,
                no_price=no_price,
            )

            if opportunity:
                # Attach token IDs from market data for live order placement
                market = self._find_market_by_condition_id(condition_id)
                if market:
                    clob_ids = market.get("clobTokenIds", [])
                    opportunity["token_id_yes"] = clob_ids[0] if len(clob_ids) > 0 else ""
                    opportunity["token_id_no"] = clob_ids[1] if len(clob_ids) > 1 else ""
                opportunity["condition_id"] = condition_id

                logger.info(
                    "🔵 STRATEGY B OPPORTUNITY: %s | Profit: %.2f%%",
                    question, opportunity["profit_pct"],
                )
                self.order_manager.execute_opportunity(opportunity)

    def _evaluate_strategy_a(self, symbol: str) -> None:
        """
        Check for price-lag arbitrage opportunities for a given symbol.

        Args:
            symbol: "BTC" or "ETH".
        """
        # Get current price from Binance stream
        if symbol == "BTC":
            current_price = self.binance_stream.get_btc_price()
        else:
            current_price = self.binance_stream.get_eth_price()

        if current_price <= 0:
            logger.debug("No %s price from Binance yet, skipping", symbol)
            return

        # Get period open price
        period_open = self.binance_stream.get_period_open(symbol)
        if period_open <= 0:
            logger.debug("No %s period open price yet, skipping", symbol)
            return

        # Find the corresponding Polymarket market
        # We look for markets matching this symbol's direction
        for condition_id, prices in self._market_prices.items():
            question = prices.get("question", "").lower()
            yes_price = prices.get("yes_price", 0.0)
            no_price = prices.get("no_price", 0.0)
            tick_size = prices.get("tick_size", "0.01")

            # Check if this market matches the symbol
            if symbol.lower() not in question:
                continue

            if yes_price <= 0 or no_price <= 0:
                continue

            # Evaluate the opportunity
            opportunity = self.strategy_a.evaluate(
                symbol=symbol,
                current_price=current_price,
                period_open=period_open,
                polymarket_yes_price=yes_price,
                polymarket_no_price=no_price,
                tick_size=tick_size,
            )

            if opportunity:
                # Attach token IDs from market data for live order placement
                market = self._find_market_by_condition_id(condition_id)
                if market:
                    clob_ids = market.get("clobTokenIds", [])
                    opportunity["token_id_yes"] = clob_ids[0] if len(clob_ids) > 0 else ""
                    opportunity["token_id_no"] = clob_ids[1] if len(clob_ids) > 1 else ""
                opportunity["condition_id"] = condition_id

                logger.info(
                    "🟢 STRATEGY A OPPORTUNITY: %s %s | Edge: %.2f%%",
                    symbol, opportunity["side"], opportunity["edge_pct"],
                )
                self.order_manager.execute_opportunity(opportunity)

    # ──────────────────────────────────────────────────────────────────────
    # Internal: Grid Trading
    # ──────────────────────────────────────────────────────────────────────

    def _run_grid_trading(self) -> None:
        """
        Execute one iteration of the grid trading strategy.
        
        This is called every 500ms from the main loop. It:
          1. Gets current BTC/ETH price from Binance stream
          2. Initialises the grid on first run
          3. Checks for rebalance
          4. Checks for order fills
          5. Evaluates grid and places new orders
        """
        if self.grid_strategy is None or self.grid_executor is None:
            return
        
        grid_symbol = config.GRID_SYMBOL
        current_price = (
            self.binance_stream.get_btc_price()
            if grid_symbol == "BTC"
            else self.binance_stream.get_eth_price()
        )
        
        if current_price <= 0:
            return  # no price data yet
        
        # Feed price to strategy for volatility tracking
        self.grid_strategy.update_price(current_price)
        
        # ── Initialise grid on first run ──────────────────────────────────
        if not self._grid_initialised:
            bankroll = get_current_bankroll()
            
            # Initialise executor
            if not self.grid_executor.initialise():
                logger.error("Grid executor init failed — disabling grid")
                self.grid_enabled = False
                return
            
            # Initialise grid strategy
            self.grid_strategy.initialise_grid(current_price, bankroll)
            self._grid_initialised = True
            
            # Place initial grid orders
            actions = self.grid_strategy.check_grid(current_price)
            self.grid_executor.place_grid_orders(actions, current_price)
            logger.info(
                "Grid[%s] initialised: center=%.2f levels=%d orders=%d",
                grid_symbol,
                self.grid_strategy.state.center_price,
                len(self.grid_strategy.state.levels),
                len(actions),
            )
            return
        
        # ── Check for rebalance ──────────────────────────────────────────
        if self.grid_strategy.should_rebalance(current_price):
            logger.info("Grid[%s] rebalancing...", grid_symbol)
            self.grid_executor.cancel_all_orders()
            
            bankroll = get_current_bankroll()
            self.grid_strategy.initialise_grid(current_price, bankroll)
            
            actions = self.grid_strategy.check_grid(current_price)
            if actions:
                self.grid_executor.place_grid_orders(actions, current_price)
            return
        
        # ── Check for order fills ────────────────────────────────────────
        fills = self.grid_executor.check_fills()
        
        if fills:
            # Notify strategy of fills
            filled_levels = list(fills.keys())
            actions = self.grid_strategy.check_grid(current_price, filled_levels)
            if actions:
                self.grid_executor.place_grid_orders(actions, current_price)
            
            # Log fill summary
            pnl = self.grid_executor.get_grid_pnl()
            logger.info(
                "Grid[%s] fills=%d | total_profit=$%.2f | cycles=%d",
                grid_symbol, pnl["total_fills"],
                pnl["total_profit"],
                self.grid_strategy.state.cycle_count,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Internal: health & monitoring
    # ──────────────────────────────────────────────────────────────────────

    def _log_health_status(self) -> None:
        """
        Log a comprehensive health snapshot every 60 seconds.
        """
        bankroll = get_current_bankroll()
        stats = get_trade_stats()
        open_positions = get_open_trades()
        uptime = int(time.time() - self._start_time) if self._start_time else 0

        # Build a formatted health string
        # Grid trading stats
        grid_pnl = self.grid_executor.get_grid_pnl() if self.grid_executor else None
        
        health_lines = [
            "━" * 50,
            "HEALTH CHECK",
            f"  Uptime:         {uptime // 3600}h {(uptime % 3600) // 60}m",
            f"  Mode:           {'LIVE' if config.is_live else 'DRY RUN'}",
            f"  Bankroll:       ${bankroll:.2f} USDC",
            f"  Open Positions: {len(open_positions)}/{config.MAX_CONCURRENT_POSITIONS}",
            f"  Total Trades:   {stats.get('total_trades', 0)}",
            f"  Win Rate:       {(stats.get('win_rate') or 0):.1f}%",
            f"  Total P&L:      ${stats.get('total_profit') or 0:.2f}",
            f"  Drawdown:       {self.risk_manager.get_daily_drawdown(bankroll):.2f}%",
            f"  BTC Price:      ${self.binance_stream.get_btc_price():.2f}",
            f"  ETH Price:      ${self.binance_stream.get_eth_price():.2f}",
        ]
        
        # Grid stats
        if grid_pnl:
            grid_cycles = self.grid_strategy.state.cycle_count if self.grid_strategy else 0
            health_lines.extend([
                f"  Grid Active:    {'YES' if self._grid_initialised else 'NO'}",
                f"  Grid Profit:    ${grid_pnl['total_profit']:.2f}",
                f"  Grid Cycles:    {grid_cycles}",
                f"  Grid Fills:     {grid_pnl['total_fills']}",
            ])
        
        health_lines.extend([
            f"  Markets Tracked: {len(self._cached_15m_markets)}",
            f"  Paused:         {'YES' if self._pause_flag.is_set() else 'NO'}",
            "━" * 50,
        ])
        health_msg = "\n".join(health_lines)

        # Log to console/file
        logger.info("\n%s", health_msg)

        # Send to Telegram as a single message
        grid_info = ""
        if grid_pnl and self.grid_strategy:
            grid_info = (
                f"Grid P&L: ${grid_pnl['total_profit']:.2f}\n"
                f"Grid Cycles: {self.grid_strategy.state.cycle_count}\n"
            )
        
        try:
            send_alert(
                f"<b>🤖 Health Check</b>\n"
                f"Bankroll: ${bankroll:.2f}\n"
                f"Mode: {'LIVE' if config.is_live else 'DRY RUN'}\n"
                f"Open: {len(open_positions)}/{config.MAX_CONCURRENT_POSITIONS}\n"
                f"Win Rate: {(stats.get('win_rate') or 0):.1f}%\n"
                f"P&L: ${(stats.get('total_profit') or 0):.2f}\n"
                f"Drawdown: {self.risk_manager.get_daily_drawdown(bankroll):.1f}%\n"
                f"{grid_info}"
                f"Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m",
                disable_notification=True,
            )
        except Exception as exc:
            logger.warning("Failed to send health alert: %s", exc)

    def _check_daily_summary(self) -> None:
        """
        Send a daily summary at midnight UTC.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_daily_summary_date:
            if self._last_daily_summary_date:  # skip the first day
                bankroll = get_current_bankroll()
                stats = get_trade_stats()
                daily_pnl = self._calculate_daily_pnl()

                try:
                    send_daily_summary(
                        total_trades=stats.get("total_trades", 0),
                        wins=stats.get("wins", 0),
                        losses=stats.get("losses", 0),
                        win_rate=stats.get("win_rate", 0),
                        daily_pnl=daily_pnl,
                        bankroll=bankroll,
                    )
                except Exception as exc:
                    logger.warning("Failed to send daily summary: %s", exc)

            self._last_daily_summary_date = today
            self._daily_summary_sent = False

    def _calculate_daily_pnl(self) -> float:
        """
        Calculate today's P&L from trades closed today.

        Returns:
            Net USDC profit/loss for today.
        """
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        try:
            trades = get_recent_trades(limit=500)
            daily_pnl = 0.0
            for t in trades:
                if t["timestamp"] >= today_start and t["profit_usdc"] is not None:
                    daily_pnl += t["profit_usdc"]
            return daily_pnl
        except Exception:
            return 0.0

    # ──────────────────────────────────────────────────────────────────────
    # Internal: shutdown
    # ──────────────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        """
        Perform graceful shutdown of all subsystems.
        """
        logger.info("Shutting down PolyTrader28...")

        # 1. Stop order execution
        logger.info("Closing all open positions...")
        try:
            self.order_manager.close_all_positions(reason="Bot shutdown")
        except Exception as exc:
            logger.error("Error closing positions: %s", exc)

        # 2. Stop grid executor
        if self.grid_executor:
            logger.info("Shutting down grid executor...")
            try:
                self.grid_executor.shutdown()
            except Exception as exc:
                logger.error("Error shutting down grid: %s", exc)

        # 3. Stop Binance stream
        logger.info("Stopping Binance price stream...")
        try:
            self.binance_stream.stop()
        except Exception as exc:
            logger.error("Error stopping Binance stream: %s", exc)

        # 3. Final equity snapshot
        try:
            bankroll = get_current_bankroll()
            insert_equity_snapshot(bankroll)
            logger.info("Final equity snapshot: $%.2f", bankroll)
        except Exception as exc:
            logger.error("Error saving final snapshot: %s", exc)

        # 4. Send shutdown alert
        try:
            send_alert("🛑 <b>PolyTrader28 Stopped</b>")
        except Exception:
            pass

        logger.info("PolyTrader28 shutdown complete.")


# ---------------------------------------------------------------------------
# Signal handler for graceful shutdown
# ---------------------------------------------------------------------------

def _signal_handler(signum, frame) -> None:
    """
    Handle SIGINT (Ctrl+C) and SIGTERM for graceful shutdown.
    """
    signame = signal.Signals(signum).name
    logger.info("Received signal %s — shutting down gracefully...", signame)

    if _bot_instance is not None:
        _bot_instance.stop()

    # If the signal was SIGINT, we also raise KeyboardInterrupt to unblock
    if signum == signal.SIGINT:
        # Give the bot a moment to clean up, then exit
        threading.Timer(5.0, lambda: sys.exit(1)).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Application entry point.

    Parses command-line arguments, initialises the bot, and runs it.
    """
    parser = argparse.ArgumentParser(
        description="PolyTrader28 — Polymarket 15-minute BTC/ETH Arbitrage Bot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Run in simulation mode (overrides .env TRADING_MODE)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=None,
        help="Run in live trading mode (overrides .env TRADING_MODE)",
    )
    args = parser.parse_args()

    # Override trading mode from CLI if provided
    if args.dry_run:
        import config as cfg_module
        cfg_module.config.TRADING_MODE = "dry_run"
        logger.info("CLI flag --dry-run: overriding mode to DRY RUN")
    elif args.live:
        import config as cfg_module
        cfg_module.config.TRADING_MODE = "live"
        logger.info("CLI flag --live: overriding mode to LIVE")

    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Create and start the bot
    global _bot_instance
    _bot_instance = PolyTraderBot()

    logger.info("Starting PolyTrader28...")
    _bot_instance.start()


if __name__ == "__main__":
    main()
