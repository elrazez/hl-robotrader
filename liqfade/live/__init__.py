"""The trading loop that wires every other package together.

Owns the process: recorder feeds into ring buffers, a feature tick every 250ms,
strategy intents, sizing, the risk gate, the order manager, the heartbeat file and
the control plane server. This is the only place a live-only branch may exist.
Governed by docs/STRATEGY_AND_RISK.md and docs/HYPERLIQUID.md, and built in Phase 5
of docs/ROADMAP.md. Empty until then.
"""
