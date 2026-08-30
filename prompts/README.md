# Prompts

One file per phase of `docs/ROADMAP.md`. Paste the part below the `---` line of each file
into a fresh Claude Code session started in the repo root. Run them in order; the
exceptions are noted below.

| File | Phase | Builds | Needs first |
|---|---|---|---|
| `00_scaffold.md` | 0 | Skeleton, config, control plane, CLI, panel, VPS scripts | nothing (done) |
| `01_recorder.md` | 1 | 24/7 market data recorder + Panel v1 | nothing; VPS optional |
| `02_backfill.md` | 2 | Historical data download (S3, Tardis, candles) | AWS keys, billing alarm |
| `03_research.md` | 3 | Feature functions + the six research questions | Phase 2 data on disk |
| `04_backtest.md` | 4 | Backtester, strategy v0, sizing, risk + Panel v2 | Phase 3 said "yes" |
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
