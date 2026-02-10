from __future__ import annotations

import os
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from src.utils.vault import SecretVault, VaultError


class SignatureType(IntEnum):
    EOA = 0
    POLY_GNOSIS_SAFE = 1
    GNOSIS_SAFE = 2


# ---------------------------------------------------------------------------
# Vault settings source for Pydantic
# ---------------------------------------------------------------------------

# Fields that live in the vault (vault key -> settings field name).
# Vault keys are lowercase, matching the Settings field names.
_VAULT_SECRET_FIELDS: dict[str, str] = {
    "private_key": "private_key",
    "telegram_bot_token": "telegram_bot_token",
    "discord_webhook_url": "discord_webhook_url",
    "dashboard_password": "dashboard_password",
    "funder": "funder",
    "binance_futures_api_key": "binance_futures_api_key",
    "binance_futures_api_secret": "binance_futures_api_secret",
}


class VaultSettingsSource(PydanticBaseSettingsSource):
    """Load secret fields from an encrypted vault file.

    Activated only when ``VAULT_PASSWORD`` env var is set **and** the vault
    file exists.  Otherwise returns an empty dict (transparent fallback to
    ``.env``).
    """

    def get_field_value(self, field: ..., field_name: str) -> tuple[..., str, bool]:  # type: ignore[override]
        # Not used — we override __call__ directly.
        return None, field_name, False  # type: ignore[return-value]

    def __call__(self) -> dict[str, SecretStr | str]:
        vault_password = os.environ.get("VAULT_PASSWORD", "")
        if not vault_password:
            return {}

        vault_path = Path(os.environ.get("VAULT_PATH", "data/secrets.vault"))
        if not vault_path.is_file():
            return {}

        try:
            vault = SecretVault(vault_path, vault_password)
            secrets = vault.load()
        except VaultError:
            # Vault exists but can't be read — fall back to .env silently
            return {}

        result: dict[str, SecretStr | str] = {}
        for vault_key, field_name in _VAULT_SECRET_FIELDS.items():
            value = secrets.get(vault_key, "")
            if value:
                result[field_name] = value

        return result


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = {"env_prefix": "BOT_", "env_file": ".env"}

    # Vault secret fields (keys that can be stored in the vault)
    VAULT_SECRET_FIELDS: ClassVar[dict[str, str]] = _VAULT_SECRET_FIELDS

    # Wallet & Auth
    private_key: SecretStr
    signature_type: SignatureType = SignatureType.POLY_GNOSIS_SAFE
    funder: str = ""

    # API Endpoints
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

    # Trading Parameters (ANALYSIS MODE — small sizes to observe all strategies)
    order_size: float = 10.0  # reduced — price_lag is a losing strategy
    order_type: str = "FOK"
    target_pair_cost: float = 0.94
    min_profit_margin: float = 0.003  # lowered from 0.005
    cooldown_seconds: float = 2.0  # faster cycling

    # Strategy Toggles
    enable_arbitrage: bool = True
    enable_asymmetric: bool = True
    enable_price_lag: bool = True
    enable_maker_arbitrage: bool = True
    enable_multi_market: bool = True
    enable_parallel_strategies: bool = True  # A/B test mode: execute best opp from each strategy

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
    stop_loss_time_decay: bool = True  # widen stop-loss as market nears expiry
    take_profit_time_decay: bool = True  # widen take-profit as market nears expiry
    stop_loss_cheap_threshold: float = 0.10  # no stop-loss for contracts < $0.10 avg price
    stop_loss_confirmations: int = 3  # consecutive triggers before exit (6s at 2s interval)

    # TA Momentum Filter (for price-lag)
    lag_enable_ta_filter: bool = True        # Master toggle for TA filter
    lag_ema_short_periods: int = 10          # Fast EMA periods
    lag_ema_long_periods: int = 30           # Slow EMA periods
    lag_rsi_periods: int = 14               # RSI lookback
    lag_rsi_overbought: float = 80.0        # RSI above this blocks UP signals
    lag_rsi_oversold: float = 20.0          # RSI below this blocks DOWN signals

    # Asymmetric Entry Strategy Parameters
    yes_cheap_threshold: float = 0.35  # buy YES when ask < this (tightened from 0.42)
    no_cheap_threshold: float = 0.35  # buy NO when ask < this (tightened from 0.42)
    accumulation_size: float = 10.0  # reduced — losing strategy
    max_accumulation_per_side: float = 50.0  # reduced — cap unhedged risk
    target_avg_combined: float = 0.90  # target avg combined cost for profit
    stale_order_seconds: float = 120.0  # cancel GTC orders older than this
    asymmetric_require_hedge: bool = True  # only enter when both sides are cheap

    # Maker Arbitrage Strategy Parameters
    maker_target_pair_cost: float = 0.97  # tighter threshold to absorb slippage (was 0.985)
    maker_price_offset: float = 0.01  # place limit below best ask (more aggressive)
    maker_pair_timeout_seconds: float = 180.0  # cancel if not filled in 3 min
    maker_max_pending_pairs: int = 5  # max concurrent arb attempts
    maker_min_profit_margin: float = 0.002  # 0.2% min profit per share (lower bar)
    maker_max_combined_fill_cost: float = 0.995  # post-fill hard ceiling on YES+NO combined

    # Dip Buyer / Mean Reversion Strategy Parameters
    enable_dip_buyer: bool = True
    dip_spot_window_seconds: int = 5        # Short window for detecting sharp moves
    dip_spot_threshold: float = 0.0015      # 0.15% move in dip window to trigger
    dip_mean_window_seconds: int = 60       # Longer window for mean comparison
    dip_outlier_ratio: float = 2.0          # Move must be 2x the rolling avg move
    dip_order_size: float = 25.0            # Small size for analysis
    dip_stop_loss_pct: float = 0.05         # 5% stop-loss
    dip_take_profit_pct: float = 0.08       # 8% take-profit
    dip_time_exit_seconds: float = 60.0     # Exit 60s before close

    # Fade Panic Strategy Parameters
    enable_fade_panic: bool = True
    fade_panic_window_seconds: float = 120.0   # Only active in last 120s
    fade_panic_hard_stop_seconds: float = 10.0 # Stop buying at T-10s (was 15s — more aggressive)
    fade_panic_odds_shift_threshold: float = 0.15  # 15% odds shift to trigger (raised from 8% — low shifts had 20% WR)
    fade_panic_spot_max_change: float = 0.0005 # Max spot change for "no movement" (0.05%)
    fade_panic_odds_window_seconds: int = 60   # Window for measuring odds shift
    fade_panic_order_size: float = 50.0        # Doubled back — 35% WR at 15% threshold with 3.6:1 win/loss ratio is profitable
    fade_panic_max_entry_price: float = 0.92   # Don't buy above this price
    fade_panic_spot_vs_open_max_change: float = 0.003  # 0.3% max spot move from window open
    fade_panic_max_per_market: float = 10.0  # Max $ per market (anti-spam)

    # Resolution Sniper Strategy Parameters
    enable_resolution_sniper: bool = True
    sniper_order_size: float = 30.0           # Must be >= 3 * MIN_TRADE_SIZE (3 tranches)
    sniper_min_confidence: float = 0.75       # Min win probability to enter (calibrated via vol_multiplier)
    sniper_window_seconds: float = 120.0      # Activate at T-120s
    sniper_hard_stop_seconds: float = 15.0    # Stop buying at T-15s
    sniper_min_entry_price: float = 0.20      # Reject fills below this (0% win rate historically)
    sniper_max_entry_price: float = 0.97      # Reject fills above this
    sniper_exit_confidence_floor: float = 0.0 # Emergency exit threshold (0 = disabled)
    sniper_min_vol_data_points: int = 10      # Min data points for vol calc
    sniper_vol_floor: float = 0.0005          # Min sigma floor (0.05%/min) — prevents overconfident CDF in quiet markets
    sniper_vol_window_seconds: int = 600      # Seconds of spot data for vol calc (10 min)
    sniper_vol_multiplier: float = 3.0        # Inflate sigma to correct overconfident CDF (fat tails + mean reversion)
    sniper_momentum_window_seconds: int = 30  # Seconds of recent prices for momentum check
    sniper_high_confidence_threshold: float = 0.90  # Win prob above this gets boosted size
    sniper_high_confidence_multiplier: float = 3.0  # Tranche size multiplier for high confidence
    sniper_max_tranches: int = 3              # Max tranches per market (1 = single entry, no doubling down)
    invert_sniper: bool = False  # Contrarian mode: flip sniper signals (buy NO when signal says UP)

    # Chainlink Oracle Validation
    enable_chainlink_filter: bool = True
    chainlink_max_divergence_pct: float = 0.003  # 0.3% max Binance-Chainlink divergence
    chainlink_rpc_url: str = "https://polygon-rpc.com"

    # Fill quality guards
    min_entry_price: float = 0.10             # Don't buy contracts below this price
    max_fill_slippage: float = 0.05           # Max VWAP-to-best-price ratio (5% default)
    max_levels_consumed: int = 3              # Max orderbook levels to walk for a fill

    # Spot buffer
    spot_buffer_window: int = 900             # Max age of spot prices in buffer (15 min)

    # Markets (only assets with 15-min up/down markets on Polymarket)
    markets: list[str] = ["BTC", "ETH", "SOL", "XRP"]
    market_intervals: list[str] = ["15m"]  # Future: add "1h", "4h" for hourly markets
    market_slug_override: str = ""

    # Risk Limits (AGGRESSIVE MODE)
    disable_circuit_breaker: bool = False  # skip circuit breaker (useful in DRY_RUN)
    max_entries_per_market: int = 10  # limit accumulation; 10 * $50 = $500 max per market
    max_entries_per_strategy_per_market: int = 5  # max entries per strategy per market window
    max_position_per_market: float = 1000.0  # aggressive for learning mode
    max_total_position: float = 4000.0  # paper money — let strategies play
    max_daily_loss: float = 250.0  # safety valve
    max_unhedged_exposure: float = 800.0  # aggressive for fade_panic learning

    # Signal Inversion (contrarian paper test)
    invert_signals: bool = False  # Flip all directional signals: YES→NO, NO→YES

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

    # Divergence / Information-Theoretic Features
    enable_divergence_scoring: bool = True
    divergence_ranking_alpha: float = 0.7  # profit_pct weight in composite ranking
    divergence_kelly_scaling: bool = False
    divergence_exit_signals: bool = False
    divergence_exit_threshold: float = 0.5
    enable_cross_asset_strategy: bool = False
    cross_asset_correlation_window: int = 3600
    cross_asset_min_divergence: float = 0.01
    cross_asset_order_size: float = 25.0

    # CEX Perp Hedging (Binance Futures)
    enable_cex_hedging: bool = False  # disabled by default, opt-in
    binance_futures_api_key: SecretStr = SecretStr("")
    binance_futures_api_secret: SecretStr = SecretStr("")
    binance_futures_testnet: bool = True  # use testnet by default for safety
    cex_hedge_ratio: float = 0.5  # hedge 50% of Polymarket notional
    cex_hedge_leverage: int = 1  # no leverage amplification
    cex_hedgeable_strategies: str = "fade_panic,resolution_sniper"  # comma-separated

    # Dynamic allocation
    enable_dynamic_allocation: bool = False

    # Decision logging (Phase 1 observability)
    enable_decision_logging: bool = True
    spot_snapshot_interval: float = 5.0  # seconds between spot price snapshots

    # Alpha Signals (Binance Futures public API)
    enable_alpha_signals: bool = True
    alpha_funding_poll_seconds: float = 28800.0   # 8h
    alpha_oi_poll_seconds: float = 60.0           # 1 min
    alpha_vol_window_seconds: int = 600           # 10 min
    alpha_vol_min_data_points: int = 10
    alpha_vol_low_threshold: float = 0.0005
    alpha_vol_high_threshold: float = 0.0020
    alpha_funding_bullish_threshold: float = -0.0001
    alpha_funding_bearish_threshold: float = 0.0001
    alpha_oi_rising_threshold: float = 0.02
    alpha_oi_falling_threshold: float = -0.02

    # Daily Summary
    daily_summary_hour: int = 0  # UTC hour to send daily summary

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert vault source with highest priority after init_settings.

        Priority order (highest first):
        1. init_settings (constructor kwargs)
        2. VaultSettingsSource (encrypted vault)
        3. env_settings (environment variables)
        4. dotenv_settings (.env file)
        5. file_secret_settings
        """
        return (
            init_settings,
            VaultSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
