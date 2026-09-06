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


class Episodes(Base):
    """Research episode detection. The two absolute floors are fixed, not tuned."""

    join_gap_s: Pos = Field(..., description="seconds between liquidations that end an episode")
    min_notional_usd: Pos = Field(..., description="USD: absolute floor on episode notional")
    min_move_bps: Pos = Field(..., description="bps: absolute floor on anchor to extreme move")
    p95_lookback_d: int = Field(..., gt=0, description="days of prior episodes behind the p95")
    liq_z_min: float = Field(..., description="z-score that qualifies regardless of the p95")
    peak_window_s: Pos = Field(..., description="seconds: the window behind peak_liq")
    participation: Frac = Field(..., description="fraction of post-extreme notional takeable")
    participation_window_s: Pos = Field(..., description="seconds after the extreme for capacity")


class Levels(Base):
    """Supply and demand zones, Q7's inputs. Dumb by design: fractals and rolling extremes."""

    fractal_n: int = Field(..., gt=0, description="bars each side a swing point must beat")
    range_d: int = Field(..., gt=0, description="days behind the rolling range extremes")
    max_dist_bps: Pos = Field(..., description="bps beyond which a level counts as absent")
    zone_width_bps: Pos = Field(..., description="bps within which a price is inside the zone")


class Features(Base):
    decision_cadence_ms: int = Field(..., gt=0, description="milliseconds between feature ticks")
    windows_s: tuple[int, ...] = Field(..., min_length=1, description="seconds: trailing windows")
    z_lookback_h: Pos = Field(..., description="hours of history behind the liquidation z-score")
    z_grid_s: Pos = Field(..., description="seconds: grid the z distribution is sampled on")
    z_robust: bool = Field(..., description="use median and MAD instead of mean and stdev")
    rv_windows_s: tuple[int, ...] = Field(..., min_length=1, description="seconds: rv windows")
    oi_windows_s: tuple[int, ...] = Field(
        ..., min_length=1, description="seconds: open interest change windows"
    )
    cvd_slope_window_s: Pos = Field(..., description="seconds over which CVD slope is measured")
    depth_bps: Pos = Field(..., description="bps from the touch used for depth features")
    vwap_anchor_s: Pos = Field(..., description="seconds: pre-cascade VWAP anchor window")
    proxy: ForcedProxy
    episodes: Episodes
    levels: Levels


CascadeTrigger = Literal["exact", "proxy", "crossvenue"]
ExhaustionMode = Literal["fixed_delay", "rule"]


class LiqUsdFloors(Base):
    """USD liquidated in `cascade.window_s` that qualifies a cascade, per universe tier.

    Same shape and the same tier lookup as `filters.max_spread_bps`, because it is the same
    question asked of size rather than of spread: what counts as a lot on this coin.
    """

    core: NonNeg = Field(..., description="USD: floor for core coins")
    major: NonNeg = Field(..., description="USD: floor for major coins")
    minor: NonNeg = Field(..., description="USD: floor for minor coins")


class Cascade(Base):
    # One detector behind all three values; the trigger picks which feed fills the
    # liquidation columns and which condition the detector consults. `exact` is the
    # historical ceiling and cannot be traded: Settings refuses it on mainnet and
    # live_trading_block_reason names it. docs/DECISIONS.md, 2026-09-06.
    trigger: CascadeTrigger = Field("exact", description="which liquidation feed the trigger reads")
    # The two liquidation conditions. Each is applied when it is set and skipped when it is
    # null; the validator below refuses both null, which would admit every row on the tape.
    liq_z_min: float | None = Field(
        ..., description="z-score: liquidation notional over window_s; null does not apply it"
    )
    liq_usd_min: LiqUsdFloors | None = Field(
        None, description="USD per tier: liquidated notional over window_s; null is off"
    )
    proxy_min: NonNeg = Field(..., description="multiple: hl_forced_proxy alternative trigger")
    ret_over_rv_min: NonNeg = Field(..., description="ratio: abs(ret_w) / rv_5m")
    imbalance_min: Frac = Field(..., description="ratio: liq_imbalance favouring the side")
    window_s: Pos = Field(..., description="seconds: window the conditions are evaluated over")

    @model_validator(mode="after")
    def _one_liquidation_condition_at_least(self) -> Cascade:
        if self.liq_z_min is None and self.liq_usd_min is None:
            raise ValueError(
                "cascade needs liq_z_min or liq_usd_min; with both null every row on the "
                "tape passes the liquidation condition"
            )
        return self


class Exhaustion(Base):
    # Q2 found a flat delay carried more PnL than the rule on every month of the sample,
    # so `fixed_delay` is the default and the rule stays as code behind the switch.
    mode: ExhaustionMode = Field(
        "fixed_delay", description="fixed_delay enters after fixed_delay_s; rule uses the below"
    )
    fixed_delay_s: Pos = Field(
        120.0, description="seconds after cascade start that mode=fixed_delay enters"
    )
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


TargetFrom = Literal["extreme", "entry"]


class Intent(Base):
    # Where the reversion target is measured from. `extreme` is the spec's formula and the
    # default, so v0 and every number taken under it are untouched. `entry` measures the
    # same fraction of the distance that is *left*, which is the only sensible reading once
    # the entry can land after the bounce has started. docs/DECISIONS.md, 2026-09-06.
    target_from: TargetFrom = Field(
        "extreme", description="anchor the target on the cascade extreme or on the entry"
    )
    k_target: Pos = Field(..., description="fraction of (anchor - extreme) used as the target")
    k_stop: Pos = Field(..., description="multiple of rv_5m * mid placed beyond the extreme")
    time_stop_s: Pos = Field(..., description="seconds before an unresolved position is flattened")
    urgency_liq_z: float = Field(..., description="z-score at or above which urgency is high")
    allow_taker_entry_high_urgency: bool = Field(..., description="allow taker entry when urgent")


class ZoneProximityRule(Base):
    """Q7 said no, so this ships disabled. `features/levels.py` stays built and tested."""

    enabled: bool = Field(False, description="run this rule after cascade and exhaustion fire")
    required: bool = Field(True, description="block on its own; false makes it confluence")
    zone_width_bps: Pos = Field(..., description="bps from the nearest level counted as in zone")


class MinCascadeNotionalRule(Base):
    """Trivial on purpose: it proves the rule mechanism with something easy to check."""

    enabled: bool = Field(False, description="run this rule after cascade and exhaustion fire")
    required: bool = Field(True, description="block on its own; false makes it confluence")
    min_notional_usd: Pos = Field(..., description="USD of liquidated notional the cascade needs")


class OiFlushRule(Base):
    """Open interest was flushed by the cascade, and had built up before it.

    The two halves are separable because they are separate claims. The drop says forced
    closing actually happened rather than price merely moving; the build says there was a
    crowded position there to be closed, which is what a trader reads before a cascade and
    what distinguishes a flush from a slow bleed. `build_min_frac` is null by default
    because the drop alone is the cheaper claim and the one the feature measures directly.
    """

    enabled: bool = Field(False, description="run this rule after cascade and exhaustion fire")
    required: bool = Field(True, description="block on its own; false makes it confluence")
    drop_min_frac: NonNeg = Field(
        ..., description="fraction of open interest closed over the rv window"
    )
    build_min_frac: NonNeg | None = Field(
        None, description="fraction open interest is up over the build window; null skips it"
    )


VwapReference = Literal["day", "anchor"]


class VwapStretchRule(Base):
    """Price is stretched away from a VWAP, on the side the cascade pushed it."""

    enabled: bool = Field(False, description="run this rule after cascade and exhaustion fire")
    required: bool = Field(True, description="block on its own; false makes it confluence")
    reference: VwapReference = Field(
        "day", description="day: session VWAP from 00:00 UTC; anchor: the pre-cascade window"
    )
    min_disp_bps: NonNeg = Field(..., description="bps mid must sit past the VWAP")


class LevelReclaimRule(Base):
    """The swing failure and the three tap: through a level, then back above it."""

    enabled: bool = Field(False, description="run this rule after cascade and exhaustion fire")
    required: bool = Field(True, description="block on its own; false makes it confluence")
    sweep_min_bps: NonNeg = Field(
        ..., description="bps beyond the level the cascade extreme must have traded"
    )
    reclaim_min_bps: NonNeg = Field(..., description="bps back past the level mid must be now")


class StrategyRules(Base):
    """One field per rule in `strategy.rules.RULES`.

    `extra="forbid"` from Base is what makes an unknown rule name in a config file fail
    loudly instead of being silently ignored, which would leave the owner reading a report
    for a rule that never ran. `tests/test_strategy_rules.py` pins that this model and the
    RULES registry name exactly the same set.
    """

    zone_proximity: ZoneProximityRule
    min_cascade_notional: MinCascadeNotionalRule
    oi_flush: OiFlushRule
    vwap_stretch: VwapStretchRule
    level_reclaim: LevelReclaimRule


class Strategy(Base):
    """The rule layer: what blocks on its own, and how much of the rest has to agree.

    An all-AND stack of confluence rules is the wrong shape for this setup. A trader takes
    tens of trades a week by hand, and the reason is that the liquidation is the signal and
    the rest are corroboration: any one of them being absent is normal. So a rule is either
    `required`, in which case it blocks alone, or optional, in which case it is one vote and
    `min_confluence` is how many votes an entry needs.
    """

    rules: StrategyRules
    min_confluence: int = Field(
        0, ge=0, description="count: enabled optional rules that must pass; 0 only reports them"
    )

    @model_validator(mode="after")
    def _confluence_is_reachable(self) -> Strategy:
        optional = sum(
            1
            for name in type(self.rules).model_fields
            if getattr(self.rules, name).enabled and not getattr(self.rules, name).required
        )
        if self.min_confluence > optional:
            raise ValueError(
                f"strategy.min_confluence is {self.min_confluence} but only {optional} enabled "
                "rules are optional, so no intent could ever pass"
            )
        return self


class SpreadTiers(Base):
    core: Pos = Field(..., description="bps: max spread for core coins")
    major: Pos = Field(..., description="bps: max spread for major coins")
    minor: Pos = Field(..., description="bps: max spread for minor coins")


class Filters(Base):
    max_spread_bps: SpreadTiers
    min_edge_multiple: Pos = Field(..., description="multiple of the round trip fee required")
    skip_when_btc_cascading: bool = Field(..., description="skip alts during market-wide sweeps")
    # None means the gate is not applied at all. Phase 3 measured it as inert, and a
    # threshold set so high it can never fire is a config that lies about what runs.
    funding_extreme_bps_8h: NonNeg | None = Field(
        None, description="bps per 8h against the trade; null disables the gate"
    )
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
    cluster_window_s: Pos = Field(
        300.0, description="seconds: entries on one side this close together are one cluster"
    )
    max_cluster_exposure_frac: Frac = Field(
        0.25, description="fraction of equity: gross notional entered inside one cluster"
    )
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
    feature_grid_ms: int = Field(
        1000, gt=0, description="milliseconds between precomputed feature grid rows"
    )
    latency_ms: int = Field(..., ge=0, description="milliseconds from intent to visible order")
    taker_fee_frac: NonNeg = Field(..., description="fraction of notional per taker side")
    maker_fee_frac: NonNeg = Field(..., description="fraction of notional per maker side")
    apply_funding: bool = Field(..., description="accrue funding hourly while a position is open")
    maker_queue_pessimism: Pos = Field(..., description="multiplier on assumed queue ahead")
    maker_requires_trade_through: bool = Field(..., description="Alo needs a trade through to fill")
    # Market data older than this is a guess, not a price. A book snapshot past it is
    # discarded and the fill falls back to the tape; a tape past it refuses the fill
    # outright and counts it. `slip_stale_bps` below is the mild penalty for a snapshot
    # that is merely a second or two old, which is a different thing.
    max_quote_age_s: Pos = Field(
        5.0, description="seconds: book or tape older than this cannot price a taker fill"
    )
    slip_stale_bps: NonNeg = Field(..., description="bps added when the book snapshot is stale")
    stale_book_s: Pos = Field(..., description="seconds after which an L2 snapshot is stale")
    window_warmup_s: NonNeg = Field(
        300.0, description="seconds of tape replayed before a cascade candidate"
    )
    window_margin_s: NonNeg = Field(
        60.0, description="seconds of slack after the last thing a cascade can cause"
    )
    holdout_frac: Frac = Field(..., description="fraction of the chronological tail held out")
    walk_forward_refit_days: int = Field(..., gt=0, description="days between walk forward refits")
    bootstrap_samples: int = Field(..., gt=0, description="trade level bootstrap draws")

    @model_validator(mode="after")
    def _grid_is_a_multiple_of_nothing_finer(self) -> Backtest:
        """A grid finer than the decision cadence would compute rows nobody ever reads."""
        if self.feature_grid_ms % 250 and 250 % self.feature_grid_ms:
            raise ValueError(
                "backtest.feature_grid_ms and the 250ms decision cadence must divide one "
                "another, so every decision tick lands on or after a grid row"
            )
        return self


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
    strategy: Strategy
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

    @model_validator(mode="after")
    def _exact_trigger_is_not_tradeable(self) -> Settings:
        """`cascade.trigger: exact` reads S3 liquidations the live bot cannot see.

        Refused on mainnet outright rather than at the point of use, because the failure
        it prevents is silent: the engine would run, emit intents and look correct, on a
        trigger whose events arrive a day late. The backtest ceiling this trigger produces
        is a measurement of an edge nobody can reach in real time; see
        `docs/DECISIONS.md`, 2026-09-06. Phase 5's loop also has to consult
        `live_trading_block_reason`, which names it, so neither route can start on it.
        """
        if self.env == "mainnet" and self.cascade.trigger == "exact":
            raise ValueError(
                "cascade.trigger 'exact' reads hl_s3 liquidations, which are not visible "
                "in real time, so it is a backtest-only trigger and mainnet refuses it. "
                "Use 'proxy' or 'crossvenue', or run this config under --env paper."
            )
        return self

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
        if self.cascade.trigger == "exact":
            # Unreachable while the validator above stands, and kept anyway: it is the
            # check Phase 5's loop calls, and it must not depend on another module having
            # already refused. Two independent refusals of the same untradeable trigger.
            return "cascade.trigger is 'exact', which is a backtest-only liquidation feed"
        return None
