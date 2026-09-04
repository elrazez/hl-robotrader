"""In-memory row buffers, one per Parquet file the recorder is currently filling.

A buffer is keyed by (table, venue, date, hour), which is exactly one output file, so a
message that arrives at 10:59:59.9 and one that arrives at 11:00:00.1 land in different
buffers and neither file has to be rewritten. Rows are plain dicts in a list: appending
is what the WebSocket receive loop does inline, and anything with a lock or a queue in
that path would be paying for a problem the recorder does not have.

Flush happens when the hour has rolled over or the buffer reaches
`recorder.ring_buffer_rows`, whichever comes first, and on shutdown. Partitioning uses
`rx_ms` when the row has one, because that is when we learned about the event and it is
what makes the file layout match wall clock; `candles_1m` has no `rx_ms` and falls back
to `ts_ms`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# table, venue, date (YYYY-MM-DD), hour
PartitionKey = tuple[str, str, str, int]


def partition_of(ms: int) -> tuple[str, int]:
    """(date, hour) in UTC for a millisecond timestamp."""
    moment = datetime.fromtimestamp(ms / 1000.0, UTC)
    return moment.strftime("%Y-%m-%d"), moment.hour


class Buffers:
    """Per file row buffers with two flush triggers: row count and hour roll."""

    def __init__(self, max_rows: int) -> None:
        self.max_rows = max_rows
        self._rows: dict[PartitionKey, list[dict[str, Any]]] = {}

    def add(self, table: str, venue: str, row: dict[str, Any]) -> None:
        stamp = row.get("rx_ms") or row.get("ts_ms") or 0
        date, hour = partition_of(int(stamp))
        self._rows.setdefault((table, venue, date, hour), []).append(row)

    def pending_rows(self) -> int:
        return sum(len(rows) for rows in self._rows.values())

    def ready(self, now_ms: int) -> list[PartitionKey]:
        """Keys whose file should be written now: full, or from an hour that has ended."""
        today, this_hour = partition_of(now_ms)
        return [
            key
            for key, rows in self._rows.items()
            if len(rows) >= self.max_rows or (key[2], key[3]) != (today, this_hour)
        ]

    def take(self, key: PartitionKey) -> list[dict[str, Any]]:
        """Remove and return one buffer. The caller owns the rows from here on."""
        return self._rows.pop(key, [])

    def partitions(self) -> list[PartitionKey]:
        """Every buffered key, in a stable order, as a snapshot safe to iterate."""
        return sorted(self._rows)

    def peek(self, key: PartitionKey) -> list[dict[str, Any]]:
        """A snapshot of one buffer's rows, leaving them buffered.

        The pair of this and `drop_written` is how the recorder writes without owning the
        rows: nothing leaves the buffer until the Parquet file is on disk.
        """
        return list(self._rows.get(key, ()))

    def drop_written(self, key: PartitionKey, count: int) -> None:
        """Forget the first `count` rows of one buffer, which have just been written.

        Rows that arrived while the write was in flight were appended after them, so
        dropping from the front never discards a row that was not written.
        """
        rows = self._rows.get(key)
        if rows is None:
            return
        del rows[:count]
        if not rows:
            del self._rows[key]

    def take_all(self) -> list[tuple[PartitionKey, list[dict[str, Any]]]]:
        """Drain everything, for shutdown."""
        taken = sorted(self._rows.items())
        self._rows = {}
        return taken
