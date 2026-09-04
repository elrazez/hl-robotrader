# Fixtures

`parquet/` is five minutes of the real lake, captured on 2026-08-30 with

    uv run liqfade record --minutes 5 --data-dir /tmp/fixture_capture

then filtered to BTC (all venues kept for `liquidations`, which is small) and rewritten
through `liqfade.recorder.writer.write_rows`, so the layout and the files are exactly what
the recorder produces. 112 KB. Tests that need real data read this rather than a mock,
which is how a schema change or a parser regression gets caught by `make test` instead of
by a research notebook three weeks later.

`asset_ctx` predates the `impact_bid_px` and `impact_ask_px` columns, which were added on
2026-09-05 so the archive's spread signal has the same home as the live one. The file was
rewritten with both null rather than recaptured; the next capture has them for real.

`liquidations` holds OKX rows only: Bybit had no liquidations in that five minute window
and Binance delivered nothing at all from the capture machine (see docs/DATA_SOURCES.md,
"What the wire actually sends"). Regenerate the fixture from a busier window, or from the
VPS, if a test ever needs multi-venue liquidations.

Raw WebSocket payloads for the parser tests live inline in `tests/test_recorder_parsers.py`
and are refreshed with `scripts/probe/ws_payloads.py`.
