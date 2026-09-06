# Prompts

One file per phase of `docs/ROADMAP.md`. Paste the part below the `---` line of each file
into a fresh Claude Code session started in the repo root. Run them in order; the
exceptions are noted below.

| File | Phase | Builds | Needs first |
|---|---|---|---|
| `00_scaffold.md` | 0 | Skeleton, config, control plane, CLI, panel, VPS scripts | nothing (done) |
| `01_recorder.md` | 1 | 24/7 market data recorder + Panel v1 | nothing; VPS optional |
| `01b_recorder_hardening.md` | 1 | Recorder fixes from the 2026-09-04 review; deploy before Phase 5 | 02d and 02f committed |
| `02_backfill.md` | 2 | Historical data download (S3, Tardis, candles) | AWS keys, billing alarm |
| `02b_compaction.md` | 2 | Streaming compaction + `liqfade compact`, run over the year | 02 data on disk (done 2026-09-04) |
| `02c_candles.md` | 2 | Tardis side-table fix + re-pull, then `liqfade derive candles`: the year of 1m bars from `trades` | 02b |
| `02d_rederive.md` | 2 | Streaming `backfill rederive` in exchange order, Tardis HL trades dropped, candles rebuilt | 02c; `fills_full` compaction done |
| `02f_lake_hardening.md` | 2 | Ingestion and lake fixes from the 2026-09-04 review | 02d committed, both its runs finished |
| `02e_quality_gate.md` | 2 | Full data quality report, side rule by month, cleanup checklist | 02d, 02f |
| `03_research.md` | 3 | Feature functions + the six research questions | 02e says no unexplained holes |
| `03b_q3_recut.md` | 3 | Q3 re-cut 1: the proxy trigger gets the episode floors, thresholds to 40 | 03 done |
| `03c_realtime_liquidations.md` | 3 | Research only: can the live bot see HL liquidations in real time, and at what cost | 03 done |
| `04_backtest.md` | 4 | Backtester, strategy v0, sizing, risk + Panel v2; trigger source is a config switch | Phase 3 done; owner overrode stop rule 5 on 2026-09-06 |
| `04b_backtest_streaming.md` | 4 | The backtester reads the lake a day at a time, caches the feature grid, replays only cascade windows | 04 done; the year run does not fit in 8 GB |
| `05_execution.md` | 5 | Live loop, paper mode, testnet execution, Telegram bot + Panel v3 | Phase 4 done, testnet keys |
| `06_mainnet.md` | 6 | Mainnet go-live at minimum size, reporting | Phase 5 ran clean 1 week |
| `07_iterate.md` | 7 | The process for every change after go-live | Phase 6 live |

Ordering rules from the roadmap:

- Phase 1 and 2 are independent of each other and can run in either order or in parallel.
- Phase 3 needs Phase 2 data. It only needs Phase 1 if research question 4 is attempted.
- Phase 3 is the go/no-go gate. If the edge is not there after fees, the project stops
  and that is a valid outcome. Do not run 04 until 03's summary says to continue.
- Do not run 06 until Phase 5 has run at least one week with zero reconciliation
  mismatches.
- `02f_lake_hardening.md` runs after 02d's two long jobs finish, then `02e_quality_gate.md`.
  `01b_recorder_hardening.md` runs any time after 02f and must be deployed before Phase 5.

Rules for whoever runs these prompts (human or agent):

1. The agent must read `CLAUDE.md` in full first, then the docs named at the top of the
   prompt. Every prompt repeats this because it is that important.
2. Each prompt starts with the agent printing a short plan and waiting for an OK.
3. The prompts pre-answer design questions in a "Decisions already made" section. The
   agent follows those instead of asking. If something is genuinely not covered, the
   agent picks the simplest option consistent with CLAUDE.md, notes it in
   `docs/DECISIONS.md`, and keeps going.
4. The only reason to stop mid-prompt is a missing credential, account or dataset. Then
   the agent prints an owner checklist and stops. Never mock around missing keys or data.
5. `make lint test` green before every commit. Conventional commit messages.
