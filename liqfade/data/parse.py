"""The three value conversions every ingestion parser needs, in one place.

Exchanges send numbers as strings and absences as `None` or `""`. Three near-identical
copies of these lived in `ingest/hl_s3.py`, `ingest/hl_archive_l2.py` and
`ingest/tardis_free.py`, which is three places for one rule to drift.

The rule they share: **an absent value is null, never zero.** A `0.0` price and a missing
price mean different things to every feature downstream, and a parser that quietly turns
one into the other is not recoverable from the lake afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

EMPTY = (None, "")


def to_float(value: Any) -> float | None:
    """Hyperliquid and Tardis send every number as a string. None and "" stay null."""
    return None if value in EMPTY else float(value)


def to_int(value: Any) -> int | None:
    return None if value in EMPTY else int(value)


def to_ms(value: Any) -> int | None:
    """Milliseconds UTC from either an epoch number or an ISO 8601 string.

    Envelope times in the Hyperliquid node data are ISO strings and fill times are epoch
    milliseconds, and both have to end up as the integer milliseconds the lake uses.
    `round`, not `int`: `timestamp() * 1000` lands on 1788609539999.9998 as often as on
    a whole number, and truncating that loses a millisecond at the boundary the whole
    ordering argument turns on.
    """
    if value in EMPTY:
        return None
    if isinstance(value, int | float):
        return round(value)
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return round(moment.timestamp() * 1000)
