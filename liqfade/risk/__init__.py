"""Pre-trade gates and the small persisted risk state.

Stateless checks (daily loss, drawdown, consecutive losses, per-coin cooldowns, max
concurrent positions, stale data, reconnect storms, exchange rejects, kill switch)
over a state file at data/risk_state.json that is written on every change so a
restart cannot reset a limit. Some gates only block entries, some also flatten.
Governed by docs/STRATEGY_AND_RISK.md section 5 and built in Phase 4 of
docs/ROADMAP.md. Empty until then.
"""
