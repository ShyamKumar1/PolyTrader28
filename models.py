"""
models.py — PolyTrader28 Database Models
=========================================
Manages the SQLite database that stores ticks, trades, equity snapshots,
and market data.  Provides simple CRUD methods used by every other module.

Database file location:  data/polytrader.db  (in the project root)

Tables:
  - trades:          Every executed trade (real or simulated).
  - equity_snapshots: Bankroll snapshots taken every 5 minutes.
  - price_ticks:     Raw price ticks from Binance (for audit / backtest).
  - opportunities:   Detected arbitrage opportunities (for analysis).
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Database file path
# ---------------------------------------------------------------------------
DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "polytrader.db"

# Ensure the data directory exists
DB_DIR.mkdir(parents=True, exist_ok=True)

# Thread-local storage for connections (each thread gets its own connection)
_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """
    Get a thread-local SQLite connection.
    SQLite connections cannot be shared across threads safely.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row  # allows accessing columns by name
        conn.execute("PRAGMA journal_mode=WAL;")       # better concurrent reads
        conn.execute("PRAGMA foreign_keys=ON;")        # enforce FK constraints
        _local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Create all tables if they don't exist.
    Safe to call multiple times — uses IF NOT EXISTS.
    """
    conn = _get_connection()
    cursor = conn.cursor()

    # --- Trades table -----------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,  -- ISO 8601 UTC
            market          TEXT    NOT NULL,  -- e.g. "BTC 15m Up"
            side            TEXT    NOT NULL,  -- "YES" or "NO"
            strategy        TEXT    NOT NULL,  -- "price_lag" or "complete_set"
            entry_price     REAL    NOT NULL,
            exit_price      REAL,
            quantity        INTEGER NOT NULL,  -- number of contracts
            profit_usdc     REAL,
            win             INTEGER,           -- 1 = won, 0 = lost, NULL = open
            entry_order_id  TEXT,
            exit_order_id   TEXT,
            is_dry_run      INTEGER NOT NULL DEFAULT 1,  -- 1 = simulated
            notes           TEXT
        );
    """)

    # --- Equity snapshots table -------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,  -- ISO 8601 UTC
            bankroll_usdc   REAL    NOT NULL
        );
    """)

    # --- Price ticks table ------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_ticks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,  -- ISO 8601 UTC
            symbol          TEXT    NOT NULL,  -- "BTC" or "ETH"
            price           REAL    NOT NULL
        );
    """)

    # --- Opportunities table ----------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,  -- ISO 8601 UTC
            market          TEXT    NOT NULL,  -- e.g. "BTC 15m Up"
            strategy        TEXT    NOT NULL,  -- "price_lag" or "complete_set"
            edge_pct        REAL,              -- detected edge percentage
            yes_price       REAL,
            no_price        REAL,
            executed        INTEGER NOT NULL DEFAULT 0,  -- 1 = trade placed
            reason          TEXT               -- why executed or skipped
        );
    """)

    # Indexes for fast queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_timestamp   ON trades(timestamp);
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON equity_snapshots(timestamp);
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time   ON price_ticks(symbol, timestamp);
    """)

    conn.commit()


# ---------------------------------------------------------------------------
# Trade CRUD
# ---------------------------------------------------------------------------

def insert_trade(
    market: str,
    side: str,
    strategy: str,
    entry_price: float,
    quantity: int,
    entry_order_id: str = "",
    is_dry_run: bool = True,
    notes: str = "",
) -> int:
    """
    Record a new trade in the database.

    Args:
        market:      Market name, e.g. "BTC 15m Up".
        side:        "YES" or "NO".
        strategy:    "price_lag" or "complete_set".
        entry_price: Price paid per contract in USDC.
        quantity:    Number of contracts bought.
        entry_order_id: Polymarket order ID (or empty for dry-run).
        is_dry_run:  True if simulated.
        notes:       Optional notes.

    Returns:
        The auto-generated trade ID.
    """
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades
            (timestamp, market, side, strategy, entry_price, quantity,
             entry_order_id, is_dry_run, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, market, side, strategy, entry_price, quantity,
          entry_order_id, 1 if is_dry_run else 0, notes))
    conn.commit()
    # lastrowid is guaranteed non-None after a successful INSERT with commit
    assert cursor.lastrowid is not None, "INSERT failed to generate a row ID"
    return cursor.lastrowid


def close_trade(
    trade_id: int,
    exit_price: float,
    profit_usdc: float,
    win: bool,
    exit_order_id: str = "",
) -> None:
    """
    Close an existing trade, recording the outcome.

    Args:
        trade_id:     The trade's database ID.
        exit_price:   Price at which the position was exited.
        profit_usdc:  Profit or loss in USDC.
        win:          True if the trade was profitable.
        exit_order_id: Polymarket order ID for the exit.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE trades
        SET exit_price = ?,
            profit_usdc = ?,
            win = ?,
            exit_order_id = ?
        WHERE id = ?
    """, (exit_price, profit_usdc, 1 if win else 0, exit_order_id, trade_id))
    conn.commit()


def get_open_trades() -> list:
    """
    Return all trades that haven't been closed yet.

    Returns:
        List of sqlite3.Row objects.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM trades WHERE exit_price IS NULL ORDER BY timestamp DESC
    """)
    return cursor.fetchall()


def get_recent_trades(limit: int = 50) -> list:
    """
    Return the most recent *limit* trades.

    Args:
        limit: Maximum number of trades to return (default 50).

    Returns:
        List of sqlite3.Row objects.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    return cursor.fetchall()


def get_trade_stats() -> dict:
    """
    Compute aggregate trade statistics.

    Returns:
        Dictionary with keys: total_trades, wins, losses, win_rate, total_profit.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*)                                         AS total_trades,
            COALESCE(SUM(win), 0)                            AS wins,
            COUNT(*) - COALESCE(SUM(win), 0)                 AS losses,
            ROUND(100.0 * COALESCE(SUM(win), 0) / NULLIF(COUNT(*), 0), 2)
                                                             AS win_rate,
            COALESCE(SUM(profit_usdc), 0.0)                  AS total_profit
        FROM trades WHERE exit_price IS NOT NULL
    """)
    row = cursor.fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Equity snapshots
# ---------------------------------------------------------------------------

def insert_equity_snapshot(bankroll_usdc: float) -> None:
    """
    Record a bankroll snapshot.

    Args:
        bankroll_usdc: Current USDC balance.
    """
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO equity_snapshots (timestamp, bankroll_usdc)
        VALUES (?, ?)
    """, (now, bankroll_usdc))
    conn.commit()


def get_equity_snapshots(range_days: int = 7) -> list:
    """
    Return equity snapshots from the last *range_days* days.

    Args:
        range_days: Number of days of history to return (default 7).

    Returns:
        List of sqlite3.Row objects sorted chronologically.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    # Filter using ISO timestamps — subtract N days from now
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=range_days)).isoformat()
    cursor.execute("""
        SELECT * FROM equity_snapshots
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
    """, (cutoff,))
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Price ticks
# ---------------------------------------------------------------------------

def insert_price_tick(symbol: str, price: float) -> None:
    """
    Log a Binance price tick.

    Args:
        symbol: "BTC" or "ETH".
        price:  Latest trade price.
    """
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO price_ticks (timestamp, symbol, price)
        VALUES (?, ?, ?)
    """, (now, symbol, price))
    conn.commit()


def get_recent_ticks(symbol: str, limit: int = 100) -> list:
    """
    Return the most recent price ticks for a symbol.

    Args:
        symbol: "BTC" or "ETH".
        limit:  Maximum ticks to return.

    Returns:
        List of sqlite3.Row objects.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM price_ticks
        WHERE symbol = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (symbol, limit))
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def insert_opportunity(
    market: str,
    strategy: str,
    edge_pct: Optional[float],
    yes_price: Optional[float],
    no_price: Optional[float],
    executed: bool = False,
    reason: str = "",
) -> None:
    """
    Log a detected arbitrage opportunity.

    Args:
        market:    Market name.
        strategy:  "price_lag" or "complete_set".
        edge_pct:  Detected edge percentage (None if N/A).
        yes_price: Current YES price.
        no_price:  Current NO price.
        executed:  Whether a trade was actually placed.
        reason:    Why the opportunity was taken or skipped.
    """
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO opportunities
            (timestamp, market, strategy, edge_pct, yes_price, no_price,
             executed, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, market, strategy, edge_pct, yes_price, no_price,
          1 if executed else 0, reason))
    conn.commit()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def get_current_bankroll() -> float:
    """
    Estimate the current bankroll from trade history.

    This takes the initial capital (from config) plus realized P&L from all
    closed trades.  For dry-run mode this is the authoritative figure.

    Returns:
        Estimated USDC balance.
    """
    from config import config as cfg
    conn = _get_connection()
    cursor = conn.cursor()

    # Start with initial capital
    bankroll = cfg.INITIAL_CAPITAL_USDC

    # Add realized profit from closed trades.
    # COALESCE ensures SUM never returns NULL even if there are no rows.
    cursor.execute("SELECT COALESCE(SUM(profit_usdc), 0.0) FROM trades WHERE exit_price IS NOT NULL")
    row = cursor.fetchone()
    # row is guaranteed non-None for an aggregate query; row[0] is guaranteed
    # non-None because of COALESCE, but we guard for the type checker.
    if row is not None and row[0] is not None:
        bankroll += row[0]

    return bankroll


# ---------------------------------------------------------------------------
# Strategy-level stats
# ---------------------------------------------------------------------------

def get_strategy_stats() -> dict:
    """
    Compute per-strategy trade statistics.

    Returns:
        Dict with keys "price_lag" and "complete_set", each containing:
            total_trades, wins, losses, win_rate, total_profit.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            strategy,
            COUNT(*)                                         AS total_trades,
            COALESCE(SUM(win), 0)                            AS wins,
            COUNT(*) - COALESCE(SUM(win), 0)                 AS losses,
            ROUND(100.0 * COALESCE(SUM(win), 0) / NULLIF(COUNT(*), 0), 2)
                                                             AS win_rate,
            COALESCE(SUM(profit_usdc), 0.0)                  AS total_profit
        FROM trades WHERE exit_price IS NOT NULL
        GROUP BY strategy
    """)
    result = {"price_lag": {}, "complete_set": {}}
    for row in cursor.fetchall():
        result[row["strategy"]] = dict(row)
    return result


def get_opportunities(limit: int = 50) -> list:
    """
    Return the most recent detected opportunities.

    Args:
        limit: Maximum opportunities to return.

    Returns:
        List of sqlite3.Row objects.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM opportunities ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    return cursor.fetchall()


def get_today_stats() -> dict:
    """
    Compute today's trade statistics.

    Returns:
        Dict with today's total_trades, wins, losses, win_rate, daily_pnl.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    cursor.execute("""
        SELECT
            COUNT(*)                                         AS total_trades,
            COALESCE(SUM(win), 0)                            AS wins,
            COUNT(*) - COALESCE(SUM(win), 0)                 AS losses,
            ROUND(100.0 * COALESCE(SUM(win), 0) / NULLIF(COUNT(*), 0), 2)
                                                             AS win_rate,
            COALESCE(SUM(profit_usdc), 0.0)                  AS daily_pnl
        FROM trades
        WHERE exit_price IS NOT NULL AND timestamp >= ?
    """, (today_start,))
    row = cursor.fetchone()
    return dict(row) if row else {}
