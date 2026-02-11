#!/bin/bash
# 15MinuteProfitable - Hetzner Amsterdam Setup Script
# Run as root on a fresh Ubuntu 22.04 VPS

set -e

echo "=== 15MinuteProfitable Hetzner Setup ==="

# 1. System updates
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip git curl

# 2. Create bot user
useradd -r -m -d /opt/15minuteprofitable -s /bin/bash botuser || true

# 3. Clone repository (replace with your repo URL)
cd /opt
if [ ! -d "15minuteprofitable/.git" ]; then
    sudo -u botuser git clone https://github.com/YOUR_USERNAME/15MinuteProfitable.git 15minuteprofitable
fi
cd 15minuteprofitable

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
    echo ">>> IMPORTANT: Edit /opt/15minuteprofitable/.env with your keys!"
fi

# 6. Create log directory
mkdir -p /var/log/15minuteprofitable
chown botuser:botuser /var/log/15minuteprofitable

# 7. Create data directory for SQLite
sudo -u botuser mkdir -p /opt/15minuteprofitable/data
chown botuser:botuser /opt/15minuteprofitable/data

# 8. Install systemd service
cp deploy/15minuteprofitable.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable 15minuteprofitable

# 9. Install logrotate
cp deploy/logrotate.conf /etc/logrotate.d/15minuteprofitable

# 10. Setup firewall (allow SSH + dashboard)
ufw allow 22/tcp
ufw allow 8080/tcp
ufw --force enable

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit /opt/15minuteprofitable/.env with your keys"
echo "2. Start: sudo systemctl start 15minuteprofitable"
echo "3. View logs: sudo journalctl -u 15minuteprofitable -f"
echo "4. Dashboard: http://YOUR_SERVER_IP:8080"
echo ""
echo "Commands:"
echo "  sudo systemctl status 15minuteprofitable  # Check status"
echo "  sudo systemctl restart 15minuteprofitable # Restart"
echo "  sudo systemctl stop 15minuteprofitable    # Stop"
