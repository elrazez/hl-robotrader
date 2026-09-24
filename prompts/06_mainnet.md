# Prompt 06: Phase 6 mainnet at minimum size

Owner notes: do not run this until Phase 5 has run at least one full week on
paper/testnet with zero reconciliation mismatches and zero unhandled exceptions. This
phase writes very little code; it is mostly verification, reporting, and a checklist
where every real-money step is yours, not the agent's. The agent cannot and must not
flip the live switch.

---

You are working in the liqfade repo. Read `docs/ROADMAP.md`
Phase 6, `docs/RUNBOOK.md`, and `docs/HYPERLIQUID.md` (fees and rate limit sections
again). The agent NEVER runs `liqfade live`, never edits `.env`, and never modifies
`config/mainnet.yaml` risk limits unless this prompt's owner explicitly typed new
values in the same message.

This session is **Phase 6: the preflight gate, live reporting, and the go-live
runbook**. Scope is exactly the four items below. Anything else you notice belongs in
`docs/DECISIONS.md` as a note, not in this phase's diff.

## Deliverables

1. **Preflight command.** `liqfade preflight` (new subcommand) runs read-only checks
   and prints PASS/FAIL per line, exit 0 only if all pass:
   - config: env resolves to mainnet cleanly; `live_trading_block_reason()` printed
     as information (preflight passes whether or not trading is enabled; it reports).
   - risk limits: mainnet.yaml values match the roadmap's conservative set
     (risk_frac 0.0025, max 2 positions, daily loss 0.02); WARN not FAIL if the owner
     has deliberately changed them (compare against the roadmap constants, print both).
   - keys: mainnet agent key present and signs a dummy payload locally (no network
     order); account address well-formed.
   - exchange: `userFees` fetched and printed; the cost model constants in config are
     compared against the live fee tier and a WARN printed on mismatch, with the
     instruction to update config from evidence.
   - clock: system time within 500ms of the HL server time (from a REST response
     header or an info timestamp).
   - state: reconciliation dry-run against `clearinghouseState` and `openOrders`
     reports zero unknown positions and orders on the mainnet account.
   - kill: `data/KILL` absent; watchdog unit file installed (on the VPS; skip with
     WARN locally); heartbeat file writable.
   - telegram: send a test alert and require a `/status` round trip within 60s
     (prints instructions and waits; `--no-telegram` skips with WARN).
   Tests: each check unit-tested with fakes, both pass and fail branches.
2. **Daily summary.** A scheduled task inside the live loop (fires at 00:05 UTC):
   yesterday's trade count, net PnL, fees, funding, hit rate, worst trade, halts, and
   reconcile mismatch count, posted to Telegram and written to
   `reports/live/daily/YYYY-MM-DD.md`. Reads only orders.sqlite and risk state.
3. **Weekly live-vs-model report.** `scripts/research/live_vs_model.py --week
   YYYY-WW`: maker fill rate achieved vs the backtest fill model's prediction on the
   same events, realized slippage on taker fills vs modeled, submit-to-ack latency
   distribution vs the 1000ms assumption, funding paid vs modeled. Writes
   `reports/live/weekly/YYYY-WW.md` ending with explicit "update these config values
   from evidence: ..." lines when measurements diverge from config by more than 20%.
   The roadmap's scaling rule is printed at the bottom of every weekly report: only
   scale after a full week inside the expected band, never after a good day.
4. **Runbook completion.** Extend `docs/RUNBOOK.md` with a "Go-live day" section: the
   exact ordered steps the OWNER performs (funding the account, setting
   `LIQFADE_ENV=mainnet` and `LIQFADE_LIVE_TRADING=1` in `/srv/liqfade/.env`, running
   `liqfade preflight`, starting `liqfade-live`, watching the first hour, the
   first-day checklist) and a "First two weeks" section: what to check daily
   (reconcile counter zero, daily summary sane), the acceptance criteria from the
   roadmap (two weeks, zero reconciliation mismatches, zero unhandled exceptions),
   and what stops the experiment (any unknown position, any unhandled exception ->
   kill, investigate, only restart after a written DECISIONS.md entry).

## How to work

- Print a short plan and wait for OK.
- Commit order: preflight + tests; daily summary + tests; weekly script + tests;
  runbook. `make lint test` green before each.
- All new code paths that touch the network are behind explicit commands the owner
  runs; nothing autonomous in this phase places or cancels anything.
- No new dependencies. No em dashes.

## Definition of done

`make lint test` green, zero skips. `liqfade preflight` runs locally and produces the
full checklist with sensible WARN/FAIL for a machine without mainnet keys. Owner-run
acceptance: preflight all-PASS on the VPS, then two weeks live at minimum size judged
by the roadmap criteria. End with the go-live owner checklist copied inline, and state
plainly that the final switch is three things only the owner touches: the two env
values and the absence of data/KILL.
