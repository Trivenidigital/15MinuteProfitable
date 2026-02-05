#!/bin/bash
# BTC15MinuteBot - Hetzner Amsterdam Setup Script
# Run as root on a fresh Ubuntu 22.04 VPS

set -e

echo "=== BTC15MinuteBot Hetzner Setup ==="

# 1. System updates
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip git curl

# 2. Create bot user
useradd -r -m -d /opt/btc15minutebot -s /bin/bash botuser || true

# 3. Clone repository (replace with your repo URL)
cd /opt
if [ ! -d "btc15minutebot/.git" ]; then
    sudo -u botuser git clone https://github.com/YOUR_USERNAME/BTC15MinuteBot.git btc15minutebot
fi
cd btc15minutebot

# 4. Create virtual environment and install
sudo -u botuser python3.11 -m venv .venv
sudo -u botuser .venv/bin/pip install --upgrade pip
sudo -u botuser .venv/bin/pip install -e .

# 5. Create .env file (EDIT THIS!)
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# === REQUIRED: Edit these values ===
BOT_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
BOT_SIGNATURE_TYPE=1
BOT_FUNDER=0xYOUR_PROXY_WALLET_ADDRESS

# === Trading Config ===
BOT_DRY_RUN=false
BOT_ORDER_SIZE=50.0
BOT_TARGET_PAIR_COST=0.94
BOT_ENABLE_ARBITRAGE=true
BOT_ENABLE_MULTI_MARKET=true
BOT_MARKETS=BTC,ETH,SOL,XRP

# === Dashboard ===
BOT_DASHBOARD_ENABLED=true
BOT_DASHBOARD_HOST=0.0.0.0
BOT_DASHBOARD_PORT=8080

# === Logging ===
BOT_LOG_FORMAT=json
BOT_LOG_LEVEL=INFO

# === Optional: Alerts ===
# BOT_TELEGRAM_BOT_TOKEN=
# BOT_TELEGRAM_CHAT_ID=
# BOT_DISCORD_WEBHOOK_URL=
EOF
    chown botuser:botuser .env
    chmod 600 .env
    echo ">>> IMPORTANT: Edit /opt/btc15minutebot/.env with your keys!"
fi

# 6. Create log directory
mkdir -p /var/log/btc15minutebot
chown botuser:botuser /var/log/btc15minutebot

# 7. Create data directory for SQLite
sudo -u botuser mkdir -p /opt/btc15minutebot/data
chown botuser:botuser /opt/btc15minutebot/data

# 8. Install systemd service
cp deploy/btc15minutebot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable btc15minutebot

# 9. Install logrotate
cp deploy/logrotate.conf /etc/logrotate.d/btc15minutebot

# 10. Setup firewall (allow SSH + dashboard)
ufw allow 22/tcp
ufw allow 8080/tcp
ufw --force enable

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit /opt/btc15minutebot/.env with your keys"
echo "2. Start: sudo systemctl start btc15minutebot"
echo "3. View logs: sudo journalctl -u btc15minutebot -f"
echo "4. Dashboard: http://YOUR_SERVER_IP:8080"
echo ""
echo "Commands:"
echo "  sudo systemctl status btc15minutebot  # Check status"
echo "  sudo systemctl restart btc15minutebot # Restart"
echo "  sudo systemctl stop btc15minutebot    # Stop"
