"""
polymarket_api.py — Polymarket CLOB V2 & Gamma API Client
==========================================================
Provides a complete interface to Polymarket's APIs using the official V2 SDK:

  - Gamma API (public, no auth):  Market discovery, metadata
  - CLOB V2 SDK (L1 + L2 auth):  Order books, order placement, cancellation

Authentication flow (V2):
  - L1: Wallet private key signs EIP-712 message to derive API credentials
  - L2: HMAC-SHA256 with derived API key/secret/passphrase for all trading requests

The V2 SDK (py-clob-client-v2) handles all EIP-712 signing internally.
No manual signature generation needed.

In dry-run mode, all methods work in simulation — orders are logged but not
actually placed on Polymarket.

Usage:
    from data.polymarket_api import PolymarketAPI
    api = PolymarketAPI()
    markets = api.get_active_15m_markets()
    book = api.get_order_book(token_id="0x...")
    result = api.place_order(token_id="0x...", side="BUY", price=0.50, size=10)
"""

import json
import os
import time
from decimal import Decimal
from typing import Any, Optional

import requests

from config import config
from utils.logger import logger


# ---------------------------------------------------------------------------
# API base URLs
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


class PolymarketAPI:
    """
    Client for Polymarket's Gamma (market data) and CLOB V2 (trading) APIs.

    This client handles:
      - Fetching active 15-minute BTC/ETH markets (via Gamma API)
      - Retrieving order books for specific token IDs (via CLOB V2 SDK)
      - Placing and cancelling orders (via CLOB V2 SDK with EIP-712 signing)
      - Checking USDC/pUSD balance (via CLOB V2 SDK)

    Thread-safe: the V2 SDK client is initialised once and reused.
    """

    def __init__(self):
        """Initialise the API client. Creates the V2 SDK client if credentials exist."""
        # HTTP session for Gamma API (public, no auth needed)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "PolyTrader28/2.0",
            "Accept": "application/json",
        })

        # Support proxy via env vars (HTTP_PROXY, HTTPS_PROXY)
        # Also support a custom POLYMARKET_PROXY for proxy-only-for-Polymarket
        proxy_url = (
            config.POLYMARKET_PROXY
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
        )
        if proxy_url:
            self._session.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
            logger.info("Using proxy for Polymarket API: %s", proxy_url)

        # Cache for market data
        self._market_cache: dict[str, dict] = {}
        self._cached_15m_markets: list[dict] = []
        self._last_market_refresh: float = 0.0

        # V2 SDK client — lazily initialised on first live trading call
        self._clob_client: Optional[Any] = None
        self._clob_initialised: bool = False
        self._init_error: Optional[str] = None

    # ──────────────────────────────────────────────────────────────────────
    # V2 SDK Client Initialisation
    # ──────────────────────────────────────────────────────────────────────

    def _ensure_clob_client(self) -> Optional[Any]:
        """
        Lazily initialise the CLOB V2 SDK client.

        Returns the client if successful, None if credentials are missing
        or initialisation fails.  Caches the result so we only try once.
        """
        if self._clob_initialised:
            if self._clob_client is not None:
                return self._clob_client
            # Initialisation failed previously
            return None

        self._clob_initialised = True

        # Check for required credentials
        private_key = config.WALLET_PRIVATE_KEY
        if not private_key:
            self._init_error = "WALLET_PRIVATE_KEY not configured"
            logger.warning("Cannot initialise CLOB V2 client: %s", self._init_error)
            return None

        try:
            # Import V2 SDK
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.exceptions import PolyApiException
        except ImportError:
            self._init_error = "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
            logger.error(self._init_error)
            return None

        try:
            # Determine signature type:
            #   0 = EOA wallet (MetaMask, hardware wallet, direct private key)
            #   1 = Email/Magic wallet (Polymarket proxy)
            # Default to 0 (EOA) since we have a private key
            signature_type = 0
            funder = config.WALLET_ADDRESS or ""

            # Initialise the V2 client
            self._clob_client = ClobClient(
                host=CLOB_API_BASE,
                key=private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=signature_type,
                funder=funder if funder else None,
            )

            # Derive or create API credentials (L1 → L2)
            # V2 splits this into derive_api_key() (returns existing) and
            # create_api_key() (creates new). Try derive first, fall back to create.
            try:
                api_creds = self._clob_client.derive_api_key()
                logger.info("Derived existing API credentials")
            except PolyApiException:
                # No existing credentials — create new ones
                api_creds = self._clob_client.create_api_key()
                logger.info("Created new API credentials")
                # Log the credentials so the user can save them
                logger.info(
                    "API Key: %s | Secret: %s | Passphrase: %s",
                    api_creds.api_key,
                    api_creds.api_secret,
                    api_creds.api_passphrase,
                )

            # Set credentials on the client for L2 authenticated requests
            self._clob_client.set_api_creds(api_creds)

            logger.info("CLOB V2 client initialised successfully")
            return self._clob_client

        except Exception as exc:
            self._init_error = f"CLOB V2 init failed: {exc}"
            logger.error(self._init_error)
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Public: Market Discovery (Gamma API — no auth required)
    # ──────────────────────────────────────────────────────────────────────

    def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        tag: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch markets from the Gamma API.

        Args:
            active: Filter for active (trading) markets.
            closed: Include closed/resolved markets.
            limit:  Maximum results per page (max 100).
            offset: Pagination offset.
            tag:    Optional tag slug to filter by (e.g. "bitcoin", "ethereum").

        Returns:
            List of market dicts from the Gamma API.
        """
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": min(limit, 100),
            "offset": offset,
        }
        if tag:
            params["tag"] = tag

        try:
            resp = self._session.get(
                f"{GAMMA_API_BASE}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Gamma API /markets failed: %s", exc)
            return []

    def get_active_15m_markets(self) -> list[dict]:
        """
        Fetch active 15-minute BTC and ETH Up/Down markets.

        These are Polymarket's "BTC 15-Minute Up/Down" and
        "ETH 15-Minute Up/Down" contracts.  The results are cached for
        60 seconds to avoid excessive API calls.

        Returns:
            List of market dicts, each containing:
                - question:         str  (e.g. "Will BTC be above...?")
                - clobTokenIds:     list[str]  [yes_token_id, no_token_id]
                - conditionId:      str
                - negRisk:          bool
                - minimumTickSize:  str
                - ...and other Gamma API fields.
        """
        now = time.time()
        # Refresh cache every 60 seconds
        if self._cached_15m_markets and (now - self._last_market_refresh) < 60:
            return self._cached_15m_markets

        all_markets = []
        # Fetch multiple pages to find 15-minute markets
        for offset in range(0, 500, 100):
            batch = self.get_markets(active=True, closed=False, limit=100, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break

        # Filter for 15-minute BTC and ETH markets
        keywords_btc = ["btc", "bitcoin", "15-minute", "15m", "15 min"]
        keywords_eth = ["eth", "ethereum", "15-minute", "15m", "15 min"]
        keywords_up = ["up", "above", "higher"]
        keywords_down = ["down", "below", "lower"]

        fifteen_min_markets = []
        seen_condition_ids = set()

        for m in all_markets:
            question = (m.get("question") or "").lower()
            condition_id = m.get("conditionId") or ""

            if condition_id in seen_condition_ids:
                continue

            # Check if it's a 15-minute market (BTC or ETH)
            is_btc = any(k in question for k in keywords_btc)
            is_eth = any(k in question for k in keywords_eth)
            is_15m = any(k in question for k in ["15-minute", "15m", "15 min", "15 minute"])

            if not (is_15m and (is_btc or is_eth)):
                continue

            is_up = any(k in question for k in keywords_up)
            is_down = any(k in question for k in keywords_down)

            if not (is_up or is_down):
                # If we can't tell, include it anyway — better to have false positives
                pass

            fifteen_min_markets.append(m)
            seen_condition_ids.add(condition_id)

        self._cached_15m_markets = fifteen_min_markets
        self._last_market_refresh = now

        logger.info(
            "Found %d active 15-minute markets",
            len(fifteen_min_markets),
        )
        return fifteen_min_markets

    def get_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """
        Fetch a single market by its condition ID.

        Args:
            condition_id: The condition ID (0x-prefixed hex string).

        Returns:
            Market dict, or None if not found.
        """
        try:
            resp = self._session.get(
                f"{GAMMA_API_BASE}/markets",
                params={"condition_ids": condition_id, "limit": 1},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data[0] if data else None
        except (requests.RequestException, IndexError, KeyError) as exc:
            logger.error("Failed to fetch market %s: %s", condition_id, exc)
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Public: Order Book (CLOB V2 SDK or fallback to REST)
    # ──────────────────────────────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> Optional[dict]:
        """
        Get the order book for a specific token.

        Uses the CLOB V2 SDK if available, falls back to direct REST call.

        Args:
            token_id: The CLOB token ID (from clobTokenIds in market data).

        Returns:
            Order book dict with keys: market, asset_id, bids, asks,
            min_order_size, tick_size, neg_risk, last_trade_price.
            Returns None on failure.
        """
        # Try SDK first
        client = self._ensure_clob_client()
        if client is not None:
            try:
                book = client.get_order_book(token_id)
                if book:
                    return {
                        "market": token_id,
                        "bids": [{"price": b.price, "size": b.size} for b in book.bids] if book.bids else [],
                        "asks": [{"price": a.price, "size": a.size} for a in book.asks] if book.asks else [],
                        "min_order_size": str(book.min_order_size) if hasattr(book, 'min_order_size') else "1",
                        "tick_size": str(book.tick_size) if hasattr(book, 'tick_size') else "0.01",
                        "neg_risk": book.neg_risk if hasattr(book, 'neg_risk') else False,
                        "last_trade_price": str(book.last_trade_price) if hasattr(book, 'last_trade_price') else "0",
                    }
            except Exception as exc:
                logger.warning("SDK get_order_book failed, falling back to REST: %s", exc)

        # Fallback: direct REST call (public endpoint, no auth needed)
        try:
            resp = self._session.get(
                f"{CLOB_API_BASE}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("CLOB /book failed for %s: %s", token_id, exc)
            return None

    def get_order_books(self, token_ids: list[str]) -> dict[str, Optional[dict]]:
        """
        Get order books for multiple tokens in parallel.

        Args:
            token_ids: List of CLOB token IDs.

        Returns:
            Dict mapping token_id -> order book (or None on failure).
        """
        results: dict[str, Optional[dict]] = {}
        for tid in token_ids:
            results[tid] = self.get_order_book(tid)
        return results

    # ──────────────────────────────────────────────────────────────────────
    # Public: Midpoint price helpers
    # ──────────────────────────────────────────────────────────────────────

    def get_midpoint_prices(self, token_id_yes: str, token_id_no: str) -> dict:
        """
        Get the midpoint YES and NO prices for a binary market.

        The midpoint is the average of the best bid and best ask.

        Args:
            token_id_yes: CLOB token ID for the YES outcome.
            token_id_no:  CLOB token ID for the NO outcome.

        Returns:
            Dict with keys:
                yes_price: float (midpoint) or 0.0
                no_price:  float (midpoint) or 0.0
                spread:    float (bid-ask spread)
                yes_bid:   float
                yes_ask:   float
                no_bid:    float
                no_ask:    float
        """
        result = {
            "yes_price": 0.0,
            "no_price": 0.0,
            "spread": 0.0,
            "yes_bid": 0.0,
            "yes_ask": 0.0,
            "no_bid": 0.0,
            "no_ask": 0.0,
        }

        # Try SDK midpoint first (faster, single call)
        client = self._ensure_clob_client()
        if client is not None:
            try:
                yes_mid = client.get_midpoint(token_id_yes)
                no_mid = client.get_midpoint(token_id_no)
                if yes_mid is not None:
                    result["yes_price"] = float(yes_mid)
                if no_mid is not None:
                    result["no_price"] = float(no_mid)
                # Get spread from order books
                books = self.get_order_books([token_id_yes, token_id_no])
                yes_book = books.get(token_id_yes)
                no_book = books.get(token_id_no)
                if yes_book:
                    bids = yes_book.get("bids", [])
                    asks = yes_book.get("asks", [])
                    result["yes_bid"] = float(bids[0]["price"]) if bids else 0.0
                    result["yes_ask"] = float(asks[0]["price"]) if asks else 0.0
                if no_book:
                    bids = no_book.get("bids", [])
                    asks = no_book.get("asks", [])
                    result["no_bid"] = float(bids[0]["price"]) if bids else 0.0
                    result["no_ask"] = float(asks[0]["price"]) if asks else 0.0
                if result["yes_bid"] > 0 and result["yes_ask"] > 0:
                    result["spread"] = (
                        (result["yes_ask"] - result["yes_bid"])
                        + (result["no_ask"] - result["no_bid"])
                    ) / 2
                return result
            except Exception as exc:
                logger.warning("SDK midpoint failed, falling back to order books: %s", exc)

        # Fallback: get order books and compute midpoints manually
        books = self.get_order_books([token_id_yes, token_id_no])

        yes_book = books.get(token_id_yes)
        no_book = books.get(token_id_no)

        if yes_book:
            bids = yes_book.get("bids", [])
            asks = yes_book.get("asks", [])
            yes_bid = float(bids[0]["price"]) if bids else 0.0
            yes_ask = float(asks[0]["price"]) if asks else 0.0
            result["yes_bid"] = yes_bid
            result["yes_ask"] = yes_ask
            if yes_bid > 0 and yes_ask > 0:
                result["yes_price"] = (yes_bid + yes_ask) / 2

        if no_book:
            bids = no_book.get("bids", [])
            asks = no_book.get("asks", [])
            no_bid = float(bids[0]["price"]) if bids else 0.0
            no_ask = float(asks[0]["price"]) if asks else 0.0
            result["no_bid"] = no_bid
            result["no_ask"] = no_ask
            if no_bid > 0 and no_ask > 0:
                result["no_price"] = (no_bid + no_ask) / 2

        # Overall spread
        if result["yes_bid"] > 0 and result["yes_ask"] > 0:
            result["spread"] = (
                (result["yes_ask"] - result["yes_bid"])
                + (result["no_ask"] - result["no_bid"])
            ) / 2

        return result

    # ──────────────────────────────────────────────────────────────────────
    # L2 Authenticated: Account / Balance
    # ──────────────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """
        Get the USDC/pUSD balance of the user's wallet.

        Returns 0.0 in dry-run mode or if authentication fails.

        Returns:
            Balance as a float.
        """
        if config.is_dry_run:
            # In dry-run mode, return simulated balance from DB
            from models import get_current_bankroll
            return get_current_bankroll()

        client = self._ensure_clob_client()
        if client is None:
            logger.warning("Cannot fetch balance: CLOB client not initialised")
            return 0.0

        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            balance_data = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # Balance is returned as a string with 6 decimal places
            balance_str = str(balance_data.balance) if hasattr(balance_data, 'balance') else "0"
            return float(balance_str) / 1_000_000
        except Exception as exc:
            logger.error("Failed to fetch balance: %s", exc)
            return 0.0

    def get_allowance(self) -> float:
        """
        Get the USDC/pUSD allowance for the CLOB exchange contract.

        Returns:
            Allowance as a float (0.0 if not authenticated / error).
        """
        client = self._ensure_clob_client()
        if client is None:
            return 0.0

        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            allowance_data = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            allowance_str = str(allowance_data.allowance) if hasattr(allowance_data, 'allowance') else "0"
            return float(allowance_str) / 1_000_000
        except Exception as exc:
            logger.error("Failed to fetch allowance: %s", exc)
            return 0.0

    # ──────────────────────────────────────────────────────────────────────
    # L2 Authenticated: Order Management (V2 SDK with EIP-712 signing)
    # ──────────────────────────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,           # "BUY" or "SELL"
        price: float,
        size: int,            # number of contracts
        order_type: str = "GTC",
    ) -> Optional[dict]:
        """
        Place a limit order on Polymarket using CLOB V2 SDK.

        The V2 SDK handles EIP-712 signing internally — no manual signature
        generation needed. Orders are signed with the wallet's private key
        and submitted to the CLOB for matching.

        In dry-run mode, the order is simulated (logged to DB, not actually sent).

        Args:
            token_id:   The CLOB token ID to trade.
            side:       "BUY" or "SELL".
            price:      Limit price per contract (in USDC/pUSD).
            size:       Number of contracts.
            order_type: Time-in-force: "GTC", "FOK", "GTD", "FAK".

        Returns:
            Response dict on success, None on failure.
            Dry-run keys: {"success": True, "is_dry_run": True,
                           "orderID": "dry_run_<timestamp>", ...}
        """
        # ── Dry-run: simulate ────────────────────────────────────────────
        if config.is_dry_run:
            logger.info(
                "DRY-RUN: Would place %s order: token=%s side=%s price=%.4f size=%d",
                order_type, token_id, side, price, size,
            )
            return {
                "success": True,
                "is_dry_run": True,
                "orderID": f"dry_run_{int(time.time()*1000)}",
                "status": "simulated",
                "makingAmount": str(int(size * 1_000_000)),
                "takingAmount": str(int(size * price * 1_000_000)),
                "errorMsg": "",
            }

        # ── Live: use V2 SDK ─────────────────────────────────────────────
        client = self._ensure_clob_client()
        if client is None:
            logger.error("Cannot place order: CLOB V2 client not initialised. %s", self._init_error or "")
            return None

        try:
            # Import V2 types
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            # Map order type string to V2 enum
            order_type_map = {
                "GTC": OrderType.GTC,
                "FOK": OrderType.FOK,
                "FAK": OrderType.FAK,
                "GTD": OrderType.GTD,
            }
            v2_order_type = order_type_map.get(order_type.upper(), OrderType.GTC)

            # Map side string to V2 constant
            side_constant = BUY if side.upper() == "BUY" else SELL

            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_constant,
            )

            # Get market info for tick_size and neg_risk
            # We need these for the order options
            market_info = None
            try:
                market_info = client.get_market(token_id)
            except Exception:
                # If we can't get market info, use defaults
                pass

            tick_size = "0.01"
            neg_risk = False
            if market_info:
                tick_size = str(getattr(market_info, 'minimum_tick_size', '0.01'))
                neg_risk = getattr(market_info, 'neg_risk', False)

            # Place the order (SDK handles EIP-712 signing internally)
            from py_clob_client_v2.clob_types import PartialCreateOrderOptions
            options = PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            )

            response = client.create_and_post_order(order_args, options=options)

            order_id = getattr(response, 'orderID', '') or getattr(response, 'order_id', '')
            logger.info("Order placed successfully: %s", order_id)

            return {
                "success": True,
                "is_dry_run": False,
                "orderID": order_id,
                "status": getattr(response, 'status', 'placed'),
                "makingAmount": str(getattr(response, 'makingAmount', '')),
                "takingAmount": str(getattr(response, 'takingAmount', '')),
                "errorMsg": "",
            }

        except Exception as exc:
            logger.error("Failed to place order: %s", exc)
            return {
                "success": False,
                "is_dry_run": False,
                "orderID": "",
                "status": "failed",
                "makingAmount": "0",
                "takingAmount": "0",
                "errorMsg": str(exc),
            }

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order on Polymarket.

        Args:
            order_id: The order ID to cancel.

        Returns:
            True if cancellation was successful, False otherwise.
        """
        if config.is_dry_run:
            logger.info("DRY-RUN: Would cancel order %s", order_id)
            return True

        client = self._ensure_clob_client()
        if client is None:
            return False

        try:
            client.cancel(order_id)
            logger.info("Order cancelled: %s", order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders for the user.

        Returns:
            True if all cancellations were successful, False otherwise.
        """
        if config.is_dry_run:
            logger.info("DRY-RUN: Would cancel all orders")
            return True

        client = self._ensure_clob_client()
        if client is None:
            return False

        try:
            client.cancel_all()
            logger.info("All orders cancelled")
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    # ──────────────────────────────────────────────────────────────────────
    # L2 Authenticated: Open orders & trade history
    # ──────────────────────────────────────────────────────────────────────

    def get_open_orders(self) -> list[dict]:
        """
        Fetch the user's currently open orders.

        Returns:
            List of order dicts, or empty list on failure / dry-run.
        """
        if config.is_dry_run:
            return []

        client = self._ensure_clob_client()
        if client is None:
            return []

        try:
            orders = client.get_orders()
            if orders:
                return [
                    {
                        "id": getattr(o, 'id', ''),
                        "status": getattr(o, 'status', ''),
                        "token_id": getattr(o, 'token_id', ''),
                        "side": getattr(o, 'side', ''),
                        "price": getattr(o, 'price', 0),
                        "size": getattr(o, 'size', 0),
                    }
                    for o in orders
                ]
            return []
        except Exception as exc:
            logger.error("Failed to fetch open orders: %s", exc)
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Tick size helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def round_to_tick_size(price: float, tick_size: str) -> float:
        """
        Round a price to the nearest valid tick size for a market.

        Args:
            price:     The raw price to round.
            tick_size: Tick size as a string (e.g. "0.01", "0.001").

        Returns:
            Price rounded to the nearest valid tick.
        """
        tick = Decimal(tick_size)
        rounded = round(Decimal(str(price)) / tick) * tick
        return float(rounded)

    @staticmethod
    def round_to_min_size(size: int, min_size: str) -> int:
        """
        Ensure order size meets the minimum order size requirement.

        Args:
            size:    Desired number of contracts.
            min_size: Minimum order size as a string (e.g. "1", "10").

        Returns:
            Size rounded up to the minimum if needed.
        """
        min_s = int(min_size)
        return max(size, min_s)
