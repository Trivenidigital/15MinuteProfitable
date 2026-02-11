# Deployment Guide

## Prerequisites
- Ubuntu 22.04+ VPS with Python 3.11+
- systemd
- A funded Polymarket account with API key

## Setup

```bash
# Create bot user
sudo useradd -r -m -d /opt/15minuteprofitable botuser

# Clone and install
sudo -u botuser git clone <repo-url> /opt/15minuteprofitable
cd /opt/15minuteprofitable
sudo -u botuser python3 -m venv .venv
sudo -u botuser .venv/bin/pip install -e .

# Configure environment
sudo -u botuser cp .env.example .env
sudo -u botuser nano .env  # Set BOT_PRIVATE_KEY, BOT_FUNDER, etc.
chmod 600 /opt/15minuteprofitable/.env

# Create log directory
sudo mkdir -p /var/log/15minuteprofitable
sudo chown botuser:botuser /var/log/15minuteprofitable

# Install systemd service
sudo cp deploy/15minuteprofitable.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable 15minuteprofitable

# Install logrotate config
sudo cp deploy/logrotate.conf /etc/logrotate.d/15minuteprofitable
```

## Operations

```bash
# Start / stop / restart
sudo systemctl start 15minuteprofitable
sudo systemctl stop 15minuteprofitable
sudo systemctl restart 15minuteprofitable

# View status and logs
sudo systemctl status 15minuteprofitable
sudo journalctl -u 15minuteprofitable -f
tail -f /var/log/15minuteprofitable/bot.log

# Dry-run mode
# Set BOT_DRY_RUN=true in .env, then restart
```

## Monitoring

Configure Telegram or Discord alerts in `.env`:

```
BOT_TELEGRAM_BOT_TOKEN=your_bot_token
BOT_TELEGRAM_CHAT_ID=your_chat_id
BOT_DISCORD_WEBHOOK_URL=your_webhook_url
```
