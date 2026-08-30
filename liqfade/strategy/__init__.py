"""Cascade detection, exhaustion detection and intent generation.

Turns feature values into Intent objects (side, coin, entry, target, stop, time stop,
urgency) plus an explain() mapping of the feature values that triggered them. This
package performs no I/O, is deterministic given its inputs, and is shared verbatim by
the backtester and the live loop; anything that needs a live-only branch belongs in
live/ instead. Governed by docs/STRATEGY_AND_RISK.md section 3 and built in Phase 4
of docs/ROADMAP.md. Empty until then.
"""
