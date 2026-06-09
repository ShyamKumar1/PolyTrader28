"""
config.py — PolyTrader28 Configuration Module
==============================================
Loads all configuration from environment variables (.env file).
Validates required values and provides typed access to all settings.

Usage:
    from config import config
    print(config.INITIAL_CAPITAL_USDC)   # -> 6.0
    print(config.TRADING_MODE)           # -> "dry_run"
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Load .env file from the project root directory
# ---------------------------------------------------------------------------
# This looks for a .env file in the same directory as this config.py
_project_root = Path(__file__).resolve().parent
_dotenv_path = _project_root / ".env"

if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)
else:
    # No .env file — warn but don't crash. The user may set env vars manually.
    print(
        "WARNING: No .env file found at", _dotenv_path,
        "\nCreate one by copying .env.example and filling in your values.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Helper: get a required string env var or exit with a helpful message
# ---------------------------------------------------------------------------
def _require_env(key: str, hint: str = "") -> str:
    """Return the value of *key* from the environment, or exit if missing."""
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        msg = f"FATAL: Required environment variable '{key}' is not set."
        if hint:
            msg += f"\n       {hint}"
        print(msg, file=sys.stderr)
        sys.exit(1)
    return value.strip()


# ---------------------------------------------------------------------------
# Configuration dataclass — all settings in one place
# ---------------------------------------------------------------------------
class Config:
    """
    Central configuration object.
    All values are loaded once at import time and should be treated as read-only.
    """

    # ──────────────────────────────────────────────────────────────────────
    # Polymarket API credentials (V2 SDK auto-derives these from private key)
    # ──────────────────────────────────────────────────────────────────────
    # The V2 SDK derives API key/secret/passphrase automatically on first run
    # using the wallet's private key. These env vars are kept for reference
    # but are NOT required — the SDK handles credential derivation internally.
    POLYMARKET_API_KEY: str = os.environ.get("POLYMARKET_API_KEY", "")
    """Polymarket API key — auto-derived by V2 SDK from wallet private key."""

    POLYMARKET_SECRET: str = os.environ.get("POLYMARKET_SECRET", "")
    """Polymarket API secret — auto-derived by V2 SDK."""

    POLYMARKET_PASSPHRASE: str = os.environ.get("POLYMARKET_PASSPHRASE", "")
    """Polymarket API passphrase — auto-derived by V2 SDK."""

    # ──────────────────────────────────────────────────────────────────────
    # Polygon wallet
    # ──────────────────────────────────────────────────────────────────────
    WALLET_PRIVATE_KEY: str = os.environ.get("WALLET_PRIVATE_KEY", "")
    """Polygon wallet private key (starts with 0x...)"""

    WALLET_ADDRESS: str = os.environ.get("WALLET_ADDRESS", "")
    """Polygon wallet address"""

    # ──────────────────────────────────────────────────────────────────────
    # Capital
    # ──────────────────────────────────────────────────────────────────────
    INITIAL_CAPITAL_USDC: float = float(
        os.environ.get("INITIAL_CAPITAL_USDC", "6.0")
    )
    """
    Estimated starting capital in USDC.  Default 6.0 (≈ ₹500 INR).
    The bot fetches the actual on-chain balance, so this is only a fallback.
    """

    # ──────────────────────────────────────────────────────────────────────
    # Binance API (read-only)
    # ──────────────────────────────────────────────────────────────────────
    BINANCE_API_KEY: str = os.environ.get("BINANCE_API_KEY", "")
    """Binance API key (market-data only permissions)"""

    BINANCE_API_SECRET: str = os.environ.get("BINANCE_API_SECRET", "")
    """Binance API secret"""

    # ──────────────────────────────────────────────────────────────────────
    # Trading mode
    # ──────────────────────────────────────────────────────────────────────
    TRADING_MODE: str = os.environ.get("TRADING_MODE", "dry_run").lower()
    """
    'live'   — real orders on Polymarket mainnet.
    'dry_run'— simulated execution, no real funds moved (default).
    """

    # ──────────────────────────────────────────────────────────────────────
    # Risk management
    # ──────────────────────────────────────────────────────────────────────
    MAX_POSITION_PCT: float = float(os.environ.get("MAX_POSITION_PCT", "5.0"))
    """Maximum % of bankroll allocated to any single trade (default 5%)."""

    MAX_CONCURRENT_POSITIONS: int = int(
        os.environ.get("MAX_CONCURRENT_POSITIONS", "3")
    )
    """Maximum number of open positions at the same time (default 3)."""

    STOP_LOSS_PCT: float = float(os.environ.get("STOP_LOSS_PCT", "-15.0"))
    """
    Stop-loss threshold per trade as a negative %.
    Default -15% (hard-coded minimum from spec).
    """

    DAILY_DRAWDOWN_LIMIT: float = float(
        os.environ.get("DAILY_DRAWDOWN_LIMIT", "15.0")
    )
    """
    Maximum allowed daily drawdown as a positive %.
    If bankroll drops by this % in a single day, trading halts.
    """

    # ──────────────────────────────────────────────────────────────────────
    # Strategy thresholds
    # ──────────────────────────────────────────────────────────────────────
    MIN_EDGE_THRESHOLD: float = float(
        os.environ.get("MIN_EDGE_THRESHOLD", "3.0")
    )
    """
    Minimum edge (in %) for Strategy A to enter a trade.
    Configurable range 3.0 – 5.0.
    """

    COMPLETE_SET_THRESHOLD: float = float(
        os.environ.get("COMPLETE_SET_THRESHOLD", "0.985")
    )
    """
    Strategy B entry threshold: enter when YES + NO price ≤ this value.
    Default 0.985 (i.e. 1.5% risk-free return).
    """

    # ──────────────────────────────────────────────────────────────────────
    # Telegram alerts
    # ──────────────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    """Telegram bot token from @BotFather. Leave blank to disable alerts."""

    TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    """Telegram chat ID for sending alerts."""

    # ──────────────────────────────────────────────────────────────────────
    # Dashboard
    # ──────────────────────────────────────────────────────────────────────
    DASHBOARD_API_KEY: str = os.environ.get("DASHBOARD_API_KEY", "")
    """
    Optional API key for dashboard control endpoints (/api/stop, /api/start).
    If empty, the endpoints are unprotected (only use on localhost).
    """

    # ──────────────────────────────────────────────────────────────────────
    # Proxy (for Polymarket in blocked regions)
    # ──────────────────────────────────────────────────────────────────────
    POLYMARKET_PROXY: str = os.environ.get("POLYMARKET_PROXY", "")
    """
    Optional proxy URL for Polymarket API calls only.
    Useful when Polymarket is ISP-blocked (e.g. India).
    Example: http://127.0.0.1:8080 or socks5://127.0.0.1:1080
    Falls back to HTTP_PROXY / HTTPS_PROXY env vars if not set.
    """

    # ──────────────────────────────────────────────────────────────────────
    # Grid Trading
    # ──────────────────────────────────────────────────────────────────────
    GRID_ENABLED: bool = os.environ.get("GRID_ENABLED", "true").lower() == "true"
    """Enable grid trading on Binance (default: true)."""

    GRID_SYMBOL: str = os.environ.get("GRID_SYMBOL", "BTC")
    """Asset to grid trade: 'BTC' or 'ETH' (default: BTC)."""

    GRID_RANGE_PCT: float = float(os.environ.get("GRID_RANGE_PCT", "10.0"))
    """Total grid range as % of center price. ±5% = 10% total (default: 10)."""

    GRID_COUNT: int = int(os.environ.get("GRID_COUNT", "10"))
    """Number of grid levels (default: 10). More = finer grid = more trades."""

    GRID_INVESTMENT_PCT: float = float(os.environ.get("GRID_INVESTMENT_PCT", "80.0"))
    """% of bankroll allocated to the grid (default: 80%)."""

    GRID_REBALANCE_INTERVAL: int = int(os.environ.get("GRID_REBALANCE_INTERVAL", "300"))
    """Seconds between grid rebalance checks (default: 300 = 5 min)."""

    # ──────────────────────────────────────────────────────────────────────
    # Derived / computed properties
    # ──────────────────────────────────────────────────────────────────────
    @property
    def is_dry_run(self) -> bool:
        """True when running in paper-trading / simulation mode."""
        return self.TRADING_MODE == "dry_run"

    @property
    def is_live(self) -> bool:
        """True when running live on mainnet."""
        return self.TRADING_MODE == "live"

    @property
    def telegram_enabled(self) -> bool:
        """True when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def dashboard_auth_enabled(self) -> bool:
        """True when DASHBOARD_API_KEY is set, enabling auth on control endpoints."""
        return bool(self.DASHBOARD_API_KEY)


# ---------------------------------------------------------------------------
# Singleton instance — import this everywhere
# ---------------------------------------------------------------------------
config: Config = Config()
"""Project-wide configuration singleton. Import with: from config import config"""
