# Prompt 07: Phase 7, how every change happens from now on

Owner notes: this is not a one-shot build prompt. It is the standing process for
changing a live trading system, plus the backlog. Reuse this file every time: fill in
the "This iteration" block at the bottom and paste the whole thing to the agent.

---

You are working in the liqfade repo. Read `CLAUDE.md` in
full, then `docs/DECISIONS.md` and the latest `reports/live/weekly/` report. The
discipline below overrides any instinct to just tweak a number.

## The iteration law

One change at a time, and every change walks this ladder in order. No step is
skippable, including for "obviously good" ideas.

1. **Research question first.** Write the hypothesis as a measurable question in a new
   script `scripts/research/q7plus_<slug>.py` (same conventions as Phase 3: event
   study on the lake, gross AND net of costs, bootstrap CI, DECISION line). If the
   study says no, the idea dies here and gets one line in docs/DECISIONS.md. Most
   ideas should die here.
2. **Config-flagged implementation.** The change lands behind a config value that
   defaults to the CURRENT behavior. New params go in models.py + default.yaml with
   unit and rationale comments like every other. New entry conditions land as
   `ConfluenceRule` classes in `strategy/rules.py` (one class, one config section,
   one test), not as edits to the engine.
3. **Backtest.** Full-history run plus holdout plus the lagged rerun, config hash
   recorded, `reports/.n_trials` increments (deflated Sharpe stays honest about how
   many things have been tried). The change must improve net metrics on the holdout,
   not just in-sample.
4. **Paper A/B for one week.** Run paper with the flag on; compare against the same
   week's live (flag off) via `scripts/research/paper_vs_backtest.py` conventions.
5. **Live, small.** Enable the flag on mainnet only after 1 through 4 are written up
   in docs/DECISIONS.md with the evidence linked. Watch one week of weekly reports.
6. **Scaling** follows the standing rule only: a full week where live PnL, fill rate
   and slippage sit inside the backtest's expected band, then the owner may raise
   risk_frac one step or deposit. Never scale after a good day. Scaling is an owner
   action, never an agent action.

Rules that never relax: `make lint test` green before every commit; no lookahead; all
results net of costs; no new dependencies without a DECISIONS.md entry; the agent
never touches mainnet config risk limits, `.env`, or `liqfade live`; anything touching
execution semantics gets tested against the FakeHLClient first and on testnet second.

## The backlog, in rough priority order

Each entry is a candidate research question, not a promise. Pick ONE per iteration.

1. Cross-venue liquidation weights: once the recorder has 8 or more weeks, redo Q4
   properly and fit the venue upweights that DATA_SOURCES marked "revisit".
2. Funding and OI regime filter: does skipping entries when funding is extreme or OI
   is draining improve the net tail? (Extends Q6.)
3. Book-imbalance exhaustion timing: does adding depth imbalance to the exhaustion
   conditions improve entry price versus the current definition?
3b. Zone library extensions (only if Q7 was a yes): volume-profile nodes, liquidation
   cluster levels from `fills_full` (where past cascades died is itself a level),
   higher-timeframe zones. Each as its own ConfluenceRule through the full ladder.
4. Coin-tier parameter sets: do minor-tier coins want different k_target/k_stop, or
   should they be dropped entirely?
5. Trailing exits: replace the fixed target with a trail after 50% reversion is
   captured. Measure give-back versus extra capture.
6. Partial-liquidation rhythm (Q5 follow-up): if the 30s chunk rhythm is real, wait
   for the last chunk instead of the generic exhaustion signal on >100k cascades.
7. Momentum variant for market-wide sweeps: the BTC-cascading filter currently skips
   these; is there a separate momentum trade there? (Separate strategy flag, off by
   default, full ladder.)
8. Paid HL liquidation stream: only if research shows the proxy misses material
   cascades AND budget allows; the LiquidationFeed interface from Phase 2 is the
   plug point. Requires an owner decision on cost first.
9. Maker exit for the target leg: is the Gtc reduce-only target getting adverse
   selection that an adaptive repricing exit would avoid? Measure from fills data.

## This iteration

Candidate number: ___
Hypothesis (one sentence): ___
Success metric and threshold: ___
Owner notes: ___

Work the ladder from step 1. Stop and report at the end of whichever step the evidence
kills the idea, or at the end of step 3 if it survives, with the paper A/B plan as the
owner checklist. Print a short plan and wait for OK before starting.
