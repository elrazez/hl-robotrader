"""The online cascade detector: one instance per coin, fed a `FeatureRow` every tick.

This is the live counterpart of `features/episodes.py`. That module is vectorized, sees a
whole episode at once and is allowed to, because it measures cascades after the fact. This
one sees each tick once, in order, and can never look forward. The two are held to the same
answer by `tests/test_strategy_cascade_consistency.py`, which runs both over one synthetic
stream and requires the same episode count with starts inside one decision tick.

Two things it decides, per coin per side:

**Start** (`docs/STRATEGY_AND_RISK.md` section 3). Forced flow is extreme, price has moved
with it by more than a couple of standard deviations, and the liquidations are lopsided
towards the side claimed. Which feed answers the first of those is `cascade.trigger`:
`exact` and `crossvenue` read the liquidation z-score and differ only in which venues
filled the column, `proxy` reads `hl_forced_proxy` because the live bot cannot see exact
Hyperliquid liquidations at all. That switch is the whole reason this phase exists; see
`docs/DECISIONS.md`, 2026-09-06.

**Entry**, under `exhaustion.mode`. `fixed_delay` waits a flat `fixed_delay_s` from the
start, which is what Q2 found carried more PnL over 2,722 episodes. `rule` is the spec's
exhaustion rule, kept as code so turning it on in a backtest config prints what it costs
rather than requiring the argument to be had again from memory.

One signal per cascade. A cascade ends when it signals or when it ages past
`exhaustion.max_age_s`, whichever comes first, and only then may the same side start again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from liqfade.config.models import Settings
from liqfade.strategy.types import CascadeState, FeatureRow, LiqSide, Tier

__all__ = ["CascadeDetector", "CascadeSignal"]

SIDES: tuple[LiqSide, ...] = ("long", "short")


def _higher(current: float | None, candidate: float | None) -> float | None:
    """The larger of two possibly undefined z-scores. None only when both are None."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


@dataclass(frozen=True, slots=True)
class CascadeSignal:
    """A cascade that is ready to be faded.

    Args:
        state: the cascade as of this tick, carrying the anchor and extreme the intent's
            target and stop are built from.
        reason: `fixed_delay` or `exhaustion_rule`, whichever mode produced it. Reaches
            `explain()` so a trade log says which entry rule was running.
        ts_ms: the tick the signal fired on.
    """

    state: CascadeState
    reason: str
    ts_ms: int


class CascadeDetector:
    """Tracks cascades on one coin. Not thread safe and not meant to be: one per coin.

    Deterministic and free of I/O, wall clock and randomness, so replaying the same rows
    twice produces the same signals. That is what lets the backtest and the live loop share
    it without a branch.
    """

    def __init__(self, coin: str, settings: Settings, *, tier: Tier = "minor") -> None:
        """
        Args:
            coin: the Hyperliquid symbol.
            settings: resolved config, read on every tick rather than snapshotted.
            tier: which `cascade.liq_usd_min` entry applies, from the coin's `AssetMeta`.
                The same lookup `filters.max_spread_bps` uses. Defaults to `minor`, which
                is `AssetMeta`'s own default for a coin the universe cannot place.
        """
        self.coin = coin
        self.settings = settings
        self.tier = tier
        self._active: dict[LiqSide, CascadeState] = {}

    def active(self, side: LiqSide) -> CascadeState | None:
        """The cascade running on `side`, or None."""
        return self._active.get(side)

    def on_tick(self, row: FeatureRow) -> CascadeSignal | None:
        """Advance one decision tick and return a signal when one fires.

        Args:
            row: this coin's features at `row.ts_ms`, built only from data knowable before
                it.

        Returns:
            A `CascadeSignal` on the tick the entry condition is first met, else None. At
            most one per cascade.
        """
        # Update before starting, so a cascade that starts on this tick does not
        # immediately consume the tick as its own first observation of the extreme.
        for side in SIDES:
            if side in self._active:
                self._advance(side, row)
        for side in SIDES:
            if side not in self._active and self._starts(side, row):
                self._begin(side, row)

        for side in SIDES:
            state = self._active.get(side)
            if state is None:
                continue
            reason = self._entry_reason(state, row)
            if reason is not None:
                del self._active[side]
                return CascadeSignal(state=state, reason=reason, ts_ms=row.ts_ms)
        return None

    # --- start ----------------------------------------------------------------

    def _starts(self, side: LiqSide, row: FeatureRow) -> bool:
        """Every start condition from spec section 3, for one side."""
        if row.mid is None or row.mid <= 0:
            return False
        return (
            self._trigger_fires(side, row)
            and self._move_is_big_enough(side, row)
            and self._imbalance_favours(side, row)
        )

    def _trigger_fires(self, side: LiqSide, row: FeatureRow) -> bool:
        """The configured feed says forced flow is extreme.

        Two conditions, each applied only when its config key is set, and `Cascade` refuses
        a config where both are null. They ask different questions and neither subsumes the
        other: `liq_z_min` asks how unusual this flow is for this coin, which travels across
        a universe whose notional spans two orders of magnitude, and `liq_usd_min` asks
        whether it is large enough in dollars to have moved price at all, which a z-score
        cannot say on a series that is quiet most of the time.

        A null z is not a pass. An undefined z-score means the trailing distribution is not
        full yet, which is a statement that we do not know how unusual this is, and trading
        on "we do not know" is how a backtest finds an edge in its own warm-up period. The
        notional has no such state: no liquidations recorded is zero notional, and zero
        fails any floor above it.
        """
        cascade = self.settings.cascade
        if cascade.trigger == "proxy":
            proxy = row.proxy(side)
            return proxy is not None and proxy >= cascade.proxy_min
        # exact and crossvenue read the same column; the trigger chose which venues filled
        # it when the feature grid was built.
        if cascade.liq_z_min is not None:
            z = row.liq_z(side)
            if z is None or z < cascade.liq_z_min:
                return False
        if cascade.liq_usd_min is not None:
            floor: float = getattr(cascade.liq_usd_min, self.tier)
            if row.liq_usd_w(side) < floor:
                return False
        return True

    def _move_is_big_enough(self, side: LiqSide, row: FeatureRow) -> bool:
        """Price has moved with the liquidations by `ret_over_rv_min` volatilities."""
        if row.ret_w is None or row.rv is None or row.rv <= 0:
            return False
        direction = -1.0 if side == "long" else 1.0
        # The move has to be in the direction the liquidated side implies, not merely
        # large: a long liquidation that coincided with a rally is not a cascade.
        if row.ret_w * direction <= 0:
            return False
        return abs(row.ret_w) / row.rv >= self.settings.cascade.ret_over_rv_min

    def _imbalance_favours(self, side: LiqSide, row: FeatureRow) -> bool:
        """`liq_imbalance` leans towards `side` by at least `imbalance_min`.

        Imbalance is `(long - short) / (long + short)`, so the long side wants it positive.

        **Not applied under `cascade.trigger: proxy`.** The imbalance is a statement about a
        liquidation feed, and the whole premise of the proxy trigger is that no such feed is
        visible: it infers forced flow from Hyperliquid's own tape, open interest and mark
        divergence instead. Requiring an imbalance there would either block every intent, or
        quietly reintroduce the cross-venue dependency the trigger exists to avoid. The
        proxy is computed per side, so the direction this condition supplies is already in
        it. See `docs/DECISIONS.md`, 2026-09-06.
        """
        if self.settings.cascade.trigger == "proxy":
            return True
        minimum = self.settings.cascade.imbalance_min
        if side == "long":
            return row.liq_imbalance_w >= minimum
        return row.liq_imbalance_w <= -minimum

    def _begin(self, side: LiqSide, row: FeatureRow) -> None:
        assert row.mid is not None  # guarded by _starts
        self._active[side] = CascadeState(
            coin=self.coin,
            side=side,
            start_ts=row.ts_ms,
            anchor_px=row.mid,
            extreme_px=row.mid,
            extreme_ts=row.ts_ms,
            peak_liq_15s=row.liq_usd_peak(side),
            exhaustion_streak=0,
            total_liq_usd=row.liq_usd_w(side),
            peak_liq_z=row.liq_z(side),
        )

    # --- tracking -------------------------------------------------------------

    def _advance(self, side: LiqSide, row: FeatureRow) -> None:
        """Roll the extreme, the peak burst and the running total forward one tick."""
        state = self._active[side]
        if state.age_s(row.ts_ms) > self.settings.exhaustion.max_age_s:
            del self._active[side]
            return

        extreme_px, extreme_ts = state.extreme_px, state.extreme_ts
        if row.mid is not None:
            # Strictly beyond: an equal touch is not a new extreme, so a flat market does
            # not keep resetting the "no new extreme" clock forever.
            beyond = row.mid < extreme_px if side == "long" else row.mid > extreme_px
            if beyond:
                extreme_px, extreme_ts = row.mid, row.ts_ms

        self._active[side] = replace(
            state,
            extreme_px=extreme_px,
            extreme_ts=extreme_ts,
            peak_liq_15s=max(state.peak_liq_15s, row.liq_usd_peak(side)),
            # The window column is a rolling sum, so adding it every tick would count the
            # same liquidation once per tick it stays in the window: 240 times a minute at
            # the 250ms cadence. The running maximum of the window is the most flow the
            # window ever held, which is what "how big was this cascade" means.
            total_liq_usd=max(state.total_liq_usd, row.liq_usd_w(side)),
            peak_liq_z=_higher(state.peak_liq_z, row.liq_z(side)),
        )

    # --- entry ----------------------------------------------------------------

    def _entry_reason(self, state: CascadeState, row: FeatureRow) -> str | None:
        """Whether this tick is the entry, and under which mode. None means not yet."""
        exhaustion = self.settings.exhaustion
        age = state.age_s(row.ts_ms)
        if age < exhaustion.min_age_s or age > exhaustion.max_age_s:
            return None
        if exhaustion.mode == "fixed_delay":
            return "fixed_delay" if age >= exhaustion.fixed_delay_s else None
        return "exhaustion_rule" if self._rule_exhausted(state, row) else None

    def _rule_exhausted(self, state: CascadeState, row: FeatureRow) -> bool:
        """The spec's exhaustion rule, held for `consecutive_ticks` in a row.

        Mutates the streak on `state`, which is why this is called at most once per side
        per tick. Every condition failing resets the streak to zero rather than decaying
        it: the rule is "all of these, consecutively", and a near miss is a miss.
        """
        exhaustion = self.settings.exhaustion
        holds = (
            self._liquidations_decayed(state, row)
            and self._flow_flipped(state, row)
            and self._mark_back_in_line(row)
            and self._no_new_extreme(state, row)
        )
        streak = state.exhaustion_streak + 1 if holds else 0
        self._active[state.side] = replace(state, exhaustion_streak=streak)
        return streak >= exhaustion.consecutive_ticks

    def _liquidations_decayed(self, state: CascadeState, row: FeatureRow) -> bool:
        """Forced flow has fallen to `liq_decay_frac` of the cascade's own peak."""
        if state.peak_liq_15s <= 0:
            return False
        floor = self.settings.exhaustion.liq_decay_frac * state.peak_liq_15s
        return row.liq_usd_peak(state.side) <= floor

    def _flow_flipped(self, state: CascadeState, row: FeatureRow) -> bool:
        """Aggressor flow or the book has turned against the cascade.

        Off when `require_flow_flip` is false. A null CVD slope and a null book imbalance
        together mean neither can be read, and unknown does not pass a gate.
        """
        if not self.settings.exhaustion.require_flow_flip:
            return True
        against = -state.direction  # a falling cascade wants buying to appear
        if row.cvd_slope is not None and row.cvd_slope * against > 0:
            return True
        return row.book_imbalance is not None and row.book_imbalance * against > 0

    def _mark_back_in_line(self, row: FeatureRow) -> bool:
        """`abs(mark - mid)` is back under its trailing percentile.

        Unknown passes here, unlike the gates above, and the asymmetry is deliberate: this
        condition is asking whether a dislocation has *ended*, and the historical year has
        no mark divergence percentile for most of its span. Requiring it would make the
        rule mode unrunnable on the data it has to be compared on, and the mode is off by
        default in any case.
        """
        if row.mark_mid_bps is None or row.mark_mid_threshold is None:
            return True
        return row.mark_mid_bps <= row.mark_mid_threshold

    def _no_new_extreme(self, state: CascadeState, row: FeatureRow) -> bool:
        """Price has not made a new extreme for `no_new_extreme_s`."""
        quiet_s = (row.ts_ms - state.extreme_ts) / 1000.0
        return quiet_s >= self.settings.exhaustion.no_new_extreme_s
