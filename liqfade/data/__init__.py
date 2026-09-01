"""Canonical data schemas, Parquet loaders and historical ingestion.

Holds the Parquet schema definitions shared by the recorder and the backtester, the
symbol mapping between Hyperliquid coins and venue symbols, a LiquidationFeed
interface so a paid Hyperliquid stream can be plugged in later, and the ingestion
jobs for Hyperliquid requester-pays S3, the Hyperliquid L2 archive, Tardis free
first-of-month days and candleSnapshot backfill. Governed by docs/DATA_SOURCES.md
and built in Phase 2 of docs/ROADMAP.md. Empty until then.
"""
