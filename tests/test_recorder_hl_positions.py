"""The Hyperliquid positions poller: the rows it writes, what it skips, what it costs.

The payloads below are real `clearinghouseState` responses, captured 2026-09-08 with
`scripts/probe/hl_positions.py` against the two counterparty addresses of the first trade
in `HL_TRADES`. They are embedded verbatim, as `ws_payloads.py` output is embedded in the
parser tests, because the field that the whole liquidation map hangs on (`liquidationPx`)
is null on three of these five positions and a hand-written payload would not have
guessed that.

`poll_once` takes an injectable client, so none of this needs the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import orjson

from liqfade.config import load_settings
from liqfade.config.models import HyperliquidVenue
from liqfade.data.schemas import SOURCE_RECORDER, rows_to_table, validate_table
from liqfade.recorder.addresses import AddressBook
from liqfade.recorder.streams.hl_positions import STREAM, HlPositionsPoller

VENUE_CFG: HyperliquidVenue = load_settings("paper", environ={}).venues.hyperliquid
CFG = VENUE_CFG.positions
INFO_BASE = VENUE_CFG.info_base

WHALE = "0x49dfb8ddbd3ba96c0699a3f120e71f2516c345d8"
ISOLATED = "0x3a62fea9cd2832f639f13d6a5bc5584f62c470e4"

# Five open positions, cross margin, three of them with no liquidation level at all.
WHALE_STATE: dict[str, Any] = {
    "marginSummary": {
        "accountValue": "157116.278824",
        "totalNtlPos": "151187.346804",
        "totalRawUsd": "14802.4691",
        "totalMarginUsed": "6424.461461",
    },
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "0.99782",
                "leverage": {"type": "cross", "value": 40},
                "entryPx": "78072.5",
                "positionValue": "77654.34368",
                "unrealizedPnl": "-248.03624",
                "returnOnEquity": "-0.1273574647",
                "liquidationPx": None,
                "marginUsed": "1941.358592",
                "maxLeverage": 40,
                "cumFunding": {"allTime": "-2423.002764", "sinceOpen": "0.0"},
            },
        },
        {
            "type": "oneWay",
            "position": {
                "coin": "ETH",
                "szi": "23.1496",
                "leverage": {"type": "cross", "value": 25},
                "entryPx": "2457.22",
                "positionValue": "56630.86648",
                "unrealizedPnl": "-252.79486",
                "returnOnEquity": "-0.1111017004",
                "liquidationPx": None,
                "marginUsed": "2265.234659",
                "maxLeverage": 25,
                "cumFunding": {"allTime": "-2071.924101", "sinceOpen": "0.0"},
            },
        },
        {
            "type": "oneWay",
            "position": {
                "coin": "INJ",
                "szi": "-611.9",
                "leverage": {"type": "cross", "value": 5},
                "entryPx": "6.36556",
                "positionValue": "3806.93585",
                "unrealizedPnl": "88.155266",
                "returnOnEquity": "0.1131620075",
                "liquidationPx": "234.8745004814",
                "marginUsed": "761.38717",
                "maxLeverage": 5,
                "cumFunding": {"allTime": "-69.487771", "sinceOpen": "-9.611364"},
            },
        },
        {
            "type": "oneWay",
            "position": {
                "coin": "HYPE",
                "szi": "151.87",
                "leverage": {"type": "cross", "value": 10},
                "entryPx": "82.9695",
                "positionValue": "12465.368104",
                "unrealizedPnl": "-135.217719",
                "returnOnEquity": "-0.1073106607",
                "liquidationPx": None,
                "marginUsed": "1246.53681",
                "maxLeverage": 10,
                "cumFunding": {"allTime": "90.231404", "sinceOpen": "1.097077"},
            },
        },
        {
            "type": "oneWay",
            "position": {
                "coin": "WCT",
                "szi": "-15870.0",
                "leverage": {"type": "cross", "value": 3},
                "entryPx": "0.037741",
                "positionValue": "629.83269",
                "unrealizedPnl": "-30.876758",
                "returnOnEquity": "-0.1546529036",
                "liquidationPx": "8.3520849527",
                "marginUsed": "209.94423",
                "maxLeverage": 3,
                "cumFunding": {"allTime": "-3.149254", "sinceOpen": "-1.111006"},
            },
        },
    ],
    "time": 1788874814783,
}

# One isolated position, whose `leverage.value` is well under the asset's `maxLeverage`.
ISOLATED_STATE: dict[str, Any] = {
    "marginSummary": {"accountValue": "8217.368935", "totalMarginUsed": "8217.368935"},
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "2.30382",
                "leverage": {"type": "isolated", "value": 15, "rawUsd": "-170980.662125"},
                "entryPx": "79401.0",
                "positionValue": "179198.03106",
                "unrealizedPnl": "-3727.58076",
                "returnOnEquity": "-0.3056636566",
                "liquidationPx": "75155.5999617147",
                "marginUsed": "8217.368935",
                "maxLeverage": 40,
                "cumFunding": {"allTime": "53.721209", "sinceOpen": "-3.85452"},
            },
        }
    ],
    "time": 1788874838688,
}

# A coin the account is flat in stays in assetPositions with szi "0.0", exactly as
# execution/hl.py::parse_clearinghouse_state already has to handle.
FLAT_STATE: dict[str, Any] = {
    "marginSummary": {"accountValue": "10.0"},
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "0.0",
                "leverage": {"type": "cross", "value": 20},
                "entryPx": None,
                "positionValue": "0.0",
                "liquidationPx": None,
                "marginUsed": "0.0",
                "maxLeverage": 40,
            },
        }
    ],
    "time": 1788874840000,
}


class FakeInfo:
    """Answers `clearinghouseState` per address and records the order it was asked in."""

    def __init__(
        self,
        states: dict[str, Any],
        *,
        failing: frozenset[str] = frozenset(),
        delay_s: float = 0.0,
    ) -> None:
        self.states = states
        self.failing = failing
        self.delay_s = delay_s
        self.asked: list[str] = []
        self.events: list[tuple[str, str]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = orjson.loads(request.content)
        address = body["user"]
        self.asked.append(address)
        self.events.append(("start", address))
        # A gathered poll would interleave two starts here; a delay also makes the wall
        # clock move between answers, which is what the rx_ms stamping test needs.
        await asyncio.sleep(self.delay_s)
        self.events.append(("end", address))
        if address in self.failing:
            return httpx.Response(500, text="nope")
        assert body["type"] == "clearinghouseState"
        return httpx.Response(200, json=self.states.get(address, {"assetPositions": []}))


def _poller(
    fake: FakeInfo,
    coins: list[str],
    *,
    addresses: list[str] | None = None,
    cfg: Any = CFG,
    book_path: Path | None = None,
) -> tuple[HlPositionsPoller, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    book = AddressBook()
    for rank, address in enumerate(reversed(addresses or list(fake.states))):
        book.note(address, float(rank + 1) * 1_000.0, 1)
    poller = HlPositionsPoller(
        cfg,
        lambda table, venue, row: rows.append(row) if table == "hl_positions" else None,
        book,
        INFO_BASE,
        book_path=book_path,
    )
    poller.set_universe(coins)
    return poller, rows


# --- the rows ------------------------------------------------------------------


async def test_one_row_per_open_universe_position() -> None:
    fake = FakeInfo({WHALE: WHALE_STATE})
    poller, rows = _poller(fake, ["BTC", "ETH", "HYPE"])
    async with fake.client() as client:
        assert await poller.poll_once(client) == 3  # INJ and WCT are not in the universe

    validate_table("hl_positions", rows_to_table("hl_positions", rows, SOURCE_RECORDER))
    by_coin = {row["coin"]: row for row in rows}
    assert set(by_coin) == {"BTC", "ETH", "HYPE"}
    assert by_coin["BTC"] | {"rx_ms": 0} == {
        "coin": "BTC",
        "ts_ms": 1788874814783,  # the response's own `time`
        "rx_ms": 0,
        "address": WHALE,
        "szi": 0.99782,
        "entry_px": 78072.5,
        "liquidation_px": None,
        "position_value_usd": 77654.34368,
        "margin_used_usd": 1941.358592,
        "leverage": 40.0,
        "leverage_type": "cross",
        "max_leverage": 40,
    }
    assert by_coin["BTC"]["rx_ms"] > 0  # a live poll, so it has a real receive time


async def test_a_short_keeps_the_sign_hyperliquid_sent() -> None:
    """A flipped sign would put the position on the wrong side of the map."""
    fake = FakeInfo({WHALE: WHALE_STATE})
    poller, rows = _poller(fake, ["INJ"])
    async with fake.client() as client:
        await poller.poll_once(client)
    assert [(row["coin"], row["szi"]) for row in rows] == [("INJ", -611.9)]


async def test_a_missing_liquidation_price_stays_null() -> None:
    """Three of these five real positions have none. A zero would bin them at price 0."""
    fake = FakeInfo({WHALE: WHALE_STATE})
    poller, rows = _poller(fake, ["BTC", "ETH", "INJ", "HYPE", "WCT"])
    async with fake.client() as client:
        await poller.poll_once(client)
    assert {row["coin"]: row["liquidation_px"] for row in rows} == {
        "BTC": None,
        "ETH": None,
        "HYPE": None,
        "INJ": 234.8745004814,
        "WCT": 8.3520849527,
    }


async def test_an_isolated_position_keeps_its_own_leverage_and_the_assets_cap() -> None:
    fake = FakeInfo({ISOLATED: ISOLATED_STATE})
    poller, rows = _poller(fake, ["BTC"])
    async with fake.client() as client:
        await poller.poll_once(client)
    (row,) = rows
    assert (row["leverage_type"], row["leverage"], row["max_leverage"]) == ("isolated", 15.0, 40)
    assert row["liquidation_px"] == 75155.5999617147


async def test_a_flat_position_is_not_a_row() -> None:
    """HL leaves a closed coin in assetPositions with szi 0.0; it is not a position."""
    fake = FakeInfo({WHALE: FLAT_STATE})
    poller, rows = _poller(fake, ["BTC"])
    async with fake.client() as client:
        assert await poller.poll_once(client) == 0
    assert rows == []


# --- the poll ------------------------------------------------------------------


async def test_only_the_top_max_addresses_are_asked_in_rank_order() -> None:
    """The budget is the reason for the cap, and the ranking is the reason for the book."""
    fake = FakeInfo({f"0x{n:02d}": {"assetPositions": []} for n in range(10)})
    cfg = CFG.model_copy(update={"max_addresses": 3})
    poller, _ = _poller(fake, ["BTC"], cfg=cfg)
    async with fake.client() as client:
        await poller.poll_once(client)
    assert fake.asked == ["0x00", "0x01", "0x02"]


async def test_the_addresses_are_asked_one_after_another_and_never_gathered() -> None:
    """A slow venue should stretch the poll, not burst 200 calls into one instant."""
    fake = FakeInfo({WHALE: WHALE_STATE, ISOLATED: ISOLATED_STATE})
    poller, _ = _poller(fake, ["BTC"])
    async with fake.client() as client:
        await poller.poll_once(client)
    assert fake.events == [
        ("start", WHALE),
        ("end", WHALE),
        ("start", ISOLATED),
        ("end", ISOLATED),
    ]


async def test_each_address_is_stamped_when_its_own_answer_arrived() -> None:
    """`rx_ms` is local receive time, and 200 addresses are walked one after another.

    Measured on the box 2026-09-08: a full poll spans 6.5 seconds, so stamping every row
    with the poll's start time dated the later ones up to that much *earlier* than they
    actually arrived. A receive time that precedes the receipt is lookahead, which is the
    one thing CLAUDE.md rule 1 does not tolerate, and the whole point of `hl_positions`
    is to be research input.
    """
    slow = FakeInfo({WHALE: WHALE_STATE, ISOLATED: ISOLATED_STATE}, delay_s=0.05)
    poller, rows = _poller(slow, ["BTC"])
    async with slow.client() as client:
        await poller.poll_once(client)

    by_address = {row["address"]: row["rx_ms"] for row in rows}
    assert by_address[ISOLATED] > by_address[WHALE], "the second answer arrived later"


async def test_the_poll_counts_as_one_message_however_many_rows_it_returned() -> None:
    fake = FakeInfo({WHALE: WHALE_STATE, ISOLATED: ISOLATED_STATE})
    poller, rows = _poller(fake, ["BTC", "ETH", "HYPE"])
    async with fake.client() as client:
        await poller.poll_once(client)
    assert len(rows) == 4
    (state,) = poller.feed_states(1788874814783)
    assert (state.name, state.msgs_1m) == (STREAM, 1)


async def test_an_address_that_fails_is_skipped_counted_and_does_not_stop_the_poll() -> None:
    fake = FakeInfo({WHALE: WHALE_STATE, ISOLATED: ISOLATED_STATE}, failing=frozenset({WHALE}))
    poller, rows = _poller(fake, ["BTC"])
    poller.stats[STREAM].connected = True  # as `run` leaves it after a good poll
    async with fake.client() as client:
        assert await poller.poll_once(client) == 1  # the isolated address still answered

    assert [row["address"] for row in rows] == [ISOLATED]
    (state,) = poller.feed_states(1788874814783)
    assert state.parse_errors_1h == 1
    assert state.connected is True  # one bad address is not a broken feed


async def test_a_poll_where_every_address_failed_is_a_disconnected_feed() -> None:
    fake = FakeInfo({WHALE: WHALE_STATE}, failing=frozenset({WHALE}))
    poller, _ = _poller(fake, ["BTC"])
    poller.stats[STREAM].connected = True
    async with fake.client() as client:
        assert await poller.poll_once(client) == 0
    assert poller.stats[STREAM].connected is False


async def test_an_empty_book_polls_nothing_and_leaves_the_feed_alone() -> None:
    """Day one: the book warms up from the tape, and no poll is not a failed poll."""
    fake = FakeInfo({})
    poller, _ = _poller(fake, ["BTC"], addresses=[])
    poller.stats[STREAM].connected = True
    async with fake.client() as client:
        assert await poller.poll_once(client) == 0
    assert fake.asked == []
    assert poller.stats[STREAM].connected is True


async def test_the_book_is_written_after_every_poll(tmp_path: Path) -> None:
    """It ranks weeks of tape. A restart must not throw that away."""
    path = tmp_path / "hl_addresses.json"
    fake = FakeInfo({WHALE: WHALE_STATE})
    poller, _ = _poller(fake, ["BTC"], book_path=path)
    async with fake.client() as client:
        await poller.poll_once(client)
    assert AddressBook.load(path).top(1) == [WHALE]


# --- being switched off --------------------------------------------------------


async def test_a_poller_that_will_not_poll_declares_no_stream() -> None:
    # Declared-but-idle would read as disconnected and page Telegram every ten minutes.
    cfg = CFG.model_copy(update={"enabled": False})
    poller = HlPositionsPoller(cfg, lambda *_: None, AddressBook(), INFO_BASE)
    assert poller.stats == {}
    assert poller.feed_states(0) == []
    stop = asyncio.Event()
    await asyncio.wait_for(poller.run(stop), timeout=1.0)  # returns at once, polls nothing


def test_the_config_carries_the_rate_budget_it_was_sized_against() -> None:
    """clearinghouseState weighs 2 of 1200/min per IP, so 200 a minute is one third."""
    assert CFG.enabled is True
    assert CFG.poll_s == 60
    assert CFG.max_addresses * 2 * (60 / CFG.poll_s) <= 1200 / 3
