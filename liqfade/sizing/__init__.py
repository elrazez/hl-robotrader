"""Intent to notional, given live equity, liquidity and risk config.

Applies the risk budget per trade, the volume and depth participation caps, the
per-coin and total exposure caps and the venue maximum leverage, takes the minimum,
rounds to the asset szDecimals and returns zero with a reason when any cap or the 10
USDC exchange minimum blocks the trade. Capital is never hardcoded and leverage is an
output, never an input. Governed by docs/STRATEGY_AND_RISK.md section 4 and built in
Phase 4 of docs/ROADMAP.md. Empty until then.
"""
