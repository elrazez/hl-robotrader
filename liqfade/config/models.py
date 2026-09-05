"""Pydantic v2 models for every configurable parameter.

One model per YAML section in config/default.yaml. Field descriptions restate the unit
so `liqfade config show` and the panel settings page can render them without reading the
YAML comments. Every model forbids unknown keys: a typo in overrides.yaml or in an env
var must fail loudly rather than silently do nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Env = Literal["paper", "testnet", "mainnet"]
Frac = Annotated[float, Field(ge=0.0, le=1.0)]
Pos = Annotated[float, Field(gt=0.0)]
NonNeg = Annotated[float, Field(ge=0.0)]
Pct = Annotated[float, Field(ge=0.0, le=100.0)]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(Base):
    data_dir: Path = Field(Path("data"), description="path: data lake, KILL flag, state.json")
    reports_dir: Path = Field(Path("reports"), description="path: backtest and research reports")


class Universe(Base):
    core_coins: tuple[str, ...] = Field(..., description="HL symbols always subscribed")
    top_n_by_volume: int = Field(..., gt=0, description="count: top N perps by dayNtlVlm")
    min_day_ntl_vlm_usd: NonNeg = Field(..., description="USD: 24h notional volume floor")
    min_open_interest_usd: NonNeg = Field(..., description="USD: open interest floor")
    min_listing_days: int = Field(..., ge=0, description="days since listing before trading")
    refresh_min: int = Field(..., gt=0, description="minutes between universe refreshes")
    exclude: tuple[str, ...] = Field((), description="HL symbols never traded")


class HyperliquidVenue(Base):
    enabled: bool
    api_url: str = Field(..., description="url: POST /info and /exchange base")
    ws_url: str = Field(..., description="url: websocket endpoint")
    ws_ping_s: Pos = Field(..., description="seconds between ping frames")
    liq_weight: NonNeg = Field(..., description="weight in aggregated liquidation notional")
    complete: bool = Field(..., description="true when the venue publishes every liquidation")


class WsVenue(Base):
    enabled: bool
    ws_url: str = Field(..., description="url: websocket endpoint")
    liq_weight: NonNeg = Field(..., description="weight in aggregated liquidation notional")
    complete: bool = Field(..., description="true when the venue publishes every liquidation")
    subscribe_batch: int = Field(10, gt=0, description="count: symbols per subscribe frame")


class Coinalyze(Base):
    enabled: bool
    base_url: str = Field(..., description="url: REST base")
    poll_s: Pos = Field(..., description="seconds between OI and funding polls")


class Venues(Base):
    hyperliquid: HyperliquidVenue
    binance: WsVenue
    bybit: WsVenue
    okx: WsVenue
    coinalyze: Coinalyze


class Recorder(Base):
    compaction_hour_utc: int = Field(..., ge=0, le=23, description="UTC hour of nightly compaction")
    ring_buffer_rows: int = Field(
        ..., gt=0, description="rows buffered per output file before an early flush"
    )
    gap_alert_s: Pos = Field(..., description="seconds of stream silence counted as a gap")
    silence_alert_s: Pos = Field(..., description="seconds of silence that alerts Telegram")
    parse_error_alert_1h: int = Field(
        ..., gt=0, description="count: refused rows plus unreadable messages per hour that alerts"
    )
    reconnect_backoff_min_s: Pos = Field(..., description="seconds: initial reconnect delay")
    reconnect_backoff_max_s: Pos = Field(..., description="seconds: capped reconnect delay")
    reconnect_jitter_frac: Frac = Field(..., description="fraction of delay added as jitter")
    disk_alert_pct: Pct = Field(..., description="percent disk used that raises an alert")

    @model_validator(mode="after")
    def _backoff_ordered(self) -> Recorder:
        if self.reconnect_backoff_max_s < self.reconnect_backoff_min_s:
            raise ValueError("reconnect_backoff_max_s must be >= reconnect_backoff_min_s")
        return self


class ForcedProxy(Base):
    vol_median_lookback_s: Pos = Field(..., description="seconds of trailing aggressive volume")
    oi_delta_window_s: Pos = Field(..., description="seconds over which OI must be falling")
    mark_mid_pct_threshold: Pct = Field(..., description="percentile of abs(mark-mid)/mid")


class Features(Base):
    decision_cadence_ms: int = Field(..., gt=0, description="milliseconds between feature ticks")
    windows_s: tuple[int, ...] = Field(..., min_length=1, description="seconds: trailing windows")
    z_lookback_h: Pos = Field(..., description="hours of history behind the liquidation z-score")
    z_grid_s: Pos = Field(..., description="seconds: grid the z distribution is sampled on")
    z_robust: bool = Field(..., description="use median and MAD instead of mean and stdev")
    rv_windows_s: tuple[int, ...] = Field(..., min_length=1, description="seconds: rv windows")
    cvd_slope_window_s: Pos = Field(..., description="seconds over which CVD slope is measured")
    depth_bps: Pos = Field(..., description="bps from the touch used for depth features")
    vwap_anchor_s: Pos = Field(..., description="seconds: pre-cascade VWAP anchor window")
    proxy: ForcedProxy


class Cascade(Base):
    liq_z_min: float = Field(..., description="z-score: liquidation notional over window_s")
    proxy_min: NonNeg = Field(..., description="multiple: hl_forced_proxy alternative trigger")
    ret_over_rv_min: NonNeg = Field(..., description="ratio: abs(ret_w) / rv_5m")
    imbalance_min: Frac = Field(..., description="ratio: liq_imbalance favouring the side")
    window_s: Pos = Field(..., description="seconds: window the conditions are evaluated over")


class Exhaustion(Base):
    consecutive_ticks: int = Field(..., gt=0, description="ticks all conditions must hold for")
    liq_decay_frac: Frac = Field(..., description="fraction of peak_liq_15s liq_15s must fall to")
    require_flow_flip: bool = Field(..., description="require a CVD slope or book imbalance flip")
    mark_mid_pct_threshold: Pct = Field(
        ..., description="percentile abs(mark-mid)/mid must be under"
    )
    no_new_extreme_s: Pos = Field(..., description="seconds since the last new extreme mid")
    min_age_s: Pos = Field(..., description="seconds: minimum cascade age")
    max_age_s: Pos = Field(..., description="seconds: maximum cascade age")

    @model_validator(mode="after")
    def _age_ordered(self) -> Exhaustion:
        if self.max_age_s <= self.min_age_s:
            raise ValueError("exhaustion.max_age_s must be > min_age_s")
        return self


class Intent(Base):
    k_target: Pos = Field(..., description="fraction of (anchor - extreme) used as the target")
    k_stop: Pos = Field(..., description="multiple of rv_5m * mid placed beyond the extreme")
    time_stop_s: Pos = Field(..., description="seconds before an unresolved position is flattened")
    urgency_liq_z: float = Field(..., description="z-score at or above which urgency is high")
    allow_taker_entry_high_urgency: bool = Field(..., description="allow taker entry when urgent")


class SpreadTiers(Base):
    core: Pos = Field(..., description="bps: max spread for core coins")
    major: Pos = Field(..., description="bps: max spread for major coins")
    minor: Pos = Field(..., description="bps: max spread for minor coins")


class Filters(Base):
    max_spread_bps: SpreadTiers
    min_edge_multiple: Pos = Field(..., description="multiple of the round trip fee required")
    skip_when_btc_cascading: bool = Field(..., description="skip alts during market-wide sweeps")
    funding_extreme_bps_8h: NonNeg = Field(..., description="bps per 8h against the trade")
    min_notional_usd: Pos = Field(..., description="USD: exchange minimum order value")


class Sizing(Base):
    risk_frac: Frac = Field(..., description="fraction of equity risked at the stop")
    vol_participation: Frac = Field(..., description="fraction of trailing HL notional volume")
    vol_window_s: Pos = Field(..., description="seconds: the volume window for the cap above")
    depth_participation: Frac = Field(..., description="fraction of resting depth within depth_bps")
    coin_exposure_frac: Frac = Field(..., description="max notional in one coin / equity")
    total_exposure_frac: Pos = Field(..., description="max gross notional / equity")
    respect_venue_max_leverage: bool = Field(..., description="also clamp to maxLeverage * equity")


class Risk(Base):
    max_daily_loss_frac: Frac = Field(..., description="fraction of day start equity")
    max_dd_frac: Frac = Field(..., description="fraction below peak equity")
    max_consecutive_losses: int = Field(..., gt=0, description="count of losses in a row")
    cooldown_after_streak_min: NonNeg = Field(
        ..., description="minutes of no entries after a streak"
    )
    coin_cooldown_after_loss_min: NonNeg = Field(..., description="minutes per coin after a loss")
    coin_cooldown_after_exit_min: NonNeg = Field(..., description="minutes per coin after any exit")
    max_concurrent_positions: int = Field(..., gt=0, description="count")
    max_positions_per_direction: int = Field(..., gt=0, description="count")
    stale_coin_s: Pos = Field(..., description="seconds without HL data before a coin is blocked")
    stale_global_cancel_s: Pos = Field(..., description="seconds of silence before cancel all")
    stale_global_flatten_s: Pos = Field(..., description="seconds of silence before flatten all")
    max_reconnects_10min: int = Field(..., gt=0, description="count before entries halt")
    max_rejects_10min: int = Field(..., gt=0, description="count before entries halt")
    heartbeat_s: Pos = Field(..., description="seconds between heartbeat file touches")
    watchdog_stale_s: Pos = Field(
        ..., description="seconds of heartbeat staleness before cancel all"
    )
    kill_deadline_s: Pos = Field(..., description="seconds within which /kill must have flattened")

    @model_validator(mode="after")
    def _stale_ordered(self) -> Risk:
        if not self.stale_coin_s <= self.stale_global_cancel_s <= self.stale_global_flatten_s:
            raise ValueError(
                "risk stale thresholds must be coin <= global_cancel <= global_flatten"
            )
        if self.watchdog_stale_s <= self.heartbeat_s:
            raise ValueError("risk.watchdog_stale_s must be > heartbeat_s")
        return self


class Execution(Base):
    entry_tif: Literal["Alo", "Gtc", "Ioc"] = Field(..., description="time in force for entries")
    entry_ttl_s: Pos = Field(..., description="seconds a resting entry lives before repricing")
    max_reprices: int = Field(..., ge=0, description="count of reprices per intent")
    taker_slippage_bps: Pos = Field(..., description="bps bound on Ioc market orders")
    reconcile_s: Pos = Field(..., description="seconds between reconciliations")
    action_budget_per_hour: int = Field(..., gt=0, description="orders+cancels+modifies per hour")
    max_intents_per_coin_per_min: int = Field(..., gt=0, description="count")
    transient_retries: int = Field(..., ge=0, description="retries on transport errors")
    use_isolated_margin: bool = Field(..., description="false means cross margin")
    order_db: str = Field(..., description="filename of the sqlite order store under data_dir")


class Backtest(Base):
    latency_ms: int = Field(..., ge=0, description="milliseconds from intent to visible order")
    taker_fee_frac: NonNeg = Field(..., description="fraction of notional per taker side")
    maker_fee_frac: NonNeg = Field(..., description="fraction of notional per maker side")
    apply_funding: bool = Field(..., description="accrue funding hourly while a position is open")
    maker_queue_pessimism: Pos = Field(..., description="multiplier on assumed queue ahead")
    maker_requires_trade_through: bool = Field(..., description="Alo needs a trade through to fill")
    slip_stale_bps: NonNeg = Field(..., description="bps added when the book snapshot is stale")
    stale_book_s: Pos = Field(..., description="seconds after which an L2 snapshot is stale")
    holdout_frac: Frac = Field(..., description="fraction of the chronological tail held out")
    walk_forward_refit_days: int = Field(..., gt=0, description="days between walk forward refits")
    bootstrap_samples: int = Field(..., gt=0, description="trade level bootstrap draws")


class Control(Base):
    socket_path: Path = Field(..., description="path under data_dir, created with mode 0600")
    state_file: Path = Field(..., description="path under data_dir for the state snapshot")
    state_interval_s: Pos = Field(..., description="seconds between state snapshots")
    connect_timeout_s: Pos = Field(..., description="seconds a client waits for the socket")
    request_timeout_s: Pos = Field(..., description="seconds a client waits for a response")


class Panel(Base):
    host: str = Field(..., description="bind address, loopback unless allow_non_loopback")
    port: int = Field(..., gt=0, lt=65536, description="tcp port")
    allow_non_loopback: bool = Field(..., description="must be true to bind a public address")
    session_ttl_s: Pos = Field(..., description="seconds a login cookie stays valid")
    login_max_attempts: int = Field(..., gt=0, description="failed logins per window")
    login_window_s: Pos = Field(..., description="seconds the failed login window covers")
    state_stale_s: Pos = Field(..., description="seconds after which /api/state returns 503")


class Telegram(Base):
    enabled: bool
    alert_on_intent: bool = Field(..., description="post every intent with its explain values")
    parse_mode: Literal["HTML", "MarkdownV2", "None"] = Field(..., description="Bot API parse mode")
    send_timeout_s: Pos = Field(..., description="seconds before an alert send is abandoned")


class Sync(Base):
    host: str = Field("", description="ssh target, e.g. liqfade@vps")
    remote_root: Path = Field(..., description="remote path holding data/ and reports/")
    paths: tuple[str, ...] = Field(..., min_length=1, description="paths relative to remote_root")
    rsync_flags: tuple[str, ...] = Field(..., description="flags passed to rsync")


class Logging(Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(..., description="log level")
    proc: str = Field(..., description="process name stamped on every log line")


class Settings(Base):
    """The whole resolved configuration. Build it with liqfade.config.load_settings."""

    env: Env
    paths: Paths
    universe: Universe
    venues: Venues
    recorder: Recorder
    features: Features
    cascade: Cascade
    exhaustion: Exhaustion
    intent: Intent
    filters: Filters
    sizing: Sizing
    risk: Risk
    execution: Execution
    backtest: Backtest
    control: Control
    panel: Panel
    telegram: Telegram
    sync: Sync
    logging: Logging

    # Paths derived from paths.data_dir, so there is one definition of each.

    @property
    def kill_file(self) -> Path:
        """The kill flag. Its presence blocks live trading unconditionally."""
        return self.paths.data_dir / "KILL"

    @property
    def kill_switch_present(self) -> bool:
        """True when the kill flag is there, and true when we cannot tell.

        Path.exists() answers False for every OSError, so an unreadable, unmounted or
        replaced-by-a-file data_dir read as "no kill file" and let live trading through.
        The kill switch is the one control that has to fail closed, so anything but a
        clean "not found" counts as present.
        """
        try:
            os.stat(self.kill_file)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    @property
    def state_path(self) -> Path:
        return self.paths.data_dir / self.control.state_file

    @property
    def socket_path(self) -> Path:
        return self.paths.data_dir / self.control.socket_path

    @property
    def heartbeat_path(self) -> Path:
        return self.paths.data_dir / "heartbeat"

    @property
    def risk_state_path(self) -> Path:
        return self.paths.data_dir / "risk_state.json"

    @property
    def order_db_path(self) -> Path:
        return self.paths.data_dir / self.execution.order_db

    @property
    def live_trading_enabled(self) -> bool:
        """True only when all three conditions of CLAUDE.md rule 3 hold.

        env is mainnet, LIQFADE_LIVE_TRADING is exactly "1", and the kill file is absent.
        Read live every time rather than cached, because the kill file can appear at any
        moment and no caller may act on a stale answer.
        """
        return (
            self.env == "mainnet"
            and os.environ.get("LIQFADE_LIVE_TRADING") == "1"
            and not self.kill_switch_present
        )

    def live_trading_block_reason(self) -> str | None:
        """Why live trading is blocked, or None when it is enabled. Used by alerts and CLI."""
        if self.env != "mainnet":
            return f"env is {self.env}, not mainnet"
        if os.environ.get("LIQFADE_LIVE_TRADING") != "1":
            return "LIQFADE_LIVE_TRADING is not set to 1"
        if self.kill_switch_present:
            return f"kill switch file present at {self.kill_file}"
        return None
