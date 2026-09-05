# Prompt 03c: can the live bot see Hyperliquid liquidations in real time?

Owner notes: a research task, no code. Paste into a fresh chat with the research skill on
top. It answers the question Phase 3 parked on: the edge is measured on Hyperliquid's own
liquidations, which the lake has from the S3 archive a day late, and the live bot needs
them within seconds. Report only. Budget context: the whole project runs under $50 a month
today; say what any option costs against that.

---

You are working in the liqfade repo. Read `CLAUDE.md`, `docs/HYPERLIQUID.md`,
`docs/DATA_SOURCES.md` and the "trigger" section of `reports/research/SUMMARY.md`.

Answer, from primary sources only (Hyperliquid's own docs and GitHub, the node repository,
the API reference, vendor documentation), with the URL and date of everything you cite:

1. **Public Hyperliquid feeds.** Which WebSocket subscriptions and Info endpoints exist
   today, and does any of them mark a fill or a trade as a liquidation, or expose the
   liquidation engine's actions, with what latency? Cover at least `trades`, `userFills`,
   `userEvents`, `explorerBlock`, `explorerTxs`, `activeAssetCtx`, `webData2`, and the
   explorer RPC. Note that `trades` carries both user addresses. Say precisely what the
   public `trades` feed lacks compared with the archive's `node_fills`, and whether the
   liquidator vault's `userFills` (public, no auth) catches only backstop liquidations or
   all of them.
2. **A non-validating Hyperliquid node.** What it produces (`node_fills`, `node_trades`,
   block time), the published hardware requirements, whether it can run in a region other
   than Tokyo for our purpose (seeing liquidations within a second or two, not trading
   from it), and the cheapest realistic monthly cost on AWS, Hetzner, Vultr or OVH that
   meets the requirements. Say whether a smaller machine than recommended is known to work.
3. **Third-party real-time sources.** Any service that streams Hyperliquid liquidations
   live (paid or free): Coinglass, Hypurrscan, Hyperdash, Tardis real-time, anything else
   found. For each: latency, completeness, price, terms that forbid bot use.
4. **What the lake already has that could substitute.** The recorder captures Bybit and
   OKX liquidations, Hyperliquid trades, bbo, l2 and `activeAssetCtx` at block cadence. Say
   what a detector could see in `activeAssetCtx` (open interest and mark divergence at
   block level, which the archive samples once a minute) that the Phase 3 proxy, built on
   the archive's cadence, could not. Do not run anything; this is a reading task.

Write `docs/research/hl_realtime_liquidations.md`: one table of options with latency,
completeness, monthly cost and a one-line verdict, then a recommendation in three
sentences. Commit it, one commit. Do not touch any other file.
