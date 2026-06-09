#!/usr/bin/env python3
"""
backtest.py — PolyTrader28 Backtesting Module
===============================================
Simulates the arbitrage strategies on historical BTC/ETH price data to
estimate win rates, returns, drawdown, and other performance metrics.

The backtester:
  1. Downloads historical 1-minute OHLCV data from Binance (using python-binance
     or CCXT).
  2. Simulates 15-minute Polymarket contracts using actual historical prices.
  3. Applies both strategies (price-lag and complete-set) with realistic
     assumptions about Polymarket pricing.
  4. Outputs performance summary and generates charts.

Usage:
    python backtest.py --symbol BTC --days 90         # Backtest BTC, 90 days
    python backtest.py --symbol ETH --days 30 --plot  # Show charts
    python backtest.py --all --year 2024              # Full year for both
"""

import argparse
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# Ensure project root is on path
_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import config
from utils.logger import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Logistic function steepness (same as strategy/price_lag.py)
LOGISTIC_K = 500.0

# Default backtest parameters
DEFAULT_DAYS = 90
DEFAULT_INITIAL_CAPITAL = 6.0  # ~₹500 in USDC


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def _load_historical_data_ccxt(
    symbol: str,
    days: int,
) -> list[dict]:
    """
    Download historical 1-minute OHLCV data using CCXT (supports Binance).

    Falls back to simulated data if download fails.

    Args:
        symbol: "BTC" or "ETH".
        days:   Number of days of historical data to fetch.

    Returns:
        List of dicts: {timestamp, open, high, low, close}
    """
    try:
        import ccxt
        exchange = ccxt.binance({"enableRateLimit": True})
        ccxt_symbol = f"{symbol}/USDT"
        since = exchange.parse8601(
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        )
        logger.info(
            "Downloading %s data for %d days from Binance via CCXT...",
            symbol, days,
        )

        all_ohlcv = []
        if since is None:
            logger.error("Failed to parse start time for CCXT download")
            return _generate_simulated_data(symbol, days)
        while since < exchange.milliseconds():
            ohlcv = exchange.fetch_ohlcv(
                ccxt_symbol, timeframe="1m", since=since, limit=1000,
            )
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            logger.debug("  Downloaded %d candles...", len(all_ohlcv))
            time.sleep(0.5)  # rate limit

        if not all_ohlcv:
            raise ValueError("No data returned from CCXT")

        result = []
        for o in all_ohlcv:
            result.append({
                "timestamp": datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc),
                "open": float(o[1]),
                "high": float(o[2]),
                "low": float(o[3]),
                "close": float(o[4]),
            })
        logger.info("Loaded %d candles for %s", len(result), symbol)
        return result

    except Exception as exc:
        logger.warning("CCXT data load failed: %s", exc)
        logger.info("Falling back to simulated price data...")
        return _generate_simulated_data(symbol, days)


def _generate_simulated_data(symbol: str, days: int) -> list[dict]:
    """
    Generate synthetic price data for testing when live data is unavailable.

    Args:
        symbol: "BTC" or "ETH".
        days:   Number of days of 1-minute data.

    Returns:
        List of candles with realistic-ish price movements.
    """
    np.random.seed(42)  # reproducible

    # Starting prices (approximate)
    base_price = 67000.0 if symbol == "BTC" else 3400.0
    n_candles = days * 24 * 60

    # Generate random walk with drift and volatility
    returns = np.random.normal(
        loc=0.00001,    # slight upward drift
        scale=0.0005,   # ~0.05% per minute volatility
        size=n_candles,
    )
    # Add occasional jumps (news events)
    jump_indices = np.random.choice(n_candles, size=int(n_candles * 0.001), replace=False)
    returns[jump_indices] += np.random.normal(0, 0.005, size=len(jump_indices))

    price = base_price
    result = []
    base_time = datetime.now(timezone.utc) - timedelta(days=days)

    for i in range(n_candles):
        price *= (1 + returns[i])
        price = max(price, base_price * 0.5)  # floor at 50% of start
        candle_time = base_time + timedelta(minutes=i)
        result.append({
            "timestamp": candle_time,
            "open": price * (1 + np.random.normal(0, 0.0001)),
            "high": price * (1 + abs(np.random.normal(0, 0.0003))),
            "low": price * (1 - abs(np.random.normal(0, 0.0003))),
            "close": price,
        })

    logger.info("Generated %d simulated candles for %s", len(result), symbol)
    return result


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Simulates the arbitrage strategies on historical data.

    The engine:
      - Divides historical data into 15-minute windows.
      - At each window, computes the "implied probability" based on price
        movement from window open → current price (during the window).
      - Simulates Polymarket pricing as: market price = true probability + noise.
      - Detects arbitrage opportunities when the discrepancy exceeds threshold.
      - Tracks all trades and computes performance metrics.
    """

    def __init__(
        self,
        symbol: str,
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        min_edge: float = 3.0,
        complete_set_threshold: float = 0.985,
        max_position_pct: float = 5.0,
    ):
        """
        Initialise the backtest engine.

        Args:
            symbol:               "BTC" or "ETH".
            initial_capital:      Starting capital in USDC.
            min_edge:             Minimum edge % for Strategy A.
            complete_set_threshold: Max YES+NO sum for Strategy B.
            max_position_pct:     Max % of capital per trade.
        """
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.min_edge = min_edge
        self.complete_set_threshold = complete_set_threshold
        self.max_position_pct = max_position_pct

        # Results
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.capital = initial_capital

    def run(self, candles: list[dict]) -> dict:
        """
        Execute the backtest on historical candle data.

        Args:
            candles: List of 1-minute OHLCV candles.

        Returns:
            Performance summary dict with keys:
                symbol, total_return, win_rate, total_trades, wins, losses,
                max_drawdown, sharpe_ratio, final_capital, start_date, end_date.
        """
        if not candles:
            return {"error": "No data"}

        self.trades = []
        self.equity_curve = []
        self.capital = self.initial_capital

        # Sort candles by timestamp
        candles = sorted(candles, key=lambda c: c["timestamp"])

        # Group into 15-minute windows
        window_duration = timedelta(minutes=15)
        start_time = candles[0]["timestamp"]
        end_time = candles[-1]["timestamp"]

        current_window_start = start_time
        equity_log_timer = start_time

        logger.info(
            "Backtesting %s from %s to %s (%d candles)...",
            self.symbol,
            start_time.strftime("%Y-%m-%d %H:%M"),
            end_time.strftime("%Y-%m-%d %H:%M"),
            len(candles),
        )

        # Track open positions for the window
        open_position = None  # {side, entry_price, edge_at_entry, window_start}

        while current_window_start < end_time:
            window_end = current_window_start + window_duration

            # Get candles in this window
            window_candles = [
                c for c in candles
                if current_window_start <= c["timestamp"] < window_end
            ]
            if len(window_candles) < 2:
                current_window_start = window_end
                continue

            # Window open price = first candle's open
            window_open = window_candles[0]["open"]

            # Iterate through each minute in the window (simulating price updates)
            for i, candle in enumerate(window_candles):
                current_price = candle["close"]
                price_change = (current_price - window_open) / window_open

                # ── Compute implied probability (Strategy A) ──────────────
                if abs(price_change) > 0.0002:
                    try:
                        implied_up_prob = 1.0 / (1.0 + math.exp(-LOGISTIC_K * price_change))
                    except OverflowError:
                        implied_up_prob = 1.0 if price_change > 0 else 0.0
                    implied_up_prob = max(0.01, min(0.99, implied_up_prob))
                else:
                    implied_up_prob = 0.5

                # ── Simulate Polymarket pricing ───────────────────────────
                # Realistic: market price lags the true probability by ~30s-2min
                # and has some noise. We simulate this as:
                #   polymarket_price = true_prob + noise - lag_component
                lag = 0.0
                if i < 3:  # first 3 minutes, Polymarket hasn't fully adjusted
                    lag = 0.02 * (1 - i / 3)

                noise = np.random.normal(0, 0.005)  # 0.5% noise
                polymarket_yes = max(0.01, min(0.99, implied_up_prob + noise - lag))
                polymarket_no = 1.0 - polymarket_yes

                # Add some spread (bid-ask)
                spread = 0.005  # 0.5% spread
                yes_bid = polymarket_yes - spread / 2
                yes_ask = polymarket_yes + spread / 2
                no_bid = polymarket_no - spread / 2
                no_ask = polymarket_no + spread / 2

                # Clamp and ensure positive
                yes_bid = max(0.005, yes_bid)
                yes_ask = max(0.005, yes_ask)
                no_bid = max(0.005, no_bid)
                no_ask = max(0.005, no_ask)

                # ── Strategy B: Complete-Set Arb ─────────────────────────
                yes_mid = (yes_bid + yes_ask) / 2
                no_mid = (no_bid + no_ask) / 2
                sum_price = yes_mid + no_mid

                if sum_price <= self.complete_set_threshold and open_position is None:
                    # Buy both sides
                    profit_per_pair = 1.0 - sum_price
                    position_size = self.capital * (self.max_position_pct / 100.0)
                    pairs = int(position_size / sum_price)

                    if pairs >= 1:
                        entry_cost = pairs * sum_price
                        self.capital -= entry_cost
                        # At settlement, we get $1.00 per pair
                        settlement_value = pairs * 1.0
                        profit = settlement_value - entry_cost
                        self.capital += settlement_value

                        self.trades.append({
                            "timestamp": candle["timestamp"].isoformat(),
                            "market": f"{self.symbol} 15m Complete-Set",
                            "side": "BOTH",
                            "strategy": "complete_set",
                            "entry_price": sum_price,
                            "exit_price": 1.0,
                            "quantity": pairs * 2,
                            "profit_usdc": profit,
                            "win": profit > 0,
                        })

                # ── Strategy A: Price-Lag Arb ────────────────────────────
                if open_position is None:
                    # Default: no opportunity
                    edge = 0.0
                    side = None
                    entry_price = 0.0

                    # Check for entry opportunity
                    if implied_up_prob > polymarket_yes + (self.min_edge / 100):
                        # Market underestimates UP → buy YES
                        edge = (implied_up_prob - polymarket_yes) * 100
                        side = "YES"
                        entry_price = yes_ask  # pay the ask
                    elif polymarket_yes > implied_up_prob + (self.min_edge / 100):
                        # Market overestimates UP → buy NO
                        edge = (polymarket_yes - implied_up_prob) * 100
                        side = "NO"
                        entry_price = no_ask  # pay the ask

                    if side and edge >= self.min_edge:
                        position_size = self.capital * (self.max_position_pct / 100.0)
                        contracts = int(position_size / entry_price)

                        if contracts >= 1:
                            cost = contracts * entry_price
                            self.capital -= cost
                            open_position = {
                                "side": side,
                                "entry_price": entry_price,
                                "contracts": contracts,
                                "edge_at_entry": edge,
                                "window_start": current_window_start,
                                "entry_time": candle["timestamp"],
                            }

                # ── Check for exit of open position ──────────────────────
                if open_position is not None:
                    pos = open_position
                    current_yes_mid = (yes_bid + yes_ask) / 2
                    current_no_mid = (no_bid + no_ask) / 2

                    if pos["side"] == "YES":
                        current_price = yes_bid  # sell at bid
                        current_pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                    else:
                        current_price = no_bid
                        current_pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100

                    # Exit conditions:
                    # 1. End of 15-minute window (settlement)
                    # 2. Stop-loss (-15%)
                    # 3. Edge has flipped / compressed significantly
                    is_end_of_window = (i == len(window_candles) - 1)
                    is_stop_loss = current_pnl_pct <= -15.0

                    # Edge compression check
                    current_edge = abs(implied_up_prob - current_yes_mid) * 100
                    edge_compressed = current_edge < (pos["edge_at_entry"] / 3)

                    if is_end_of_window or is_stop_loss or edge_compressed:
                        # Exit the position
                        exit_value = pos["contracts"] * current_price
                        profit = exit_value - (pos["contracts"] * pos["entry_price"])
                        self.capital += exit_value

                        self.trades.append({
                            "timestamp": candle["timestamp"].isoformat(),
                            "market": f"{self.symbol} 15m {pos['side']}",
                            "side": pos["side"],
                            "strategy": "price_lag",
                            "entry_price": pos["entry_price"],
                            "exit_price": current_price,
                            "quantity": pos["contracts"],
                            "profit_usdc": profit,
                            "win": profit > 0,
                            "exit_reason": "settlement" if is_end_of_window else ("stop_loss" if is_stop_loss else "edge_compression"),
                        })
                        open_position = None

            # ── Record equity snapshot (every 15 minutes) ────────────────
            self.equity_curve.append({
                "timestamp": current_window_start.isoformat(),
                "capital": self.capital,
            })

            # ── Progress ─────────────────────────────────────────────────
            current_window_start = window_end

        # ── Compute performance metrics ────────────────────────────────
        return self._compute_performance(candles)

    def _compute_performance(self, candles: list[dict]) -> dict:
        """
        Calculate performance metrics from the backtest results.

        Args:
            candles: Full candle list (for date range).

        Returns:
            Performance summary dict.
        """
        total_trades = len(self.trades)
        wins = sum(1 for t in self.trades if t["win"])
        losses = total_trades - wins
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        total_return = ((self.capital - self.initial_capital) / self.initial_capital) * 100

        # Max drawdown
        peak = self.initial_capital
        max_drawdown = 0.0
        for eq in self.equity_curve:
            if eq["capital"] > peak:
                peak = eq["capital"]
            dd = (peak - eq["capital"]) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # Sharpe ratio (using daily returns from equity curve)
        if len(self.equity_curve) > 1:
            daily_returns = []
            for i in range(1, len(self.equity_curve)):
                prev = self.equity_curve[i - 1]["capital"]
                curr = self.equity_curve[i]["capital"]
                if prev > 0:
                    daily_returns.append((curr - prev) / prev)

            if daily_returns:
                avg_return = np.mean(daily_returns)
                std_return = np.std(daily_returns)
                sharpe = (avg_return / std_return * math.sqrt(365)) if std_return > 0 else 0.0
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Total profit
        total_profit = sum(t["profit_usdc"] for t in self.trades)

        return {
            "symbol": self.symbol,
            "start_date": candles[0]["timestamp"].isoformat() if candles else "",
            "end_date": candles[-1]["timestamp"].isoformat() if candles else "",
            "initial_capital": self.initial_capital,
            "final_capital": round(self.capital, 2),
            "total_return_pct": round(total_return, 2),
            "total_profit_usdc": round(total_profit, 2),
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "trades": self.trades,
            "equity_curve": self.equity_curve,
        }


# ---------------------------------------------------------------------------
# Results Plotting
# ---------------------------------------------------------------------------

def plot_results(results: dict, show: bool = True, save: bool = True) -> None:
    """
    Generate charts for the backtest results.

    Creates:
      1. Equity curve chart
      2. Drawdown chart
      3. Win/loss distribution

    Args:
        results: Performance summary dict from BacktestEngine.run().
        show:    Whether to display the charts interactively.
        save:    Whether to save charts to PNG files.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib not installed. Skipping charts.")
        return

    trades = results.get("trades", [])
    equity = results.get("equity_curve", [])
    symbol = results.get("symbol", "?")

    if not equity:
        logger.warning("No equity data to plot.")
        return

    # Prepare data
    times = []
    capital = []
    for eq in equity:
        try:
            times.append(datetime.fromisoformat(eq["timestamp"]))
            capital.append(eq["capital"])
        except (ValueError, TypeError):
            continue

    if not times:
        logger.warning("No valid equity timestamps.")
        return

    # Convert to numpy arrays
    times_arr = np.array(times)
    capital_arr = np.array(capital)

    # Calculate drawdown
    peak = np.maximum.accumulate(capital_arr)
    drawdown = (peak - capital_arr) / peak * 100

    # Create figure with 3 subplots
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(
        f"PolyTrader28 Backtest — {symbol}\n"
        f"Win Rate: {results.get('win_rate', 0):.1f}%  |  "
        f"Return: {results.get('total_return_pct', 0):+.2f}%  |  "
        f"Max DD: {results.get('max_drawdown_pct', 0):.1f}%  |  "
        f"Sharpe: {results.get('sharpe_ratio', 0):.2f}",
        fontsize=13, fontweight="bold", y=1.02,
    )

    # ── Equity curve ────────────────────────────────────────────────────
    ax1 = axes[0]
    ax1.plot(times_arr, capital_arr, color="#00ff88", linewidth=1.5, label="Capital")
    ax1.axhline(y=results.get("initial_capital", 0), color="#6a6a7a", linestyle="--",
                linewidth=0.8, alpha=0.5, label="Initial Capital")
    ax1.fill_between(times_arr, results.get("initial_capital", 0), capital_arr,
                     where=(capital_arr >= results.get("initial_capital", 0)),
                     color="#00ff88", alpha=0.1)
    ax1.fill_between(times_arr, capital_arr, results.get("initial_capital", 0),
                     where=(capital_arr < results.get("initial_capital", 0)),
                     color="#ff3355", alpha=0.1)
    ax1.set_ylabel("Capital (USDC)")
    ax1.legend(loc="upper left", fontsize=10)
    ax1.grid(True, alpha=0.2)
    ax1.set_facecolor("#0a0a0f")

    # ── Drawdown ────────────────────────────────────────────────────────
    ax2 = axes[1]
    ax2.fill_between(times_arr, 0, drawdown, color="#ff3355", alpha=0.3)
    ax2.plot(times_arr, drawdown, color="#ff3355", linewidth=1)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_ylim(bottom=0)
    ax2.invert_yaxis()
    ax2.grid(True, alpha=0.2)
    ax2.set_facecolor("#0a0a0f")

    # ── Trade P&L scatter ────────────────────────────────────────────────
    ax3 = axes[2]
    if trades:
        trade_times = []
        trade_pnl = []
        colors = []
        for t in trades:
            try:
                trade_times.append(datetime.fromisoformat(t["timestamp"]))
                pnl = t.get("profit_usdc", 0)
                trade_pnl.append(pnl)
                colors.append("#00ff88" if pnl >= 0 else "#ff3355")
            except (ValueError, TypeError):
                continue

        if trade_times:
            ax3.scatter(trade_times, trade_pnl, c=colors, s=15, alpha=0.7)
            ax3.axhline(y=0, color="#6a6a7a", linestyle="-", linewidth=0.5)
            ax3.set_ylabel("Trade P&L (USDC)")

    ax3.set_xlabel("Date")
    ax3.grid(True, alpha=0.2)
    ax3.set_facecolor("#0a0a0f")

    # Format x-axis dates
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        ax.tick_params(colors="#6a6a7a")
        for spine in ax.spines.values():
            spine.set_color("#1a1a28")

    plt.tight_layout()

    # Save
    if save:
        output_dir = _project_root / "backtest_results"
        output_dir.mkdir(exist_ok=True)
        filename = output_dir / f"backtest_{symbol}_{results.get('start_date', 'unknown')[:10]}.png"
        plt.savefig(filename, dpi=150, bbox_inches="tight", facecolor="#0a0a0f")
        logger.info("Chart saved to %s", filename)

    # Show
    if show:
        plt.show()
    else:
        plt.close()


def print_summary(results: dict) -> None:
    """
    Print a formatted performance summary to the console.

    Args:
        results: Performance summary dict from BacktestEngine.run().
    """
    if "error" in results:
        print(f"\n  ERROR: {results['error']}")
        return

    print()
    print("=" * 60)
    print(f"  BACKTEST SUMMARY — {results.get('symbol', '?')}")
    print("=" * 60)
    print(f"  Period:       {results.get('start_date', '?')[:10]} → {results.get('end_date', '?')[:10]}")
    print(f"  Initial:      ${results.get('initial_capital', 0):.2f}")
    print(f"  Final:        ${results.get('final_capital', 0):.2f}")
    print(f"  Total Return: {results.get('total_return_pct', 0):+.2f}%")
    print(f"  Total P&L:    ${results.get('total_profit_usdc', 0):.2f}")
    print(f"  Trades:       {results.get('total_trades', 0)}")
    print(f"  Wins:         {results.get('wins', 0)}")
    print(f"  Losses:       {results.get('losses', 0)}")
    print(f"  Win Rate:     {results.get('win_rate', 0):.1f}%")
    print(f"  Max DD:       {results.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Sharpe:       {results.get('sharpe_ratio', 0):.2f}")
    print("=" * 60)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    CLI entry point for the backtester.
    """
    parser = argparse.ArgumentParser(
        description="PolyTrader28 — Backtesting Module",
    )
    parser.add_argument(
        "--symbol", type=str, default="BTC",
        choices=["BTC", "ETH"],
        help="Symbol to backtest (default: BTC)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Days of historical data (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--capital", type=float, default=DEFAULT_INITIAL_CAPITAL,
        help=f"Initial capital in USDC (default: ${DEFAULT_INITIAL_CAPITAL})",
    )
    parser.add_argument(
        "--edge", type=float, default=3.0,
        help="Minimum edge threshold %% (default: 3.0)",
    )
    parser.add_argument(
        "--plot", action="store_true", default=True,
        help="Show interactive charts (default: True)",
    )
    parser.add_argument(
        "--no-plot", action="store_false", dest="plot",
        help="Disable interactive charts",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run backtest for both BTC and ETH",
    )

    args = parser.parse_args()

    symbols = ["BTC", "ETH"] if args.all else [args.symbol]

    for symbol in symbols:
        # Load data
        candles = _load_historical_data_ccxt(symbol, args.days)
        if not candles:
            print(f"ERROR: No data loaded for {symbol}")
            continue

        # Run backtest
        engine = BacktestEngine(
            symbol=symbol,
            initial_capital=args.capital,
            min_edge=args.edge,
        )
        results = engine.run(candles)

        # Output
        print_summary(results)
        plot_results(results, show=args.plot)


if __name__ == "__main__":
    main()
