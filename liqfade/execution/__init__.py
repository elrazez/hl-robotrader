"""Hyperliquid client wrapper, order manager and reconciliation.

Holds the signer (the only module that ever touches the agent private key), price
and size rounding, the per-cloid order state machine persisted to data/orders.sqlite,
idempotent submit, repricing, partial fill handling, TP/SL grouping, the rate limit
and action budget accounting, the watchdog and the reconciliation loop against
clearinghouseState and openOrders. Governed by docs/HYPERLIQUID.md and built in
Phase 5 of docs/ROADMAP.md. Empty until then.
"""
