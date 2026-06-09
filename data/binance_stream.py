"""
binance_stream.py — Real-Time Price Feed from Binance WebSocket
================================================================
Streams live trade prices for BTC and ETH from Binance's public WebSocket.
Prices are stored in the database and also made available via a thread-safe
shared dictionary for other modules to read.

Handles reconnection with exponential backoff.

Usage:
    from data.binance_stream import BinanceStream
    stream = BinanceStream()
    stream.start()                          # launches background thread
    btc_price = stream.latest_prices["BTC"] # get latest price
    stream.stop()                           # graceful shutdown
"""

import json
import threading
import time
from typing import Optional
from decimal import Decimal

import websocket

from config import config
from utils.logger import logger
from models import insert_price_tick


# ---------------------------------------------------------------------------
# Binance WebSocket URLs
# ---------------------------------------------------------------------------
_BINANCE_BASE = "wss://stream.binance.com:9443/ws"

# Individual trade streams for BTC and ETH
_BTC_STREAM = "btcusdt@trade"
_ETH_STREAM = "ethusdt@trade"

# Combined stream URL (subscribe to both in one connection)
_COMBINED_URL = f"{_BINANCE_BASE}/{_BTC_STREAM}/{_ETH_STREAM}"


class BinanceStream:
    """
    Manages a WebSocket connection to Binance for real-time price data.

    Runs in a background daemon thread. Automatically reconnects on failure
    with exponential backoff.

    Thread-safe attributes:
        latest_prices: dict[str, float] — current price for "BTC" and "ETH".
        is_connected:  bool — True when WebSocket is actively receiving data.
    """

    def __init__(self):
        """Initialise the stream with default values (no connection yet)."""
        # Thread-safe shared state
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {"BTC": 0.0, "ETH": 0.0}
        self._connected = False
        self._should_stop = False

        # WebSocket internals
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None

        # For 15-minute candle tracking (uses trade prices to approximate)
        self._period_prices: dict[str, dict] = {
            "BTC": {"open": 0.0, "last_check": 0.0},
            "ETH": {"open": 0.0, "last_check": 0.0},
        }
        self._period_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────
    # Public properties (thread-safe)
    # ──────────────────────────────────────────────────────────────────────

    @property
    def latest_prices(self) -> dict[str, float]:
        """Get a snapshot of the latest prices for BTC and ETH."""
        with self._lock:
            return dict(self._prices)

    @property
    def is_connected(self) -> bool:
        """Check if the WebSocket is currently connected."""
        with self._lock:
            return self._connected

    def get_btc_price(self) -> float:
        """Get the latest BTC price."""
        with self._lock:
            return self._prices.get("BTC", 0.0)

    def get_eth_price(self) -> float:
        """Get the latest ETH price."""
        with self._lock:
            return self._prices.get("ETH", 0.0)

    def get_period_open(self, symbol: str) -> float:
        """
        Get the opening price for the current 15-minute period.

        The "open" is the first trade price received in this 15-minute window.
        Returns 0.0 if no data yet.

        Args:
            symbol: "BTC" or "ETH".
        """
        with self._period_lock:
            return self._period_prices.get(symbol, {}).get("open", 0.0)

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Launch the WebSocket connection in a background daemon thread.

        This method returns immediately. The stream runs until stop() is called.
        """
        self._should_stop = False
        self._thread = threading.Thread(
            target=self._run_forever,
            name="BinanceStream",
            daemon=True,  # daemon so it doesn't block shutdown
        )
        self._thread.start()
        logger.info("BinanceStream background thread started")

    def stop(self) -> None:
        """
        Signal the stream to disconnect and the thread to exit.

        This is a graceful shutdown — it closes the WebSocket and waits
        for the thread to finish (with a 3-second timeout).
        """
        logger.info("BinanceStream stopping...")
        self._should_stop = True
        if self._ws:
            self._ws.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("BinanceStream stopped")

    # ──────────────────────────────────────────────────────────────────────
    # Internal — WebSocket event handlers
    # ──────────────────────────────────────────────────────────────────────

    def _on_message(self, _ws, message: str) -> None:
        """
        Handle an incoming WebSocket message from Binance.

        The message is a JSON object with trade data. We extract the symbol
        and price, update shared state, and log to the database.

        Args:
            _ws:     WebSocketApp instance (unused).
            message: Raw JSON string from Binance.
        """
        try:
            data = json.loads(message)
        except json.JSONDecodeError as exc:
            logger.warning("Binance WS: invalid JSON: %s", exc)
            return

        # Binance trade stream fields:
        # { "e": "trade", "s": "BTCUSDT", "p": "47123.50", ... }
        symbol_raw: str = data.get("s", "")
        price_str: str = data.get("p", "")

        if not symbol_raw or not price_str:
            return  # skip malformed messages

        # Map Binance symbol -> our short name
        symbol_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
        symbol = symbol_map.get(symbol_raw)
        if symbol is None:
            return  # not a symbol we track

        try:
            price = float(price_str)
        except ValueError:
            logger.warning("Binance WS: invalid price '%s'", price_str)
            return

        # Update latest prices (thread-safe)
        with self._lock:
            self._prices[symbol] = price
            self._connected = True

        # Update 15-minute period tracking
        self._update_period(symbol, price)

        # Log to database (fire-and-forget — don't block the WS thread)
        # We use a background insert to avoid slowing down the stream
        try:
            insert_price_tick(symbol, price)
        except Exception as exc:
            logger.error("Failed to log price tick: %s", exc)

    def _on_error(self, _ws, error) -> None:
        """Log WebSocket errors."""
        logger.error("Binance WS error: %s", error)
        with self._lock:
            self._connected = False

    def _on_close(self, _ws, close_status_code, close_msg) -> None:
        """Log WebSocket disconnection."""
        logger.warning(
            "Binance WS closed (code=%s, msg=%s)", close_status_code, close_msg
        )
        with self._lock:
            self._connected = False

    def _on_open(self, _ws) -> None:
        """Log successful connection."""
        logger.info("Binance WS connected — streaming BTC and ETH prices")
        with self._lock:
            self._connected = True

    # ──────────────────────────────────────────────────────────────────────
    # Internal — reconnection loop
    # ──────────────────────────────────────────────────────────────────────

    def _run_forever(self) -> None:
        """
        Run the WebSocket connection with automatic reconnection.

        If the connection drops, it waits and retries with exponential backoff
        (1s → 2s → 4s → … → max 60s).
        """
        retry_delay = 1  # seconds, doubles on each failure

        while not self._should_stop:
            try:
                logger.debug(
                    "Binance WS connecting to %s (retry_delay=%ds)",
                    _COMBINED_URL, retry_delay,
                )
                self._ws = websocket.WebSocketApp(
                    _COMBINED_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # Run the WebSocket (blocking — it loops internally)
                self._ws.run_forever(ping_interval=30, ping_timeout=10)

            except Exception as exc:
                logger.error("Binance WS exception: %s", exc)

            # If we get here, the connection dropped
            with self._lock:
                self._connected = False

            if self._should_stop:
                break

            # Exponential backoff
            logger.info(
                "Binance WS reconnecting in %ds...", retry_delay
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # cap at 60 seconds

        logger.info("Binance WS thread exiting")

    # ──────────────────────────────────────────────────────────────────────
    # Internal — 15-minute period tracking
    # ──────────────────────────────────────────────────────────────────────

    def _update_period(self, symbol: str, price: float) -> None:
        """
        Track the 15-minute opening price.

        Every 15 minutes, the "open" price resets to the first trade of the
        new period. This is used by Strategy A to compute implied probability.

        Args:
            symbol: "BTC" or "ETH".
            price:  Current trade price.
        """
        now = time.time()
        with self._period_lock:
            period = self._period_prices[symbol]
            # If 15 minutes have passed since last reset, reset the open price
            if now - period["last_check"] >= 900:  # 15 minutes = 900 seconds
                period["open"] = price
                period["last_check"] = now
                logger.debug(
                    "New 15m period for %s — open price: %.2f", symbol, price
                )
