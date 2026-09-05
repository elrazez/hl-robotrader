# Prompt 03b: Q3 re-cut 1, the trigger gets the episode's own floors

Owner notes: paste into the Phase 3 research chat, or a fresh one. It runs two to three
hours against the lake at `/Volumes/<drive>/liqfade-data`. This consumes one of the
two re-cuts the preregistration allows. Nothing else changes.

---

You are working in the liqfade repo. Read `CLAUDE.md`, then `reports/research/SUMMARY.md`,
`reports/proxy_calibration.md` and the Q3 section of `reports/research/PREREGISTRATION.md`.

Q3 said no: the three-gate proxy fires 429,227 times a year against 2,617 qualifying
episodes, precision 0.01 at every preregistered threshold, and threshold 6 still fires
303,490 times. The gates work (recall 0.68); the trigger rate is the failure.

## The re-cut, logged before it runs

One change to the trigger definition, and it is the episode's own definition applied to the
trigger: an episode qualifies only above `$250k` notional in its window and a `30 bps`
move. The proxy has no floor at all, so it fires on a small spike on a thin coin at 3am,
and the strategy would never trade that. Give the trigger the same two floors, read from
the same config keys the episode detector reads, evaluated on the same 60s window and
only from data at or before the tick. Then extend the threshold grid to
`(4, 6, 10, 20, 40)`.

Before any code: a dated entry in `docs/DECISIONS.md` that says this is re-cut 1 of 2,
what was already seen (the numbers above), and what the trigger is after the change. Then
the code, in `scripts/research/q3_proxy.py` and wherever the proxy's trigger lives; the
feature functions in `liqfade/features/` are not touched. One commit for the log, one for
the code, `make lint test` green.

## Run

    uv run python scripts/research/q3_proxy.py --start 2025-09-01 --end 2026-08-30 \
        --data-dir "/Volumes/<drive>/liqfade-data"

The day checkpoints under `reports/research/q3_days/` are from the old trigger; delete that
directory first or the run answers the old question. Peak memory must stay under 3 GB;
report `/usr/bin/time -l`'s maximum resident set size.

## Report back

The calibration table at every threshold, gated and ungated, core+major and by tier, the
trigger count per year at each threshold, and the DECISION against the preregistered bar
(precision at or above 0.5 at recall at or above 0.5 on core+major). If precision stays
near zero at every threshold, say so plainly: the public proxy is dead and the live trigger
is cross-venue or a real-time liquidation source. Update the Q3 row of `SUMMARY.md` and the
`proxy_calibration.md` report. Leave `cascade.proxy_min` in config untouched unless a
threshold passes the bar.
