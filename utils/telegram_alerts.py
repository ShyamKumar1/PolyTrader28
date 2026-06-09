"""
telegram_alerts.py — Telegram Notification Integration
========================================================
Sends alerts to a Telegram chat for important events:
  - Trade executed (entry / exit)
  - Stop-loss triggered
  - Daily drawdown limit reached
  - Daily performance summary
  - Bot started / stopped

Uses the Telegram Bot API (HTTP). No third-party library required beyond
the 'requests' package already in requirements.txt.

Usage:
    from utils.telegram_alerts import send_alert
    send_alert("🚨 Stop-loss triggered on BTC 15m Up")
"""

from typing import Optional
import requests

from config import config
from utils.logger import logger


# ---------------------------------------------------------------------------
# Telegram API constants
# ---------------------------------------------------------------------------
_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_alert(message: str, disable_notification: bool = False) -> bool:
    """
    Send a text message to the configured Telegram chat.

    This is a fire-and-forget function — failures are logged but not raised,
    so a Telegram outage never blocks the bot's main loop.

    Args:
        message:             The message text (max 4096 characters).
        disable_notification: If True, sends silently (no sound on the user's
                              phone). Default False.

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    # Skip if Telegram is not configured
    if not config.telegram_enabled:
        return False

    url = _TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_notification": disable_notification,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.debug("Telegram alert sent: %.60s", message)
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to send Telegram alert: %s", exc)
        return False


def send_trade_alert(
    action: str,           # "ENTER" or "EXIT"
    market: str,
    side: str,
    price: float,
    quantity: int,
    profit_usdc: Optional[float] = None,
) -> None:
    """
    Send a formatted alert for a trade execution.

    Args:
        action:      "ENTER" or "EXIT".
        market:      Market name (e.g. "BTC 15m Up").
        side:        "YES" or "NO".
        price:       Execution price per contract.
        quantity:    Number of contracts.
        profit_usdc: Realised profit (only for EXIT).
    """
    if action == "ENTER":
        emoji = "🟢"
        text = (
            f"{emoji} <b>TRADE ENTRY</b>\n"
            f"Market: {market}\n"
            f"Side: {side}\n"
            f"Price: ${price:.4f}\n"
            f"Qty: {quantity} contracts\n"
            f"Total: ${price * quantity:.2f}"
        )
    else:
        emoji = "✅" if (profit_usdc or 0) >= 0 else "🔴"
        text = (
            f"{emoji} <b>TRADE EXIT</b>\n"
            f"Market: {market}\n"
            f"Side: {side}\n"
            f"Exit Price: ${price:.4f}\n"
            f"Qty: {quantity} contracts\n"
            f"P&L: <b>${profit_usdc:+.2f}</b>" if profit_usdc is not None else ""
        )

    send_alert(text)


def send_stop_loss_alert(market: str, loss_usdc: float, loss_pct: float) -> None:
    """
    Send an urgent alert when a stop-loss is triggered.

    Args:
        market:    Market name.
        loss_usdc: Loss amount in USDC.
        loss_pct:  Loss percentage.
    """
    text = (
        f"🚨 <b>STOP-LOSS TRIGGERED</b>\n"
        f"Market: {market}\n"
        f"Loss: ${loss_usdc:.2f} ({loss_pct:.1f}%)\n"
        f"Action: Position closed immediately."
    )
    send_alert(text, disable_notification=False)


def send_drawdown_alert(current_bankroll: float, peak_bankroll: float) -> None:
    """
    Send an alert when the daily drawdown limit is reached.

    Args:
        current_bankroll: Current USDC balance.
        peak_bankroll:    Today's peak USDC balance.
    """
    drawdown_pct = (1 - current_bankroll / peak_bankroll) * 100
    text = (
        f"⚠️ <b>DAILY DRAWDOWN LIMIT REACHED</b>\n"
        f"Current: ${current_bankroll:.2f}\n"
        f"Peak: ${peak_bankroll:.2f}\n"
        f"Drawdown: {drawdown_pct:.1f}%\n"
        f"Trading halted until manual restart."
    )
    send_alert(text, disable_notification=False)


def send_daily_summary(
    total_trades: int,
    wins: int,
    losses: int,
    win_rate: float,
    daily_pnl: float,
    bankroll: float,
) -> None:
    """
    Send an end-of-day performance summary.

    Args:
        total_trades: Total completed trades today.
        wins:         Winning trades today.
        losses:       Losing trades today.
        win_rate:     Win rate as a percentage (e.g. 97.5).
        daily_pnl:    Today's P&L in USDC.
        bankroll:     Current total bankroll.
    """
    emoji = "📈" if daily_pnl >= 0 else "📉"
    text = (
        f"{emoji} <b>DAILY SUMMARY</b>\n"
        f"Trades: {total_trades} ({wins}W / {losses}L)\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Day P&L: <b>${daily_pnl:+.2f}</b>\n"
        f"Bankroll: <b>${bankroll:.2f}</b>\n"
    )
    send_alert(text)


def send_startup_alert() -> None:
    """Send a notification that the bot has started."""
    text = (
        f"🤖 <b>PolyTrader28 Started</b>\n"
        f"Mode: {'LIVE' if config.is_live else 'DRY RUN'}\n"
        f"Max Positions: {config.MAX_CONCURRENT_POSITIONS}\n"
        f"Min Edge: {config.MIN_EDGE_THRESHOLD}%\n"
    )
    send_alert(text)
