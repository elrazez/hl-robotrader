# Decision log

Append-only. One entry per decision that a future reader would otherwise question. Format: date, decision, why, what would reverse it.

## 2026-08-30

**Custom engine over NautilusTrader / Hummingbot / freqtrade.** Edge is in custom cross-venue liquidation features that these frameworks do not model natively; Nautilus is in a v1 to v2 migration in 2026; a small custom codebase is easier for the owner to reason about. Reverse if execution quality (fill rate, reconciliation bugs) becomes the bottleneck; `strategy/` stays framework-agnostic to keep that door open.

**Coinglass rejected.** Hobbyist tier ($29) serves only 4h and coarser history; minute-level needs the $299 tier and gives 6 days. Not useful for scalp backtests. Reverse only if budget rises and the Standard tier's 90-day 15-minute history becomes relevant for a slower variant.

**Tardis subscription rejected, free first-of-month days used.** Subscriptions are far over budget. The free days give real cross-exchange liquidation samples for validation. Reverse never for v0.

**HL real-time liquidations via proxy plus cross-venue feeds.** The free public HL trades feed does not flag liquidations. Paid streams exist (Quicknode, Hydromancer, Dwellir, 0xArchive, PurrData). For v0 we use Bybit (complete), Binance and OKX (throttled) plus an HL-native forced-flow proxy calibrated against exact S3 liquidations. Reverse if research shows the proxy is poor and cross-venue feeds do not predict HL reversion; then budget a paid HL stream.

**Historical HL data from requester-pays S3 and Hydromancer Reservoir.** Both are egress-only cost. `hyperliquid-data` PyPI tool used for cost estimates. Reverse if egress exceeds $15 for the initial pull; then narrow the coin set.

**Hosting in Tokyo.** HL validators and API run in AWS ap-northeast-1; a distant region adds about 200ms. A small Tokyo VPS fits the budget. Reverse never.

**Cross margin by default, isolated as config option.** Simpler order flow and no margin transfers; strict exposure caps and stops prevent account-level liquidation. Reverse if the owner wants hard per-position loss isolation at the cost of capital efficiency.

**Sizing driven by equity fraction and liquidity caps, leverage as an output.** Owner instruction: capital scales and leverage does not matter, volume and size do.

**Backtest latency default 1.0s.** Independent Tokyo measurement in March 2026 showed about 0.9s median order-to-fill despite official 0.2s docs. Reverse when live measurements say otherwise.

## 2026-08-30 (Phase 0 scaffold)

**argparse over typer or click for the CLI.** The whole surface is ten subcommands with a
handful of flags. argparse is in the standard library, so the trading process carries one
fewer dependency and one fewer thing to audit. Reverse if the CLI grows nested command
groups or needs shell completion badly enough to pay for a dependency.

**Hand-rolled config layering over pydantic-settings.** `liqfade config show` and the
panel settings page both have to tell the owner where a value came from (default file,
environment file, overrides.yaml, env var). pydantic-settings resolves its sources
internally and does not expose that provenance. So the loader deep-merges the layers
itself, recording the source of every leaf, and hands one dict to a plain pydantic model.
Reverse if pydantic-settings ever exposes per-field source information.

**Config YAML at the repo root, models in the package.** `config/default.yaml` sits beside
`data/` and `reports/`, matching how CLAUDE.md refers to `config/overrides.yaml` and
`config/mainnet.yaml` as root-relative paths, and keeping the file the panel writes out of
the installed package. The pydantic models live in `liqfade/config/`.

**Newline-delimited JSON framing on the control socket.** Messages are a command name and
a few arguments, so framing efficiency is irrelevant, and `nc -U /srv/liqfade/data/
control.sock` stays usable when the loop is misbehaving. Length-prefixed framing would buy
nothing here. Reverse if the control plane ever carries bulk data such as full order books.

**No authentication on the control socket.** Access control is the file: mode 0600, owned
by the service user. Anything that can open it already runs as the user that holds the
agent key, so a token on the wire would protect nothing. Reverse if a lower privileged
process ever needs read-only access.

**bcrypt for the panel password, not argon2.** Named in CLAUDE.md, one small wheel with no
build step on the VPS, and the real boundary is the network: the panel binds loopback and
is reached over Tailscale or an SSH tunnel. Against a single high-entropy password behind a
private network, argon2's better memory hardness does not change the outcome. Reverse if
the panel is ever exposed more widely, which it should not be.

**Panel sessions signed with itsdangerous, secret generated per process by default.** A
restart invalidating sessions is the safe default; `LIQFADE_PANEL_SESSION_SECRET` opts into
persistence. Reverse never for v0.

**/api/state reads data/state.json, never the control socket.** A slow HTTP client must not
be able to make the trading loop wait. A snapshot older than `panel.state_stale_s` returns
503 rather than a warning field, because every number on that page is a trading number and
quietly showing stale positions is worse than an obvious outage.

**Python pinned to 3.12 via .python-version.** pyarrow ships no 3.14 wheels yet and uv
otherwise tries to build it from source. Reverse once pyarrow publishes 3.14 wheels.

**Integration tests deselected, not skipped.** `-m "not integration"` in addopts, so a
clean `make test` reports zero skipped tests, which is the Phase 0 definition of done.

**deploy.sh restarts only services whose unit file changed.** Restarting the trading loop
because the panel's unit was edited is a surprise nobody wants at 3am. Code-only changes
need an explicit `systemctl restart`, which the script prints.

**Tailscale installed but not brought up by the bootstrap.** `tailscale up` needs an
interactive auth URL, and a bootstrap script should not decide to open a tunnel unattended.
The script prints the command.

## 2026-08-30 (cross-exchange liquidation history survey)

**No purchase of Binance/Bybit/OKX liquidation history for v0.** Surveyed Tardis
(https://docs.tardis.dev/faq/billing-and-subscriptions, https://tardis.dev/,
https://docs.tardis.dev/historical-data-details/binance-futures,
https://docs.tardis.dev/historical-data-details/bybit), CryptoStruct
(https://cryptostruct.com/pricing, https://cryptostruct.com/exchanges,
https://cryptostruct.com/download), Hyblock (https://docs.hyblockcapital.com/liquidation),
Velo (https://docs.velo.xyz/api, https://docs.velo.xyz/api/data), Coinglass
(https://www.coinglass.com/pricing), Amberdata
(https://blog.amberdata.io/accessing-crypto-liquidations-data-for-research-and-visualization),
Binance's public archive (https://data.binance.vision/, S3 listing under
data/futures/um/daily/ and data/futures/cm/daily/liquidationSnapshot/), and the
Binance developer forum (https://dev.binance.vision/t/is-there-historical-data-for-forceorder-endpoint/8362).
Finding: Binance and OKX have only ever published throttled streams, so no vendor holds
anything more complete than what the recorder captures; Bybit is complete only since
2025-02-25. Tardis has no one-off purchases and starts at $700/month; CryptoStruct sells
per instrument-day at EUR 1 but only carries liquidation events in 2026+ files. Summary
table in docs/DATA_SOURCES.md. Reverse if research question 4 shows cross-venue signals
matter; Hyblock 1-minute aggregates or a small CryptoStruct order are the affordable
paths then.
