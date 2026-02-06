from enum import IntEnum

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class SignatureType(IntEnum):
    EOA = 0
    POLY_GNOSIS_SAFE = 1
    GNOSIS_SAFE = 2


class Settings(BaseSettings):
    model_config = {"env_prefix": "BOT_", "env_file": ".env"}

    # Wallet & Auth
    private_key: SecretStr
    signature_type: SignatureType = SignatureType.POLY_GNOSIS_SAFE
    funder: str = ""

    # API Endpoints
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

    # Trading Parameters (AGGRESSIVE MODE)
    order_size: float = 250.0  # 5x increase from $50
    order_type: str = "FOK"
    target_pair_cost: float = 0.94
    min_profit_margin: float = 0.003  # lowered from 0.005
    cooldown_seconds: float = 2.0  # faster cycling

    # Strategy Toggles
    enable_arbitrage: bool = True
    enable_asymmetric: bool = False
    enable_price_lag: bool = False
    enable_maker_arbitrage: bool = False
    enable_multi_market: bool = True
    enable_parallel_strategies: bool = False  # A/B test mode: execute best opp from each strategy

    # Price-Lag Strategy Parameters (AGGRESSIVE MODE)
    spot_move_threshold: float = 0.0005  # 0.05% - ultra sensitive to spot moves
    spot_window_seconds: int = 10  # shorter window for faster signals
    odds_lag_threshold: float = 0.01  # 1% discrepancy triggers trade
    lag_entry_dead_zone_start: float = 30.0  # trade earlier after open
    lag_entry_dead_zone_end: float = 15.0  # trade later before close
    stop_loss_pct: float = 0.08  # 8% stop loss (wider to avoid whipsaws)
    take_profit_pct: float = 0.15  # 15% take profit (let winners run)
    time_exit_seconds: float = 45.0  # exit 45s before expiry
    lag_confirmations: int = 1  # react immediately, no confirmation wait

    # Asymmetric Entry Strategy Parameters
    yes_cheap_threshold: float = 0.42  # buy YES when ask < this
    no_cheap_threshold: float = 0.42  # buy NO when ask < this
    accumulation_size: float = 10.0  # shares per accumulation buy
    max_accumulation_per_side: float = 200.0  # max shares before completing pair
    target_avg_combined: float = 0.90  # target avg combined cost for profit
    stale_order_seconds: float = 120.0  # cancel GTC orders older than this

    # Maker Arbitrage Strategy Parameters
    maker_target_pair_cost: float = 0.985  # aggressive threshold (higher fill rate)
    maker_price_offset: float = 0.01  # place limit below best ask (more aggressive)
    maker_pair_timeout_seconds: float = 180.0  # cancel if not filled in 3 min
    maker_max_pending_pairs: int = 5  # max concurrent arb attempts
    maker_min_profit_margin: float = 0.002  # 0.2% min profit per share (lower bar)

    # Markets (only assets with 15-min up/down markets on Polymarket)
    markets: list[str] = ["BTC", "ETH", "SOL", "XRP"]
    market_intervals: list[str] = ["15m"]  # Future: add "1h", "4h" for hourly markets
    market_slug_override: str = ""

    # Risk Limits (AGGRESSIVE MODE)
    max_position_per_market: float = 1000.0  # 2x increase
    max_total_position: float = 5000.0  # 2.5x increase
    max_daily_loss: float = 250.0  # 5x increase (matches position size)
    max_unhedged_exposure: float = 500.0  # 5x increase for directional trades

    # Simulation
    dry_run: bool = False
    sim_balance: float = 1000.0

    # Monitoring
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    discord_webhook_url: SecretStr = SecretStr("")
    alert_on_trade: bool = True
    alert_on_error: bool = True

    # Operational
    log_level: str = "INFO"
    log_format: str = "json"
    neg_risk: bool = True

    # Risk Sizing
    kelly_fraction: float = 0.25

    # Process Management
    pid_lock_path: str = "btc15minutebot.pid"
    state_snapshot_path: str = "state_snapshot.json"

    # Dashboard
    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_username: str = "admin"
    dashboard_password: SecretStr = SecretStr("")  # empty = auth disabled
    db_path: str = "data/trades.db"

    # Daily Summary
    daily_summary_hour: int = 0  # UTC hour to send daily summary
