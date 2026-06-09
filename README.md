# PolyTrader28

> Polymarket 15-minute BTC & ETH Arbitrage Bot

A Python trading bot that exploits pricing discrepancies between Binance (real-time spot prices) and Polymarket (15-minute prediction market contracts). Runs in dry-run (paper trading) or live mode.

---

## Architecture

```
PolyTrader28/
├── polymarket_bot.py      # Main orchestrator — main loop, lifecycle, signal handling
├── config.py              # Configuration loaded from .env (singleton)
├── models.py              # SQLite database layer (trades, equity, ticks, opportunities)
├── dashboard.py           # Flask web dashboard + REST API
├── backtest.py            # Historical backtesting engine with charts
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── DEPLOYMENT_GUIDE.md    # Step-by-step setup instructions
│
├── data/
│   ├── binance_stream.py  # WebSocket client for live BTC/ETH prices
│   └── polymarket_api.py  # Polymarket CLOB V2 + Gamma API client
│
├── strategy/
│   ├── price_lag.py       # Strategy A: Time arbitrage (Binance vs Polymarket lag)
│   └── complete_set.py    # Strategy B: Risk-free YES+NO complete-set arbitrage
│
├── execution/
│   ├── order_manager.py   # Order placement, fill monitoring, position management
│   └── risk_manager.py    # Position sizing, stop-loss, drawdown limits, compounding
│
└── utils/
    ├── logger.py          # Rotating file + console logger
    └── telegram_alerts.py # Telegram bot notifications
```

---

## Strategies

### Strategy A — Price-Lag (Time Arbitrage)

Polymarket's 15-minute BTC/ETH contracts lag behind real-time Binance prices by 30-90 seconds.

**How it works:**
1. Stream live BTC/ETH prices from Binance WebSocket
2. Track the opening price of each 15-minute period
3. Compute implied probability using a logistic function: `P(UP) = 1 / (1 + exp(-k × price_change))`
4. Compare implied probability to Polymarket's YES price
5. When the discrepancy exceeds the configured edge threshold (default 3%), enter a directional trade

**Edge calculation:**
- If `implied_prob > market_yes_price` → buy YES (market underestimates UP)
- If `implied_prob < market_yes_price` → buy NO (market overestimates UP)

### Strategy B — Complete-Set Arbitrage (Risk-Free)

When YES + NO prices for the same contract sum to less than $1.00, buying both sides guarantees profit.

**How it works:**
1. Scan all active 15-minute markets every 500ms
2. When `YES_price + NO_price ≤ 0.985`, buy both sides simultaneously
3. Regardless of outcome, payout is $1.00 per pair → risk-free profit

**Example:** YES = $0.48, NO = $0.48 → sum = $0.96 → profit = $0.04 per pair (4.17%)

---

## Risk Management

| Rule | Default | Description |
|---|---|---|
| Max position per trade | 5% of bankroll | Never risk more than 5% on a single trade |
| Max concurrent positions | 3 | Limits total exposure |
| Stop-loss per trade | -15% | Hard-coded minimum, never overridden |
| Daily drawdown limit | 15% | Halts all trading if breached |
| Compounding | Every 10 wins | Recalculates position size based on new bankroll |
| Leverage | None | Spot positions only |

---

## Data Pipeline

| Source | Type | Purpose |
|---|---|---|
| Binance WebSocket (`wss://stream.binance.com`) | Real-time trade stream | BTC/ETH live prices for Strategy A |
| Polymarket Gamma API (`gamma-api.polymarket.com`) | REST (public) | Market discovery, metadata, token IDs |
| Polymarket CLOB V2 API (`clob.polymarket.com`) | REST + SDK (authenticated) | Order books, order placement, account data |
| SQLite (`data/polytrader.db`) | Local database | Trade history, equity snapshots, price ticks, opportunities |

---

## API Endpoints (Dashboard)

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web dashboard (auto-refreshes every 1s) |
| `/api/status` | GET | Bot status: bankroll, positions, win rate, uptime |
| `/api/trades?limit=50` | GET | Recent trade history |
| `/api/equity?range=7d` | GET | Equity curve data (1d, 7d, 30d, all) |
| `/api/opportunities?limit=50` | GET | Detected opportunities (executed + missed) |
| `/api/strategy-stats` | GET | Per-strategy breakdown (trades, win rate, P&L) |
| `/api/today-stats` | GET | Today's performance summary |
| `/api/stop` | POST | Stop the bot (closes all positions) |
| `/api/start` | POST | Resume trading |
| `/api/pause` | POST | Pause new trades (monitors existing) |
| `/api/resume` | POST | Resume after pause |

---

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your values
```

### 3. Run in dry-run mode (no money needed)

```bash
python polymarket_bot.py
```

Dashboard: `http://localhost:8050`

### 4. Run live (requires funded wallet)

```bash
python polymarket_bot.py --live
```

---

## Backtesting

```bash
python backtest.py --symbol BTC --days 90 --plot
python backtest.py --symbol ETH --days 30
python backtest.py --all --days 30
```

Charts saved to `backtest_results/`.

---

## India-Specific Notes

### ISP Block
Most Indian ISPs block `polymarket.com` and `gamma-api.polymarket.com` at the DNS level (since March 2026). The CLOB API (`clob.polymarket.com`) may also be affected.

**Fix:** Change DNS to Cloudflare (`1.1.1.1`) or Google (`8.8.8.8`):
```bash
networksetup -setdnsservers Wi-Fi 1.1.1.1 8.8.8.8
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

### Funding from India
1. Buy USDC on CoinDCX/WazirX via UPI
2. Withdraw USDC on **Polygon network** (not Ethereum) to your Polymarket wallet
3. Polymarket auto-converts USDC → pUSD (collateral token)

### Tax
- 30% VDA tax on crypto gains (Income Tax Act §115BBH)
- 1% TDS on crypto transactions over ₹10,000 (§194S)
- Prediction markets are in a legal gray area — no specific regulation yet

---

## CLOB V2 Migration

This project uses `py-clob-client-v2` (Polymarket's current SDK). The legacy `py-clob-client` (V1) was deprecated on April 28, 2026.

**Key V2 changes:**
- EIP-712 signing handled automatically by the SDK
- API credentials derived from wallet private key on first run
- Collateral token changed from USDC → pUSD
- Order fields: removed `nonce`, `feeRateBps`, `taker`; added `timestamp`, `metadata`, `builder`

---

## Known Issues

- **Polymarket API blocked in India**: Requires DNS change (see above)
- **15-minute market availability**: Polymarket may not always have active 15-minute BTC/ETH contracts — they appear during active trading hours
- **Dry-run only until funded**: Live trading requires a Polygon wallet with USDC/pUSD

---

## License

Private project. Not for redistribution.
