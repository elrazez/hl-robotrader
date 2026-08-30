"""Telegram bot: outbound alerts and inbound commands.

Posts every intent, fill, exit, risk block, halt, reconnect and error, and accepts
/status, /pause, /resume, /flat, /kill and /risk from an allowlist of chat ids,
dispatching them through the control plane so there is exactly one implementation of
each command. Governed by CLAUDE.md section 5 and built in Phase 5 of
docs/ROADMAP.md. Empty until then.
"""
