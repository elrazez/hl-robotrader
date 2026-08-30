"""Event-driven replay engine, fill model, cost model and metrics.

Replays the canonical Parquet tables at native granularity on the same 250ms decision
cadence as live, through the same features, strategy, sizing and risk code, with a
latency model, separate maker and taker fill models, fees and funding on every
number, walk-forward and holdout validation, and a one page markdown report per run
under reports/. Governed by docs/STRATEGY_AND_RISK.md section 6 and built in Phase 4
of docs/ROADMAP.md. Empty until then.
"""
