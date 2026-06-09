"""
price_lag.py — Strategy A: Time Arbitrage / Price-Lag
=======================================================
Exploits the lag between real-time Binance prices and Polymarket's 15-minute
contract pricing.

Concept:
  1. Track the opening price of BTC/ETH at the start of each 15-minute period
     (from Binance trade stream).
  2. Compute the "implied probability" that the price will be UP or DOWN at
     the end of the period, based on current price vs. period open.
  3. Fetch Polymarket's current YES/NO prices for the corresponding 15-minute
     contract.
  4. If the difference between the market-implied probability and the
     exchange-implied probability exceeds the configured edge threshold,
     enter a trade on the mispriced side.

Edge calculation:
  - If current_price > period_open:
        UP implied probability ≈ (current - open) / (open * volatility_factor)
    Realistically, we use a simplified model:
        UP_prob = 1 / (1 + exp(-k * (current - open) / open))
    This logistic function maps price changes to a 0-1 probability.
  - Compare UP_prob to Polymarket's YES price (which represents the market's
    probability of UP).
  - If |UP_prob - yes_price| > min_edge, trade the discrepancy.

Usage:
    from strategy.price_lag import PriceLagStrategy
    strategy = PriceLagStrategy()
    opportunity = strategy.evaluate(btc_price, btc_open, eth_price, eth_open,
                                    polymarket_prices)
"""

import math
from typing import Optional

from config import config
from utils.logger import logger
from models import insert_opportunity


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Logistic function steepness factor (k).  Higher = more sensitive to price changes.
# Derived from empirical volatility: BTC 15m typical move ~0.1%
LOGISTIC_K = 500.0

# Minimum price movement (as fraction) to consider a signal meaningful
MIN_PRICE_MOVE_FRACTION = 0.0002  # 0.02%


class PriceLagStrategy:
    """
    Strategy A: Time Arbitrage / Price-Lag.

    Detects discrepancies between Binance real-time prices and Polymarket's
    15-minute contract pricing.
    """

    def __init__(self):
        """Initialise the strategy tracker."""
        self._name = "price_lag"

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Return the strategy name identifier."""
        return self._name

    def evaluate(
        self,
        symbol: str,
        current_price: float,
        period_open: float,
        polymarket_yes_price: float,
        polymarket_no_price: float,
        tick_size: str = "0.01",
    ) -> Optional[dict]:
        """
        Evaluate a single 15-minute market for a price-lag arbitrage opportunity.

        Args:
            symbol:              "BTC" or "ETH".
            current_price:       Current Binance trade price.
            period_open:         Period opening price (first trade of 15m window).
            polymarket_yes_price: Current Polymarket YES midpoint price.
            polymarket_no_price: Current Polymarket NO midpoint price.
            tick_size:           Minimum price tick for the market.

        Returns:
            Opportunity dict if a viable trade is found, else None.
            Dict keys:
                - market:       str   (e.g. "BTC 15m Up/Down")
                - strategy:     str   ("price_lag")
                - side:         str   ("YES" or "NO")
                - edge_pct:     float (edge percentage)
                - entry_price:  float (limit price to use)
                - confidence:   float (signal strength 0-1)
                - implied_prob: float
                - market_prob:  float
        """
        # Guard: no data to work with
        if current_price <= 0 or period_open <= 0:
            return None

        # Compute price change fraction
        price_change = (current_price - period_open) / period_open

        # Skip if price hasn't moved enough
        if abs(price_change) < MIN_PRICE_MOVE_FRACTION:
            return None

        # Compute implied probability using logistic function
        # P(UP) = 1 / (1 + exp(-k * price_change))
        # When price is up, P > 0.5; when down, P < 0.5.
        try:
            implied_up_prob = 1.0 / (1.0 + math.exp(-LOGISTIC_K * price_change))
        except OverflowError:
            # In extreme cases, the exponent can overflow
            implied_up_prob = 1.0 if price_change > 0 else 0.0

        # Clamp to [0.01, 0.99] to avoid extreme probabilities
        implied_up_prob = max(0.01, min(0.99, implied_up_prob))

        # Polymarket YES price = market's probability of UP
        market_up_prob = polymarket_yes_price

        # Edge = discrepancy between our model and the market
        # If implied > market: the market underestimates UP → buy YES
        # If implied < market: the market overestimates UP → buy NO
        if implied_up_prob > market_up_prob:
            # Market is underpricing YES relative to our model
            edge = implied_up_prob - market_up_prob
            side = "YES"
            entry_price = polymarket_yes_price
            market_label = f"{symbol} 15m Up"
        else:
            # Market is overpricing YES → NO is undervalued
            edge = market_up_prob - implied_up_prob
            side = "NO"
            entry_price = polymarket_no_price
            market_label = f"{symbol} 15m Down"

        edge_pct = edge * 100  # convert to percentage

        # Check if edge exceeds the minimum threshold
        if edge_pct < config.MIN_EDGE_THRESHOLD:
            # Log the sub-threshold opportunity for analysis
            insert_opportunity(
                market=market_label,
                strategy=self._name,
                edge_pct=edge_pct,
                yes_price=polymarket_yes_price,
                no_price=polymarket_no_price,
                executed=False,
                reason=f"Edge {edge_pct:.2f}% below threshold {config.MIN_EDGE_THRESHOLD}%",
            )
            return None

        # Build the opportunity dict
        opportunity = {
            "market": market_label,
            "strategy": self._name,
            "side": side,
            "edge_pct": round(edge_pct, 2),
            "entry_price": entry_price,
            "confidence": min(1.0, edge_pct / config.MIN_EDGE_THRESHOLD),
            "implied_prob": round(implied_up_prob, 4),
            "market_prob": round(market_up_prob, 4),
            "symbol": symbol,
            "current_price": current_price,
            "period_open": period_open,
        }

        logger.info(
            "[%s] %s | Edge: %.2f%% | Implied: %.1f%% | Market: %.1f%% | Side: %s",
            self._name, market_label, edge_pct,
            implied_up_prob * 100, market_up_prob * 100, side,
        )

        # Log the opportunity to the database
        insert_opportunity(
            market=market_label,
            strategy=self._name,
            edge_pct=edge_pct,
            yes_price=polymarket_yes_price,
            no_price=polymarket_no_price,
            executed=False,  # will be updated when order is placed
            reason=f"Edge {edge_pct:.2f}% >= threshold {config.MIN_EDGE_THRESHOLD}%",
        )

        return opportunity

    def estimate_exit_price(
        self,
        side: str,
        polymarket_yes_price: float,
        polymarket_no_price: float,
    ) -> float:
        """
        Estimate the exit price for an existing position.

        For a YES position, exit by selling at the current YES bid price.
        For a NO position, exit by selling at the current NO bid price.

        Args:
            side:                "YES" or "NO".
            polymarket_yes_price: Current YES price.
            polymarket_no_price: Current NO price.

        Returns:
            Estimated exit price per contract. 0.0 if unknown.
        """
        if side == "YES":
            return polymarket_yes_price
        elif side == "NO":
            return polymarket_no_price
        return 0.0

    def should_close_early(
        self,
        side: str,
        entry_price: float,
        current_yes_price: float,
        current_no_price: float,
        edge_at_entry: float,
    ) -> tuple[bool, str]:
        """
        Determine if a position should be closed early.

        Early exit triggers:
          1. Edge has flipped (the opportunity reversed direction).
          2. The edge has compressed significantly (< 1/3 of entry edge).

        Args:
            side:             "YES" or "NO".
            entry_price:      Price paid at entry.
            current_yes_price: Current YES midpoint price.
            current_no_price: Current NO midpoint price.
            edge_at_entry:    Edge percentage at time of entry.

        Returns:
            Tuple of (should_close: bool, reason: str).
        """
        current_price = current_yes_price if side == "YES" else current_no_price
        price_change_pct = ((current_price - entry_price) / entry_price) * 100

        # Check for stop-loss (handled by risk manager, but also check here)
        if price_change_pct <= config.STOP_LOSS_PCT:
            return True, f"Stop-loss triggered: {price_change_pct:.2f}%"

        # Current edge
        if side == "YES":
            current_edge = abs(current_yes_price - (1.0 - current_no_price))
        else:
            current_edge = abs(current_no_price - (1.0 - current_yes_price))

        # If edge has compressed below 1/3 of entry edge, close early
        if current_edge < (edge_at_entry / 3) and current_edge < 1.0:
            return True, f"Edge compressed from {edge_at_entry:.2f}% to {current_edge:.2f}%"

        return False, ""
