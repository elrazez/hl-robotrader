"""Pure feature functions computed from market data.

Liquidation intensity and z-scores, liquidation imbalance, the Hyperliquid forced
flow proxy, returns, realized volatility, displacement from the pre-cascade VWAP
anchor, CVD and its slope, spread, depth and book imbalance, open interest delta,
funding and premium, plus market-wide BTC context. Every function is sync, pure and
takes numpy or polars inputs, and may only read data with rx_ms strictly before the
decision timestamp. Governed by docs/STRATEGY_AND_RISK.md section 2 and built in
Phase 3 of docs/ROADMAP.md. Empty until then.
"""
