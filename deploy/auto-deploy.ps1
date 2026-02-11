# 15MinuteProfitable - Fully Automated Deployment Script
# Run from: C:\Projects\15MinuteProfitable

param(
    [Parameter(Mandatory=$true)]
    [string]$ServerIP,

    [string]$User = "root",
    [string]$RemotePath = "/opt/15minuteprofitable"
)

$ErrorActionPreference = "Stop"

Write-Host "=== 15MinuteProfitable Automated Deployment ===" -ForegroundColor Cyan
Write-Host "Server: $User@$ServerIP" -ForegroundColor Yellow
Write-Host ""

# Step 1: Create exclude list for scp
$excludeDirs = @(".venv", ".git", "__pycache__", "*.pyc", ".env", "data", "*.pid", ".mypy_cache", ".pytest_cache", ".ruff_cache")

Write-Host "[1/5] Preparing files for upload..." -ForegroundColor Green

# Step 2: Create remote directory and upload files
Write-Host "[2/5] Uploading code to server..." -ForegroundColor Green

# Use scp to upload (excluding large/sensitive files)
$sourcePath = (Get-Location).Path
scp -r "$sourcePath/src" "$sourcePath/tests" "$sourcePath/deploy" "$sourcePath/pyproject.toml" "$sourcePath/README.md" "${User}@${ServerIP}:${RemotePath}/" 2>$null

if ($LASTEXITCODE -ne 0) {
    # Directory might not exist, create it first
    Write-Host "Creating remote directory..." -ForegroundColor Yellow
    ssh "${User}@${ServerIP}" "mkdir -p ${RemotePath}"
    scp -r "$sourcePath/src" "$sourcePath/tests" "$sourcePath/deploy" "$sourcePath/pyproject.toml" "${User}@${ServerIP}:${RemotePath}/"
}

Write-Host "[3/5] Running server setup..." -ForegroundColor Green

# Step 3: Run setup commands on server
$setupScript = @'
#!/bin/bash
set -e

echo "=== Server Setup Starting ==="

# Install dependencies
apt update
apt install -y python3.11 python3.11-venv python3-pip curl

# Setup bot directory
cd /opt/15minuteprofitable

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install bot
pip install --upgrade pip
pip install -e .

# Create directories
mkdir -p /var/log/15minuteprofitable
mkdir -p /opt/15minuteprofitable/data

# Create .env template if not exists
if [ ! -f ".env" ]; then
cat > .env << 'ENVEOF'
# === REQUIRED: Edit these values ===
BOT_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
BOT_SIGNATURE_TYPE=1
BOT_FUNDER=0xYOUR_PROXY_WALLET_ADDRESS

# === Trading Config ===
BOT_DRY_RUN=true
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
ENVEOF
chmod 600 .env
fi

# Install systemd service
cp deploy/15minuteprofitable.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable 15minuteprofitable

# Setup firewall
ufw allow 22/tcp
ufw allow 8080/tcp
ufw --force enable

echo "=== Server Setup Complete ==="
'@

# Execute setup on server
$setupScript | ssh "${User}@${ServerIP}" "cat > /tmp/setup.sh && chmod +x /tmp/setup.sh && /tmp/setup.sh"

Write-Host "[4/5] Configuring environment..." -ForegroundColor Green

# Step 4: Copy .env values (prompt user)
Write-Host ""
Write-Host "=== Environment Configuration ===" -ForegroundColor Cyan
Write-Host "I need your credentials to configure the bot." -ForegroundColor Yellow
Write-Host ""

$privateKey = Read-Host "Enter BOT_PRIVATE_KEY (starts with 0x)"
$funder = Read-Host "Enter BOT_FUNDER (proxy wallet address)"
$dryRun = Read-Host "Enable DRY_RUN mode? (true/false) [default: true]"

if ([string]::IsNullOrEmpty($dryRun)) { $dryRun = "true" }

# Update .env on server
$envUpdate = @"
sed -i 's|BOT_PRIVATE_KEY=.*|BOT_PRIVATE_KEY=$privateKey|' /opt/15minuteprofitable/.env
sed -i 's|BOT_FUNDER=.*|BOT_FUNDER=$funder|' /opt/15minuteprofitable/.env
sed -i 's|BOT_DRY_RUN=.*|BOT_DRY_RUN=$dryRun|' /opt/15minuteprofitable/.env
"@

$envUpdate | ssh "${User}@${ServerIP}" "bash"

Write-Host "[5/5] Starting bot..." -ForegroundColor Green

# Step 5: Start the bot
ssh "${User}@${ServerIP}" "systemctl start 15minuteprofitable && sleep 3 && systemctl status 15minuteprofitable --no-pager"

Write-Host ""
Write-Host "=== Deployment Complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "Dashboard URL: http://${ServerIP}:8080" -ForegroundColor Cyan
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Yellow
Write-Host "  ssh ${User}@${ServerIP} 'journalctl -u 15minuteprofitable -f'  # View logs"
Write-Host "  ssh ${User}@${ServerIP} 'systemctl restart 15minuteprofitable' # Restart"
Write-Host "  ssh ${User}@${ServerIP} 'systemctl stop 15minuteprofitable'    # Stop"
Write-Host ""
