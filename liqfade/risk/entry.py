"""Sizing, the gates, and the entry the gates then have to remember. One function.

`execution/policy.py` exists because two implementations of the entry policy would drift
invisibly. This exists for the same reason and a worse failure: the backtest sized an
intent, gated it and told the risk engine about the entry, and the venue path sized an
intent, gated it and told the risk engine nothing. Nothing failed. The cluster cap counted
zero entries on testnet, the drawdown peak never rose, and every one of those limits looked
like a limit that was simply not being hit.

So the sequence is one function that the backtest engine, the paper session (through the
backtest engine) and the venue session all call: mark equity, size, gate, and record the
entry that survived both. A third copy cannot drift because there is no third copy.

Pure of I/O beyond the risk engine's own persistence: no clock, no book, no venue. The
caller supplies `now_ms`, the equity and the account view, which is what lets a replay drive
it from replayed time and the live loop from the wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from liqfade.config.models import Settings
from liqfade.risk.engine import AccountView, EntryRequest, RiskEngine
from liqfade.sizing import LiquidityStats, size_intent
from liqfade.strategy.types import AssetMeta, Intent, Side

__all__ = ["GatedEntry", "gate_entry"]


@dataclass(frozen=True, slots=True)
class GatedEntry:
    """What to send, or which gate refused it.

    Args:
        size: coin units, rounded to the asset's `szDecimals`. Zero when refused.
        notional_usd: what sizing decided, USD. Zero when sizing itself refused.
        depth_cap_applied: whether resting depth was known and the depth cap was in force.
        gate: `"sizing"` or `"risk"`, naming which layer refused. None when allowed.
        reason: the cap, floor or gate that refused, for the counters and Telegram.
    """

    size: float
    notional_usd: float
    depth_cap_applied: bool
    gate: str | None = None
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None


def gate_entry(
    intent: Intent,
    *,
    now_ms: int,
    equity: float,
    stats: LiquidityStats,
    meta: AssetMeta,
    settings: Settings,
    risk: RiskEngine,
    coin_exposure_usd: float,
    total_exposure_usd: float,
    open_sides: Mapping[str, Side],
    coin_last_data_ms: Mapping[str, int],
    global_last_data_ms: int | None,
) -> GatedEntry:
    """Mark equity, size the intent, run every gate, and record what survives.

    Args:
        intent: the entry the strategy asked for.
        now_ms: decision time. Replayed time in the backtest, wall clock in the live loop.
        equity: live account equity, USD. Marked before the gates run, so the drawdown
            peak includes a gain the account has just made rather than lagging it by a
            trade.
        stats: the coin's traded volume and resting depth, for the sizing caps.
        meta: the coin's `szDecimals` and `maxLeverage`.
        settings: resolved config.
        risk: the engine whose state this both reads and moves.
        coin_exposure_usd: gross notional already open in this coin.
        total_exposure_usd: gross notional already open across every coin.
        open_sides: coin to the side currently held in it.
        coin_last_data_ms: coin to the last timestamp data arrived for it.
        global_last_data_ms: last timestamp any data arrived. None means never.

    Returns:
        A `GatedEntry`. `allowed` means `on_entry` has already been recorded against the
        cluster cap, so the caller places the order rather than deciding again.

    Note:
        The entry is recorded before it is sent, because that is the order the backtest has
        always used and the cluster cap is about the bet being taken rather than the ack
        coming back. An order the venue then refuses leaves one entry in the cluster window
        that no position matches, which tightens the cap for `cluster_window_s` and is the
        safe direction to be wrong in.
    """
    risk.on_equity(equity, now_ms)
    decision = size_intent(
        intent,
        equity=equity,
        stats=stats,
        meta=meta,
        settings=settings,
        coin_exposure_usd=coin_exposure_usd,
        total_exposure_usd=total_exposure_usd,
    )
    if decision.reason is not None:
        return GatedEntry(0.0, 0.0, False, "sizing", decision.reason)

    request = EntryRequest(
        coin=intent.coin,
        side=intent.side,
        notional_usd=decision.notional_usd,
        now_ms=now_ms,
    )
    verdict = risk.check_entry(
        request,
        AccountView(
            equity=equity,
            open_sides=open_sides,
            coin_last_data_ms=coin_last_data_ms,
            global_last_data_ms=global_last_data_ms,
        ),
    )
    if not verdict.allowed:
        assert verdict.reason is not None
        return GatedEntry(
            0.0, decision.notional_usd, decision.depth_cap_applied, "risk", verdict.reason
        )

    risk.on_entry(request)
    return GatedEntry(float(decision.size), decision.notional_usd, decision.depth_cap_applied)
