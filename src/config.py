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

    # Trading Parameters
    order_size: float = 50.0
    order_type: str = "FOK"
    target_pair_cost: float = 0.94
    min_profit_margin: float = 0.005
    cooldown_seconds: float = 5.0

    # Strategy Toggles
    enable_arbitrage: bool = True
    enable_asymmetric: bool = False
    enable_price_lag: bool = False
    enable_maker_arbitrage: bool = False
    enable_multi_market: bool = True
    enable_parallel_strategies: bool = False  # A/B test mode: execute best opp from each strategy

    # Price-Lag Strategy Parameters
    spot_move_threshold: float = 0.001  # 0.10% minimum spot move to trigger (more sensitive)
    spot_window_seconds: int = 15  # window to measure spot movement
    odds_lag_threshold: float = 0.02  # min discrepancy between implied direction and PM odds (more sensitive)
    lag_entry_dead_zone_start: float = 45.0  # seconds after market open to wait (reduced)
    lag_entry_dead_zone_end: float = 20.0  # seconds before market close to stop (reduced)
    stop_loss_pct: float = 0.05  # 5% per-position stop loss
    take_profit_pct: float = 0.10  # 10% per-position take profit
    time_exit_seconds: float = 60.0  # force close N seconds before expiry
    lag_confirmations: int = 2  # N consecutive spot confirmations before acting

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

    # Risk Limits
    max_position_per_market: float = 500.0
    max_total_position: float = 2000.0
    max_daily_loss: float = 50.0
    max_unhedged_exposure: float = 100.0

    # Simulation
    dry_run: bool = False
    sim_balance: float = 1000.0

    # Monitoring
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
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
    db_path: str = "data/trades.db"

    # Daily Summary
    daily_summary_hour: int = 0  # UTC hour to send daily summary
