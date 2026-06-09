# PolyTrader28 — Deployment Guide

**Polymarket 15-Minute BTC & ETH Arbitrage Bot**  
*Grow ₹500 (~$6 USDC) to millions through strict compounding and risk management*

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Step-by-Step Setup](#2-step-by-step-setup)
   - [2.1 Install Python & Git](#21-install-python--git)
   - [2.2 Download the Bot](#22-download-the-bot)
   - [2.3 Install Dependencies](#23-install-dependencies)
   - [2.4 Set Up a Polygon Wallet](#24-set-up-a-polygon-wallet)
   - [2.5 Fund Your Wallet with USDC](#25-fund-your-wallet-with-usdc)
   - [2.6 Create a Polymarket Account & API Credentials](#26-create-a-polymarket-account--api-credentials)
   - [2.7 Generate Binance API Keys](#27-generate-binance-api-keys)
   - [2.8 Configure the .env File](#28-configure-the-env-file)
3. [Running the Bot](#3-running-the-bot)
   - [3.1 Dry-Run / Paper Trading (Recommended First)](#31-dry-run--paper-trading-recommended-first)
   - [3.2 Live Trading](#32-live-trading)
4. [Web Dashboard](#4-web-dashboard)
   - [4.1 Accessing the Dashboard](#41-accessing-the-dashboard)
   - [4.2 Dashboard Controls](#42-dashboard-controls)
   - [4.3 Securing the Dashboard (Remote Access)](#43-securing-the-dashboard-remote-access)
5. [24/7 Operation on a VPS](#5-247-operation-on-a-vps)
   - [5.1 Recommended VPS](#51-recommended-vps)
   - [5.2 systemd Service Setup](#52-systemd-service-setup)
   - [5.3 Nginx Reverse Proxy (Optional)](#53-nginx-reverse-proxy-optional)
6. [Monitoring](#6-monitoring)
   - [6.1 Checking Logs](#61-checking-logs)
   - [6.2 Telegram Alerts](#62-telegram-alerts)
   - [6.3 Health Check Output](#63-health-check-output)
7. [Backtesting](#7-backtesting)
8. [Emergency Stop Procedure](#8-emergency-stop-procedure)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites

Before starting, make sure you have:

- **A computer or VPS** running Linux (Ubuntu 22.04+), macOS, or Windows (WSL2)
- **Python 3.10 or higher** installed
- **Git** installed
- **Internet connection** (the bot streams prices from Binance and trades on Polymarket)
- **~₹500 worth of USDC** on the Polygon network (approx. $6 USD)
- **A smartphone** (optional, for Telegram alerts)

---

## 2. Step-by-Step Setup

### 2.1 Install Python & Git

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
python3 --version  # should show Python 3.10+
```

**macOS:**
```bash
brew install python3 git
python3 --version
```

**Windows (WSL2):**
```bash
# Install Ubuntu from Microsoft Store, then run:
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

### 2.2 Download the Bot

```bash
# Clone or download the project
cd ~
git clone <your-repo-url> PolyTrader28
# OR if you have the files manually:
# cd ~/PolyTrader28

cd PolyTrader28
```

### 2.3 Install Dependencies

It's recommended to use a Python virtual environment:

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate  # On Linux/macOS
# .\venv\Scripts\activate  # On Windows (cmd.exe)

# Install all required packages
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note**: The bot uses `py-clob-client-v2` (Polymarket's current CLOB V2 SDK).
> All EIP-712 order signing is handled automatically by the SDK — no manual
> signature generation needed. API credentials are derived from your wallet
> private key on first run.

### 2.4 Set Up a Polygon Wallet

You need a Polygon wallet to trade on Polymarket.

**Option A: MetaMask (Recommended for beginners)**

1. Install the [MetaMask browser extension](https://metamask.io/download/) (Chrome/Firefox/Brave)
2. Create a new wallet — **write down your 12-word seed phrase on paper**. Never type it into any website.
3. Add the Polygon network to MetaMask:
   - Open MetaMask → Settings → Networks → Add Network
   - **Network Name**: Polygon Mainnet
   - **RPC URL**: `https://polygon-rpc.com`
   - **Chain ID**: `137`
   - **Symbol**: `MATIC`
   - **Block Explorer**: `https://polygonscan.com`
4. Your wallet address starts with `0x...`. Copy it.

**Option B: Use an existing wallet**  
If you already have a Polygon wallet (e.g., from CoinDCX or WazirX), you can use it. You'll need the private key.

> **⚠️ SECURITY WARNING**: Your private key controls your funds. Never share it. The bot stores it in `.env` (which is in `.gitignore`). Keep your computer secure.

### 2.5 Fund Your Wallet with USDC

This is the most important step — you need USDC on the **Polygon network** (not Ethereum mainnet, which has high fees).

**From an Indian exchange (WazirX / CoinDCX):**

**WazirX:**
1. Log in to WazirX → Funds → USDT → Withdraw
2. Withdraw USDT to your exchange wallet (choose Polygon/ MATIC network)
3. On an exchange like Binance or KuCoin, swap USDT → USDC
4. Withdraw USDC to your MetaMask wallet address on the **Polygon network**

**CoinDCX:**
1. Log in to CoinDCX → Portfolio → USDC → Withdraw
2. Select **Polygon** as the withdrawal network
3. Enter your MetaMask wallet address (starts with `0x...`)
4. Withdraw the USDC (minimum depends on CoinDCX)

**Direct Purchase (if you have a credit card):**
1. Use [Transak](https://global.transak.com/) or [MoonPay](https://www.moonpay.com/) within MetaMask
2. Buy USDC directly on the Polygon network
3. You'll need a small amount of MATIC (~$0.50) for gas fees

> **Important**: Make sure you have at least **0.01 MATIC (POL)** in your wallet for transaction fees. You can buy POL from the same exchanges.

**Note on pUSD**: Polymarket now uses **pUSD** (Polymarket USD) as its collateral token.
When you deposit USDC on Polygon, it's automatically converted to pUSD. You don't need
to do anything — just send USDC to your Polymarket deposit address on the Polygon network.

**Minimum Initial Capital: ~₹500 ≈ $6 USDC**

### 2.6 Create a Polymarket Account & API Credentials

**Step 1: Create a Polymarket account**

1. Go to [https://polymarket.com](https://polymarket.com)
2. Click "Connect Wallet" → Select MetaMask
3. Sign the signature request
4. Complete any onboarding steps

**Step 2: API Credentials (Auto-Derived)**

The V2 SDK automatically derives API credentials from your wallet's private key.
You do NOT need to manually generate them from the Polymarket website.

On your **first live run**, the bot will:
1. Connect to Polymarket using your private key
2. Derive (or create) API credentials automatically
3. Print the derived credentials to the log

**Save the printed credentials** (API Key, Secret, Passphrase) in case you need
them later. The SDK caches them internally, so you won't see them on subsequent runs.

> **Note**: If you prefer to generate credentials manually, go to
> [https://polymarket.com/profile/api](https://polymarket.com/profile/api),
> click "Create API Key", and sign the MetaMask transaction. Then add the
> values to your `.env` file (optional — the SDK doesn't require them).

### 2.7 Generate Binance API Keys

The bot uses Binance only for **price data** (read-only). No trading occurs on Binance.

1. Log in to [Binance](https://www.binance.com)
2. Go to API Management: Profile → API Management
3. Click **"Create API"**
4. Label it "PolyTrader28 Price Feed"
5. **IMPORTANT**: Under "Restrictions", check ONLY:
   - ☑ Enable Reading (no trading or withdrawal permissions)
6. Complete security verification
7. Copy the **API Key** and **Secret Key**

### 2.8 Configure the .env File

```bash
# From the PolyTrader28 directory
cp .env.example .env
```

Now edit `.env` with your values:

```bash
nano .env   # or use any text editor
```

Fill in every field:

```ini
# Polygon wallet (from step 2.4) — REQUIRED for live trading
WALLET_PRIVATE_KEY="0x..."  # Your wallet private key
WALLET_ADDRESS="0x..."      # Your wallet address

# Polymarket API credentials — auto-derived by V2 SDK on first live run
# You can leave these blank; the SDK derives them from your private key
POLYMARKET_API_KEY=""
POLYMARKET_SECRET=""
POLYMARKET_PASSPHRASE=""

# Initial capital
INITIAL_CAPITAL_USDC=6.0    # ≈ ₹500

# Binance API (from step 2.7)
BINANCE_API_KEY="your-binance-api-key"
BINANCE_API_SECRET="your-binance-secret"

# Telegram (optional - skip if you don't want alerts)
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""

# Dashboard security (optional - only if exposing remotely)
DASHBOARD_API_KEY=""
```

> **Keep your `.env` file safe!** It contains your private keys. Never commit it to Git.

---

## 3. Running the Bot

### 3.1 Dry-Run / Paper Trading (Recommended First)

The default mode is `dry_run` — the bot runs all logic but **does not place real orders**.

```bash
# Activate virtual environment (if not already)
source venv/bin/activate

# Run the bot in dry-run mode
python polymarket_bot.py
```

You'll see output like:
```
15:30:00 | INFO     | PolyTrader28 — Initialising...
15:30:00 | INFO     | Mode: DRY RUN
15:30:01 | INFO     | Database initialised at data/polytrader.db
15:30:01 | INFO     | BinanceStream background thread started
15:30:01 | INFO     | Dashboard starting on http://0.0.0.0:8050
15:30:05 | INFO     | Binance WS connected — streaming BTC and ETH prices
...
```

Let it run for at least 15 minutes to observe opportunities being detected.

**What happens in dry-run mode:**
- Binance price feeds stream in real-time
- Polymarket markets are scanned every 500ms
- Arbitrage opportunities are detected and logged
- "Orders" are simulated (logged to DB but not sent to Polymarket)
- Dashboard updates every second
- All data is stored in the SQLite database for analysis

### 3.2 Live Trading

When you're confident the bot works correctly:

1. Stop the dry-run bot (Ctrl+C)
2. Edit `.env` and change: `TRADING_MODE="live"`
3. Run:
```bash
python polymarket_bot.py --live
```

Or with the CLI flag (overrides .env):
```bash
python polymarket_bot.py --live
```

> **⚠️ BEFORE GOING LIVE**: Run the backtest first (see Section 7). Start with dry-run for at least 24 hours. Monitor the dashboard for opportunity detection. Only switch to live when you're satisfied with the dry-run results.

---

## 4. Web Dashboard

### 4.1 Accessing the Dashboard

The dashboard automatically starts on the same machine as the bot:

- **Local access**: Open `http://localhost:8050` in your browser
- **Same network**: Open `http://<YOUR_IP>:8050` (e.g., `http://192.168.1.100:8050`)

**What you'll see:**
- **Top bar**: Bankroll (green), today's P&L, win rate, bot status indicator
- **Active Positions**: Current open trades with P&L
- **Controls**: STOP, START, PAUSE, RESUME buttons
- **Recent Trades**: Last 20 trades with win/loss badges
- **Equity Curve**: Line chart showing bankroll over time (with 1D/7D/30D toggles)

The dashboard auto-refreshes every 1 second.

### 4.2 Dashboard Controls

| Button | Action | API Endpoint |
|--------|--------|-------------|
| **STOP** | Stops the bot, closes all positions | `POST /api/stop` |
| **START** | Resumes trading (if paused) | `POST /api/start` |
| **PAUSE** | Pauses new trades, monitors current positions | `POST /api/pause` |
| **RESUME** | Resumes trading after pause | `POST /api/resume` |

> Note: The "S T O P" button triggers a confirmation dialog. After stopping, the bot will exit and must be restarted manually.

### 4.3 Securing the Dashboard (Remote Access)

If you want to access the dashboard from outside your local network:

**Option 1: API Key Authentication (Built-in)**

1. Set a strong password in `.env`:
   ```
   DASHBOARD_API_KEY="your-strong-random-password-here"
   ```
2. Restart the bot
3. The control endpoints (`/api/stop`, `/api/start`) now require:
   - Header: `X-API-Key: your-strong-random-password-here`
   - Or query parameter: `?api_key=your-strong-random-password-here`
4. The dashboard page will prompt for the key when you click STOP/START

**Option 2: Nginx Reverse Proxy with Password (Recommended for VPS)**

See Section 5.3 below.

---

## 5. 24/7 Operation on a VPS

For the bot to trade continuously, run it on a VPS (Virtual Private Server).

### 5.1 Recommended VPS

| Provider | Plan | Cost | Reason |
|----------|------|------|--------|
| **AWS Lightsail** | $5/month | ~₹415/mo | Reliable, easy setup |
| **DigitalOcean** | Basic ($6) | ~₹500/mo | Good documentation |
| **Linode** | Nanode 1GB | $5/mo | Same price |
| **Hetzner** | CX22 | €3.99/mo | Cheapest reliable option |

**Requirements:**
- 1 GB RAM (minimum)
- 1 vCPU
- 20 GB SSD
- Ubuntu 22.04 LTS

### 5.2 systemd Service Setup

This keeps the bot running even if you log out, and auto-restarts it if it crashes.

**Step 1: Install the bot on the VPS**

```bash
# SSH into your VPS
ssh ubuntu@<your-vps-ip>

# Install dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# Clone the repository
git clone <your-repo-url> PolyTrader28
cd PolyTrader28

# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Upload your .env file (scp from local machine or create with nano)
nano .env  # Paste your configuration
```

**Step 2: Create the systemd service**

```bash
sudo nano /etc/systemd/system/polytrader.service
```

Paste the following (adjust `YOUR_USERNAME` and paths):

```ini
[Unit]
Description=PolyTrader28 — Polymarket Arbitrage Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/PolyTrader28
ExecStart=/home/YOUR_USERNAME/PolyTrader28/venv/bin/python polymarket_bot.py
Restart=always
RestartSec=10
StandardOutput=append:/home/YOUR_USERNAME/PolyTrader28/logs/daemon.log
StandardError=append:/home/YOUR_USERNAME/PolyTrader28/logs/daemon.log

# Security hardening
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=full
PrivateDevices=true

[Install]
WantedBy=multi-user.target
```

**Step 3: Enable and start the service**

```bash
sudo systemctl daemon-reload
sudo systemctl enable polytrader
sudo systemctl start polytrader

# Check status
sudo systemctl status polytrader
# Should show "active (running)"
```

**Step 4: Useful commands**

```bash
sudo systemctl status polytrader      # Check if running
sudo systemctl stop polytrader        # Stop the bot
sudo systemctl start polytrader       # Start the bot
sudo systemctl restart polytrader     # Restart
sudo journalctl -u polytrader -f      # Watch live logs
```

### 5.3 Nginx Reverse Proxy (Optional)

If you want to access the dashboard securely from outside:

```bash
sudo apt install -y nginx apache2-utils
```

Create a password:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd traderview
# Enter a strong password
```

Create the Nginx config:

```bash
sudo nano /etc/nginx/sites-available/polytrader
```

```nginx
server {
    listen 80;
    server_name your-vps-ip;  # or your domain name

    location / {
        proxy_pass http://127.0.0.1:8050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Password protection
        auth_basic "PolyTrader28 Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd;
    }

    location /api/status {
        # Allow unauthenticated access to status (read-only)
        proxy_pass http://127.0.0.1:8050;
    }

    location /api/trades {
        proxy_pass http://127.0.0.1:8050;
    }
}
```

Enable and restart:

```bash
sudo ln -s /etc/nginx/sites-available/polytrader /etc/nginx/sites-enabled/
sudo nginx -t    # Test config
sudo systemctl restart nginx
```

Now access: `http://your-vps-ip/` — you'll be prompted for the username/password.

> **💡 Tip**: For real security, use a domain with Let's Encrypt SSL (HTTPS). Install certbot:
> ```bash
> sudo apt install -y certbot python3-certbot-nginx
> sudo certbot --nginx -d your-domain.com
> ```

---

## 6. Monitoring

### 6.1 Checking Logs

The bot logs everything to two places:

**Console output** (if running in terminal):
```text
15:30:00 | INFO     | PolyTrader28 — Initialising...
```

**Rotating log files** (in `logs/` directory):
```bash
tail -f logs/polytrader.log        # Live view
tail -100 logs/polytrader.log      # Last 100 lines
grep ERROR logs/polytrader.log     # Show only errors
```

If running as a systemd service:
```bash
sudo journalctl -u polytrader -n 100 -f
```

### 6.2 Telegram Alerts

The bot sends alerts to your Telegram if configured:

**Setup:**
1. Open Telegram, search for `@BotFather`
2. Send `/newbot` and follow instructions
3. You'll receive a **bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Search for your bot's username and click "Start"
5. Search for `@userinfobot` and send `/start` — it will give you your **chat ID**
6. Add both values to your `.env`:
   ```
   TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
   TELEGRAM_CHAT_ID="123456789"
   ```

**Alerts you'll receive:**
- 🤖 Bot started / stopped
- 🟢 Trade entry notifications
- ✅ Trade exit with profit
- 🔴 Trade exit with loss
- 🚨 Stop-loss triggered
- ⚠️ Daily drawdown limit reached
- 📈 Daily performance summary
- 🤖 Health check every 60 seconds

### 6.3 Health Check Output

Every 60 seconds, the bot prints a health summary:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HEALTH CHECK
  Uptime:         2h 15m
  Mode:           DRY RUN
  Bankroll:       $6.00 USDC
  Open Positions: 0/3
  Total Trades:   0
  Win Rate:       0.0%
  Total P&L:      $0.00
  Drawdown:       0.00%
  BTC Price:      $67123.50
  ETH Price:      $3456.80
  Markets Tracked: 8
  Paused:         NO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 7. Backtesting

Before trading live, run the backtester to see how the strategies would have performed historically:

```bash
# Activate virtual environment
source venv/bin/activate

# Backtest BTC for the last 90 days
python backtest.py --symbol BTC --days 90 --plot

# Backtest ETH
python backtest.py --symbol ETH --days 90 --plot

# Backtest both
python backtest.py --all --days 30
```

**What the backtester does:**
1. Downloads 1-minute OHLCV data from Binance
2. Simulates 15-minute Polymarket contracts with realistic pricing
3. Applies both strategies (price-lag and complete-set)
4. Outputs summary stats and charts

**Sample output:**
```
============================================================
  BACKTEST SUMMARY — BTC
============================================================
  Period:       2025-02-17 → 2025-05-18
  Initial:      $6.00
  Final:        $8.47
  Total Return: +41.17%
  Total P&L:    $2.47
  Trades:       142
  Wins:         138
  Losses:       4
  Win Rate:     97.18%
  Max DD:       8.50%
  Sharpe:       2.34
============================================================
```

Charts are saved to `backtest_results/` as PNG files.

> **Note**: Backtest results are **not a guarantee** of future performance. Polymarket market conditions, liquidity, and latency differ from simulations. Always start with dry-run first.

---

## 8. Emergency Stop Procedure

If you need to stop the bot **immediately**:

### If running in terminal:
Press **Ctrl+C** twice. The bot will close all open positions and shut down.

### If running as a systemd service:
```bash
sudo systemctl stop polytrader
```

### If you have the dashboard open:
1. Click the **STOP** button
2. Confirm the dialog
3. The bot will close all positions and exit

### If you have Telegram alerts:
Send yourself a message — the bot will stop on its own if the daily drawdown limit is breached (default: 15%). But for immediate stop:

**Kill command directly:**
```bash
# Find the process
ps aux | grep polymarket_bot

# Kill it
kill -SIGTERM <PID>
# Or force kill if necessary:
kill -9 <PID>
```

### Manual trade close on Polymarket:
If the bot stops unexpectedly and leaves open positions:
1. Go to [https://polymarket.com/portfolio](https://polymarket.com/portfolio)
2. Find your open positions
3. Click "Sell" to close them manually

---

## 9. Troubleshooting

### "No module named ..."
```bash
# Make sure you've activated the virtual environment
source venv/bin/activate
pip install -r requirements.txt
```

### "WebSocket connection failed"
- Check your internet connection
- Binance may be blocked in your region — use a VPN
- Check if `wss://stream.binance.com:9443` is reachable:
  ```bash
  curl -v https://api.binance.com/api/v3/ping
  ```

### "Polymarket API returned 401"
- Your wallet private key may be incorrect — verify it in MetaMask or your wallet
- The V2 SDK derives API credentials automatically on first run. Check the logs
  for "Created new API credentials" or "Derived existing API credentials"
- If credentials were revoked, delete the `data/polytrader.db` and restart
  (the SDK will re-derive credentials)

### "py-clob-client-v2 not installed"
- Run: `pip install py-clob-client-v2`
- Make sure you're in the virtual environment: `source venv/bin/activate`

### "No 15-minute markets found"
- Polymarket may not have active 15-minute contracts right now (they appear only during trading hours)
- Try running the bot at a different time (market hours)
- Verify by visiting polymarket.com and searching for "15-minute"

### "Bot stopped due to drawdown limit"
- The daily drawdown limit was reached (default: 15%).
- To restart, adjust the bot or wait for the next day
- Check the logs to understand why losses occurred

### "Dashboard not loading"
- Ensure the bot is running
- Check `http://localhost:8050` (not port 80)
- If on a VPS, check the firewall:
  ```bash
  sudo ufw status
  sudo ufw allow 8050/tcp  # if needed
  ```

### "Not enough POL (MATIC) for gas"
- Polymarket transactions on Polygon require a small amount of POL (~0.001-0.01 per trade)
- Send at least 0.01 POL to your wallet:
  ```bash
  # You can buy POL from Binance and withdraw to your Polygon wallet
  ```

### Logs show "Rate limited" or "429"
- The bot implements exponential backoff, so it will recover
- This is normal during high-frequency polling
- If it persists, the bot automatically slows down requests

---

## Final Notes

- **Start small**: Use dry-run for at least 24 hours before going live
- **Monitor regularly**: Check the dashboard and logs daily
- **Compound patiently**: The strategy relies on compounding small gains — don't withdraw profits early
- **Update dependencies**: Run `pip install -r requirements.txt --upgrade` periodically
- **Backup your .env**: Store a copy of your `.env` file securely (password manager)
- **Not financial advice**: This is experimental software. Only risk what you can afford to lose.

---

**Good luck! 🚀**  
*— PolyTrader28 Team*
