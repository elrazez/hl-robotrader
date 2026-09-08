"""The address book: who trades the most notional on Hyperliquid, ranked, on disk.

Every public HL trade names its counterparties (`users: [buyer, seller]` on the `trades`
feed), and the positions poller can only ask `clearinghouseState` about addresses it
knows. This is that set, ranked by cumulative traded notional, because cascade fuel is
concentrated in large leveraged positions and a top-N-by-notional set carries most of it.

Written to `<data_dir>/hl_addresses.json` after every poll and read back at recorder
start, so a restart does not throw away weeks of ranking. The write is the same
temp-sibling plus `os.replace` as `control/state.py`: rsync and the panel read this
directory, and a half-written file must never be visible.

See docs/research/external_bots_scan.md section 1 for why the addresses come from the
tape rather than from a hardcoded list or a paid API.
"""

from __future__ import annotations

import os
from pathlib import Path

import orjson

from liqfade.log import get_logger

log = get_logger(__name__)

FILENAME = "hl_addresses.json"
# Addresses kept. 5,000 is nothing to hold (a few hundred KB on disk) and is far more
# than any poll will ever ask for, so the bound exists to stop a 24/7 process growing a
# dict forever, not to be tuned.
MAX_TRACKED = 5_000


class AddressBook:
    """Cumulative traded notional per address, with the time each was last seen.

    ponytail: cumulative and never decayed, so a permanently dormant whale holds its rank
    forever. That is deliberate (a whale who opened a position a week ago and has not
    traded since is exactly who the map is for), but it means the ranking drifts towards
    whoever was busy longest rather than whoever is heavy now. Upgrade, if the poll ever
    spends its budget on ghosts: weight by `last_ms`, or drop anything unseen for N days.
    """

    def __init__(self, max_tracked: int = MAX_TRACKED) -> None:
        self.max_tracked = max_tracked
        # address -> (cumulative notional USD, last seen ts_ms)
        self.entries: dict[str, tuple[float, int]] = {}

    def note(self, address: str, notional_usd: float, ts_ms: int) -> None:
        """Add one trade's notional to an address and stamp when it was seen."""
        total, _ = self.entries.get(address, (0.0, 0))
        self.entries[address] = (total + notional_usd, ts_ms)
        # Pruning normally happens on save, which is every poll. This is the case where
        # saves keep failing (a full disk is the usual cause) and nothing else would ever
        # bound the dict. Twice the bound, so this costs one sort per max_tracked trades.
        if len(self.entries) > self.max_tracked * 2:
            self._prune()

    def top(self, n: int) -> list[str]:
        """The `n` addresses by cumulative notional, largest first.

        Ties break on the address, so two addresses with the same notional are polled in
        the same order on every poll rather than in dict order.
        """
        ranked = sorted(self.entries.items(), key=lambda item: (-item[1][0], item[0]))
        return [address for address, _ in ranked[:n]]

    def _prune(self) -> None:
        keep = set(self.top(self.max_tracked))
        self.entries = {a: v for a, v in self.entries.items() if a in keep}

    def save(self, path: Path) -> None:
        """Write the book atomically, pruned to `max_tracked`."""
        self._prune()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(orjson.dumps({"addresses": self.entries}))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    @classmethod
    def load(cls, path: Path, *, max_tracked: int = MAX_TRACKED) -> AddressBook:
        """Read a book from disk. A missing or unreadable file is an empty book.

        Day one has no file at all, and a file truncated by a crash mid-write must not be
        the reason the recorder will not start: the cost of an empty book is that the
        ranking warms up again from the tape, which takes hours, not weeks of data.
        """
        book = cls(max_tracked=max_tracked)
        try:
            payload = orjson.loads(Path(path).read_bytes())
            book.entries = {
                str(address): (float(total), int(last_ms))
                for address, (total, last_ms) in payload["addresses"].items()
            }
        except FileNotFoundError:
            log.info("address_book_absent", path=str(path))
        except Exception as exc:
            log.warning(
                "address_book_unreadable", path=str(path), error=f"{type(exc).__name__}: {exc}"
            )
        book._prune()
        return book
