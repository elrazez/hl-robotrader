"""The two Hyperliquid jobs that are not the fills pull: candles and the L2 archive.

Candles use a fake httpx transport, L2 the fake S3 client. Nothing here touches the
network and nothing spends.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from liqfade.data.ingest import hl_archive_l2, hl_candles
from liqfade.data.ingest.common import BackfillError, Manifest
from liqfade.data.schemas import SOURCE_HL_API, SOURCE_HL_ARCHIVE, validate_table
from liqfade.recorder.writer import DAY_SUFFIX
from tests.conftest import FakeS3

API = "https://api.hyperliquid.xyz"
DAY = date(2026, 9, 5)
FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
L2_LZ4 = FIXTURES / "l2book_sample.lz4"

# 2026-09-05T00:00:00Z, the first minute of the test day.
DAY_START_MS = 1_788_566_400_000


# --- candles: a fake API ------------------------------------------------------


def candle(coin: str, ts_ms: int) -> dict[str, object]:
    return {
        "t": ts_ms,
        "T": ts_ms + 59_999,
        "s": coin,
        "i": "1m",
        "o": "78862.0",
        "c": "78863.0",
        "h": "78870.0",
        "l": "78860.0",
        "v": "0.81106",
        "n": 21,
    }


class FakeApi:
    """Serves candleSnapshot from a per-coin minute series, and metaAndAssetCtxs.

    `oldest_ms` is where each coin's history begins, so a coin listed inside the
    requested range exercises the "the API runs dry" exit.
    """

    def __init__(self, coins: list[str], oldest_ms: int, *, delisted: list[str] | None = None):
        self.coins = coins
        self.oldest_ms = oldest_ms
        self.delisted = delisted or []
        self.windows: list[tuple[str, int, int]] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        import orjson

        body = orjson.loads(request.content)
        if body["type"] == "metaAndAssetCtxs":
            universe = [{"name": name} for name in self.coins]
            universe += [{"name": name, "isDelisted": True} for name in self.delisted]
            return httpx.Response(200, json=[{"universe": universe}, []])

        req = body["req"]
        self.windows.append((req["coin"], req["startTime"], req["endTime"]))
        start = max(int(req["startTime"]), self.oldest_ms)
        end = int(req["endTime"])
        minutes = range(start - start % 60_000, end + 1, 60_000)
        return httpx.Response(
            200, json=[candle(req["coin"], ms) for ms in minutes if ms >= self.oldest_ms]
        )


# --- candles ------------------------------------------------------------------


def test_a_day_range_covers_midnight_to_the_last_millisecond() -> None:
    start_ms, end_ms = hl_candles.day_bounds(DAY, DAY)
    assert start_ms == DAY_START_MS
    assert end_ms == DAY_START_MS + 86_400_000 - 1


def test_a_backwards_range_is_an_error() -> None:
    with pytest.raises(BackfillError, match="is before start"):
        hl_candles.day_bounds(date(2026, 9, 5), date(2026, 9, 1))


def test_the_default_coin_set_is_every_live_perp_not_the_universe() -> None:
    api = FakeApi(["BTC", "ETH", "SOL"], DAY_START_MS, delisted=["DEAD"])
    assert hl_candles.live_coins(api.client(), API) == ["BTC", "ETH", "SOL"]


def test_pagination_walks_backwards_from_the_end_of_the_range() -> None:
    api = FakeApi(["BTC"], 0)
    start_ms, end_ms = hl_candles.day_bounds(date(2026, 9, 1), DAY)
    list(hl_candles.walk_back(api.client(), API, "BTC", start_ms, end_ms, sleep_s=0))

    windows = [(s, e) for _, s, e in api.windows]
    assert windows[0][1] == end_ms
    # Each window ends strictly before the previous one began: strictly backwards.
    for (_, earlier_end), (later_start, _) in zip(windows[1:], windows, strict=False):
        assert earlier_end < later_start
    assert windows[-1][0] == start_ms  # the last page is clamped to the requested start


def test_pagination_stops_when_the_api_returns_nothing_older() -> None:
    """A coin listed mid-range must terminate, not ask for the same empty window forever."""
    start_ms, end_ms = hl_candles.day_bounds(date(2026, 8, 1), DAY)
    listed = end_ms - 2 * hl_candles.PAGE_MS
    api = FakeApi(["NEW"], listed)
    pages = list(hl_candles.walk_back(api.client(), API, "NEW", start_ms, end_ms, sleep_s=0))
    assert 0 < len(pages) <= 3
    assert len(api.windows) == len(pages) + 1  # one extra call, which came back empty


def test_a_page_never_returns_more_than_the_api_limit() -> None:
    api = FakeApi(["BTC"], 0)
    start_ms, end_ms = hl_candles.day_bounds(date(2026, 9, 1), DAY)
    page = next(hl_candles.walk_back(api.client(), API, "BTC", start_ms, end_ms, sleep_s=0))
    assert len(page) <= hl_candles.MAX_CANDLES_PER_CALL


def test_candles_outside_the_requested_window_are_dropped() -> None:
    page = [candle("BTC", DAY_START_MS - 60_000), candle("BTC", DAY_START_MS)]
    rows = hl_candles.to_rows(page, DAY_START_MS, DAY_START_MS + 60_000)
    assert [row["ts_ms"] for row in rows] == [DAY_START_MS]


def test_rows_carry_the_coin_from_the_symbol_field_and_typed_numbers() -> None:
    row = hl_candles.to_rows([candle("BTC", DAY_START_MS)], 0, 10**14)[0]
    assert row["coin"] == "BTC"
    assert (row["o"], row["h"], row["l"], row["c"]) == (78862.0, 78870.0, 78860.0, 78863.0)
    assert row["n"] == 21


def test_estimate_only_prints_the_call_count_and_fetches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(["BTC", "ETH"], 0)
    assert hl_candles.run(api.client(), tmp_path, API, DAY, DAY, estimate_only=True) == 0
    out = capsys.readouterr().out
    assert "No egress cost" in out
    assert api.windows == []


def test_a_pull_writes_candles_and_records_each_coin(tmp_path: Path) -> None:
    api = FakeApi(["BTC", "ETH"], 0)
    assert hl_candles.run(api.client(), tmp_path, API, DAY, DAY, sleep_s=0) == 0

    paths = sorted((tmp_path / "parquet" / "candles_1m").glob("date=*/*.parquet"))
    assert paths
    for path in paths:
        arrow = pq.read_table(path, partitioning=None)
        validate_table("candles_1m", arrow)
        assert set(arrow.column("source").to_pylist()) == {SOURCE_HL_API}
    assert Manifest.load(tmp_path, hl_candles.JOB).done(f"BTC/{DAY:%Y%m%d}-{DAY:%Y%m%d}")


def test_a_second_run_skips_coins_already_pulled(tmp_path: Path) -> None:
    api = FakeApi(["BTC"], 0)
    hl_candles.run(api.client(), tmp_path, API, DAY, DAY, sleep_s=0)
    calls = len(api.windows)
    hl_candles.run(api.client(), tmp_path, API, DAY, DAY, sleep_s=0)
    assert len(api.windows) == calls


def test_coins_restrict_the_pull(tmp_path: Path) -> None:
    api = FakeApi(["BTC", "ETH", "SOL"], 0)
    hl_candles.run(api.client(), tmp_path, API, DAY, DAY, coins=["ETH"], sleep_s=0)
    assert {coin for coin, _, _ in api.windows} == {"ETH"}


def test_an_empty_universe_is_an_error(tmp_path: Path) -> None:
    api = FakeApi([], 0)
    with pytest.raises(BackfillError, match="empty universe"):
        hl_candles.run(api.client(), tmp_path, API, DAY, DAY, sleep_s=0)


def derived_day_file(data_dir: Path, day: date) -> Path:
    """Stand in for what `liqfade derive candles` leaves behind for a day it owns."""
    directory = data_dir / "parquet" / "candles_1m" / f"date={day.isoformat()}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hl_candles.VENUE}{DAY_SUFFIX}"
    path.write_bytes(b"")
    return path


def test_the_api_job_refuses_a_day_the_derivation_owns(tmp_path: Path) -> None:
    """Both land in the same partition and no reader filters `source`."""
    derived_day_file(tmp_path, DAY)
    api = FakeApi(["BTC"], 0)
    with pytest.raises(BackfillError, match="already holds derived bars"):
        hl_candles.run(api.client(), tmp_path, API, DAY, DAY, sleep_s=0)
    assert api.windows == []


def test_only_the_derived_days_of_a_range_are_skipped(tmp_path: Path) -> None:
    earlier = date(2026, 9, 4)
    derived_day_file(tmp_path, DAY)
    api = FakeApi(["BTC"], 0)
    assert hl_candles.run(api.client(), tmp_path, API, earlier, DAY, sleep_s=0) == 0

    written = {
        path.parent.name
        for path in (tmp_path / "parquet" / "candles_1m").glob("date=*/*.parquet")
        if not path.name.endswith(DAY_SUFFIX)
    }
    assert written == {f"date={earlier.isoformat()}"}


# --- l2 archive ---------------------------------------------------------------


def test_the_l2_key_is_the_documented_path() -> None:
    assert hl_archive_l2.object_key(DAY, 11, "BTC") == "market_data/20260905/11/l2Book/BTC.lz4"
    assert hl_archive_l2.hour_prefix(DAY, 0) == "market_data/20260905/0/l2Book/"
    assert hl_archive_l2.coin_of_key("market_data/20260905/11/l2Book/kPEPE.lz4") == "kPEPE"


def test_snapshots_parse_to_twenty_levels_per_side() -> None:
    from liqfade.data.ingest.common import read_lz4_lines

    rows = hl_archive_l2.parse_snapshots(read_lz4_lines(L2_LZ4), "BTC")
    first = [row for row in rows if row["ts_ms"] == 1_788_609_540_000]
    # The fixture holds 22 levels; only the top 20 are kept, as the recorder does.
    assert [row["level"] for row in first] == list(range(20))
    assert first[0]["bid_px"] == 78860.0
    assert first[0]["ask_px"] == 78862.0
    assert {row["rx_ms"] for row in rows} == {0}


def test_a_wrapped_payload_is_unwrapped_and_takes_the_coin_from_the_key() -> None:
    from liqfade.data.ingest.common import read_lz4_lines

    rows = hl_archive_l2.parse_snapshots(read_lz4_lines(L2_LZ4), "BTC")
    wrapped = [row for row in rows if row["ts_ms"] == 1_788_609_545_000]
    assert len(wrapped) == 1  # a one level book
    assert wrapped[0]["coin"] == "BTC"
    assert (wrapped[0]["bid_px"], wrapped[0]["ask_px"]) == (78850.0, 78855.0)


def test_an_unrecognised_payload_is_skipped_rather_than_guessed_at() -> None:
    assert hl_archive_l2.parse_snapshots([b'{"something":"else"}'], "BTC") == []


def test_a_snapshot_with_no_timestamp_is_skipped() -> None:
    assert hl_archive_l2.parse_snapshots([b'{"coin":"BTC","levels":[[],[]]}'], "BTC") == []


def test_the_l2_job_refuses_to_run_without_coins(tmp_path: Path) -> None:
    with pytest.raises(BackfillError, match="--coins is required"):
        hl_archive_l2.run(FakeS3({}), tmp_path, DAY, DAY, [])


def test_the_l2_job_pulls_only_the_named_coins(tmp_path: Path) -> None:
    payload = L2_LZ4.read_bytes()
    client = FakeS3(
        {
            hl_archive_l2.object_key(DAY, 11, "BTC"): payload,
            hl_archive_l2.object_key(DAY, 11, "DOGE"): payload,
        }
    )
    assert hl_archive_l2.run(client, tmp_path, DAY, DAY, ["BTC"]) == 0
    assert client.downloaded == [hl_archive_l2.object_key(DAY, 11, "BTC")]

    for path in sorted((tmp_path / "parquet" / "l2").glob("date=*/*.parquet")):
        arrow = pq.read_table(path, partitioning=None)
        validate_table("l2", arrow)
        assert set(arrow.column("source").to_pylist()) == {SOURCE_HL_ARCHIVE}


def test_the_l2_job_estimates_before_it_downloads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeS3({hl_archive_l2.object_key(DAY, 11, "BTC"): L2_LZ4.read_bytes()})
    assert hl_archive_l2.run(client, tmp_path, DAY, DAY, ["BTC"], estimate_only=True) == 0
    assert "estimated $" in capsys.readouterr().out
    assert client.downloaded == []


def test_the_l2_job_warns_when_a_coin_has_nothing_in_the_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hl_archive_l2.run(FakeS3({}), tmp_path, DAY, DAY, ["BTC"], estimate_only=True)
    assert "no objects for ['BTC']" in capsys.readouterr().out


def test_the_l2_job_deletes_the_raw_file_unless_keep_raw(tmp_path: Path) -> None:
    objects = {hl_archive_l2.object_key(DAY, 11, "BTC"): L2_LZ4.read_bytes()}
    hl_archive_l2.run(FakeS3(objects), tmp_path, DAY, DAY, ["BTC"])
    assert not list((tmp_path / "raw").rglob("*.lz4"))
    hl_archive_l2.run(FakeS3(objects), tmp_path / "keep", DAY, DAY, ["BTC"], keep_raw=True)
    assert list((tmp_path / "keep" / "raw").rglob("*.lz4"))
