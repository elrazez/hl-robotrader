"""The address book: what it ranks, what it survives, and what it refuses to grow into.

The book is the input to the positions poller, so a wrong ranking is not a cosmetic
bug: it decides whose liquidation prices end up on the map and whose do not. It also
lives for the life of the recorder process and is written to disk every poll, which is
why the bound and the corrupt-file path get tests of their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqfade.recorder.addresses import AddressBook


def test_top_ranks_by_cumulative_notional_not_by_the_last_trade() -> None:
    """A whale who traded ten times small outranks a tourist who traded once big."""
    book = AddressBook()
    for _ in range(10):
        book.note("0xwhale", 1_000.0, 1)
    book.note("0xtourist", 5_000.0, 2)
    book.note("0xminnow", 1.0, 3)
    assert book.top(3) == ["0xwhale", "0xtourist", "0xminnow"]


def test_top_returns_at_most_n_and_never_more_than_it_holds() -> None:
    book = AddressBook()
    book.note("0xa", 1.0, 1)
    book.note("0xb", 2.0, 1)
    assert book.top(1) == ["0xb"]
    assert book.top(50) == ["0xb", "0xa"]
    assert AddressBook().top(10) == []


def test_a_tie_breaks_on_the_address_so_the_poll_order_is_deterministic() -> None:
    book = AddressBook()
    book.note("0xb", 1.0, 1)
    book.note("0xa", 1.0, 1)
    assert book.top(2) == ["0xa", "0xb"]


def test_a_round_trip_keeps_the_notional_and_the_last_seen_stamp(tmp_path: Path) -> None:
    path = tmp_path / "hl_addresses.json"
    book = AddressBook()
    book.note("0xa", 3.0, 111)
    book.note("0xa", 4.0, 222)  # cumulative, and the newer stamp wins
    book.note("0xb", 1.0, 333)
    book.save(path)

    loaded = AddressBook.load(path)
    assert loaded.top(2) == ["0xa", "0xb"]
    assert loaded.entries["0xa"] == (7.0, 222)
    assert loaded.entries["0xb"] == (1.0, 333)


def test_saving_leaves_no_temp_file_beside_the_book(tmp_path: Path) -> None:
    """Same temp-sibling plus os.replace as control/state.py: a reader sees all or none."""
    path = tmp_path / "hl_addresses.json"
    book = AddressBook()
    book.note("0xa", 1.0, 1)
    book.save(path)
    book.save(path)  # and again, over an existing file
    assert [p.name for p in tmp_path.iterdir()] == ["hl_addresses.json"]


def test_the_book_is_bounded_and_the_tail_is_what_goes(tmp_path: Path) -> None:
    """5000 addresses is nothing to hold; an unbounded dict in a 24/7 process is not."""
    path = tmp_path / "hl_addresses.json"
    book = AddressBook(max_tracked=10)
    for n in range(100):
        book.note(f"0x{n:03d}", float(n), n)
    book.save(path)

    loaded = AddressBook.load(path, max_tracked=10)
    assert len(loaded.entries) == 10
    assert loaded.top(1) == ["0x099"]  # the largest survived
    assert "0x000" not in loaded.entries  # the tail did not


def test_noting_far_past_the_bound_prunes_without_waiting_for_a_save() -> None:
    """A save that keeps failing (a full disk) must not turn the book into the leak."""
    book = AddressBook(max_tracked=10)
    for n in range(500):
        book.note(f"0x{n:03d}", float(n), n)
    assert len(book.entries) <= 20
    assert book.top(1) == ["0x499"]


@pytest.mark.parametrize("content", [None, b"", b"{not json", b'{"addresses": 7}'])
def test_a_missing_or_unreadable_book_loads_as_empty(tmp_path: Path, content: bytes | None) -> None:
    """Day one has no file, and a half-written one must not stop the recorder starting."""
    path = tmp_path / "hl_addresses.json"
    if content is not None:
        path.write_bytes(content)
    book = AddressBook.load(path)
    assert book.entries == {}
    book.note("0xa", 1.0, 1)  # and it is a working book, not a broken one
    assert book.top(1) == ["0xa"]
