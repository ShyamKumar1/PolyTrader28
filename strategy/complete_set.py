"""
complete_set.py — Strategy B: Dual-Side / Complete-Set Arbitrage
=================================================================
Exploits mispricing where YES + NO < $1.00 for the same 15-minute contract.

Concept:
  Polymarket binary markets settle at $1.00 for the winning side and $0.00 for
  the losing side.  If YES + NO < $1.00, buying both sides guarantees a profit
  regardless of outcome:

    Profit per pair = $1.00 - (YES_price + NO_price)

  Example:
    YES = $0.48, NO = $0.48  → sum = $0.96  → profit = $0.04 per pair (4.17%)

This is a pure arbitrage — zero directional risk. The bot must:
  1. Scan all active 15-minute BTC and ETH contracts every second.
  2. When YES + NO <= COMPLETE_SET_THRESHOLD (default 0.985), buy both sides.
  3. Hold both positions until settlement (or sell both if pricing normalizes).

Usage:
    from strategy.complete_set import CompleteSetStrategy
    strategy = CompleteSetStrategy()
    opportunity = strategy.evaluate(yes_price, no_price)
"""

from typing import Optional

from config import config
from utils.logger import logger
from models import insert_opportunity


class CompleteSetStrategy:
    """
    Strategy B: Dual-Side / Complete-Set Arbitrage.

    Buys both YES and NO when their sum is below $1.00, locking in a
    risk-free profit.
    """

    def __init__(self):
        """Initialise the strategy."""
        self._name = "complete_set"

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Return the strategy name identifier."""
        return self._name

    def evaluate(
        self,
        market_label: str,
        yes_price: float,
        no_price: float,
        tick_size: str = "0.01",
    ) -> Optional[dict]:
        """
        Evaluate a single market for a complete-set arbitrage opportunity.

        Args:
            market_label: Human-readable market name.
            yes_price:    Current YES midpoint price.
            no_price:     Current NO midpoint price.
            tick_size:    Minimum price tick for the market.

        Returns:
            Opportunity dict if viable, else None.
            Dict keys:
                - market:         str
                - strategy:       str
                - side:           str ("BOTH")
                - yes_price:      float
                - no_price:       float
                - sum_price:      float
                - profit_per_pair: float (profit in USDC per pair of contracts)
                - profit_pct:     float (profit as % of investment)
                - entry_yes_price: float
                - entry_no_price:  float
        """
        # Validate inputs
        if yes_price <= 0 or no_price <= 0:
            return None

        sum_price = yes_price + no_price

        # Check if the sum is below the threshold
        if sum_price > config.COMPLETE_SET_THRESHOLD:
            return None

        # Calculate profit
        profit_per_pair = 1.0 - sum_price
        investment = sum_price
        profit_pct = (profit_per_pair / investment) * 100

        logger.info(
            "[%s] %s | YES=%.4f NO=%.4f SUM=%.4f | Profit=%.4f USDC (%.2f%%)",
            self._name, market_label, yes_price, no_price, sum_price,
            profit_per_pair, profit_pct,
        )

        # Log the opportunity
        insert_opportunity(
            market=market_label,
            strategy=self._name,
            edge_pct=round(profit_pct, 2),
            yes_price=yes_price,
            no_price=no_price,
            executed=False,
            reason=f"Complete-set arb: sum={sum_price:.4f} <= {config.COMPLETE_SET_THRESHOLD}",
        )

        return {
            "market": market_label,
            "strategy": self._name,
            "side": "BOTH",
            "yes_price": yes_price,
            "no_price": no_price,
            "sum_price": round(sum_price, 4),
            "profit_per_pair": round(profit_per_pair, 4),
            "profit_pct": round(profit_pct, 2),
            "entry_yes_price": yes_price,
            "entry_no_price": no_price,
        }

    def should_exit_early(
        self,
        entry_yes_price: float,
        entry_no_price: float,
        current_yes_price: float,
        current_no_price: float,
    ) -> tuple[bool, str]:
        """
        Check if we should exit a complete-set position early.

        Early exit is beneficial if the sum of current prices is higher than
        the entry sum (i.e., someone is willing to buy the pair at a better
        price than we paid, giving us an early profit).

        Args:
            entry_yes_price:   YES price paid at entry.
            entry_no_price:    NO price paid at entry.
            current_yes_price: Current YES midpoint price.
            current_no_price:  Current NO midpoint price.

        Returns:
            Tuple of (should_exit: bool, reason: str).
        """
        entry_sum = entry_yes_price + entry_no_price
        current_sum = current_yes_price + current_no_price

        # If the current sum is higher, we can sell both sides for a profit
        if current_sum > entry_sum:
            profit = current_sum - entry_sum
            profit_pct = (profit / entry_sum) * 100
            # Only exit if the profit is meaningful (> 0.5%)
            if profit_pct > 0.5:
                return True, f"Early exit profit: {profit_pct:.2f}%"

        # If the sum is now above $1.00 (unlikely), definitely exit
        if current_sum >= 1.0:
            return True, "Sum reached $1.00, exiting"

        return False, ""
