# Prompt 04d: `liqfade lab`, the terminal workbench

Owner notes: the owner wants to run backtests extensively before going live without asking
for a code change each time. That means a tool that manages strategy configs, launches runs
over short ranges and then the year, shows progress, and compares results. It has to work
on the laptop and over SSH on the VPS alike, so it is a terminal UI with real widgets
rather than a web page. It never runs the engine in process: a crash in the tool must not
kill a run, and a run must survive the tool being closed. Check
`pgrep -f "liqfade backtest"` before anything touches the lake, and never run two at once;
this machine has 8 GB.

---

You are working in the liqfade repo. Read `CLAUDE.md`, then `docs/ROADMAP.md` Phase 4,
`liqfade/cli.py` (`cmd_backtest`, `cmd_watch`), `liqfade/backtest/progress.py` and
`watch.py`, `liqfade/backtest/report.py` (`report.md`, `summary.json`, `trades.parquet` per
run under `reports/<stamp>_<hash>/`), `liqfade/config/models.py` and `loader.py` (the
`Settings` model, the layer order, the field descriptions), `config/backtest/v0.yaml` and
`v1_confluence.yaml`, `tests/test_backtest_progress.py` and `tests/test_cli.py`.

Work in a git worktree so the main checkout is left alone:

    git worktree add ../hl-robotrader-lab -b 04d-lab main

Develop against `tests/fixtures` (the fixture lake: coin BTC, day 2026-08-30), which needs
no drive. The real lake may be busy with a year run from another session.

## Decisions already made

1. **The lab is a client of the CLI and of the files it writes.** It never imports the
   engine to run a backtest. It spawns `liqfade backtest` as a subprocess with its own
   `--progress-file` and `--out`, so a crash in the lab never kills a run and a run keeps
   going when the lab is closed.
2. **Textual**, in a new optional dependency group `lab` in `pyproject.toml`, pinned to a
   major. `make setup` already runs `uv sync --all-extras`, so it installs.
3. **Nothing in the lab can start live trading or touch `config/mainnet.yaml`.** Nothing in
   `strategy/`, `features/` or `backtest/` changes behaviour.
4. **One lake job at a time**, enforced by the lab's own queue and by `pgrep` before every
   start. Queued runs start when the lake is free; cancel sends SIGTERM.
5. **The edit form is generated from the `Settings` model**, never listed. A knob added to
   the model appears in the lab with no lab change, and a test pins that.

## Screens, in this order, each its own commit

1. **Configs.** Lists `config/backtest/*.yaml`: name (from a `# name:` header line, else the
   filename), notes (`# notes:`), modified time, the keys it overrides. View, duplicate,
   edit, save, delete (asks twice). The form covers the sections a backtest reads:
   `features`, `cascade`, `exhaustion`, `intent`, `strategy.rules`, `filters`, `sizing`,
   `risk`, `backtest`. Each field is typed from the model, validated by the model on save,
   and an invalid value shows the validator's message in red. The writer emits only
   overridden keys, each with the model's description as a comment, plus the name and notes
   header, so a saved file reads like the hand-written ones.
2. **Run.** Pick a config, coins, a window from presets or typed dates, equity, latency, the
   lagged rerun, the cache. Show the resolved command before launch. Queue, progress panel
   through `backtest/watch.py`'s `render`, a `run.log` beside the report.
3. **Results.** The runs under `reports/` from `summary.json`: config name, window, coins,
   trades, net, hit rate, worst drawdown, holdout net, lagged net, wall clock, finished or
   not. Sort and filter. Open renders `report.md` and the funnel per rule. Two selected runs
   go side by side with the diff of their configs. Promote reruns a short run's config and
   coins over the year.
4. **Status.** Read-only. `data/state.json` and recorder health when a live or recorder
   process is up, and a plain statement when nothing is running.

`liqfade lab` opens on Configs. Keyboard first: every action has a key shown in the footer.
Tests on the model and controller layer, and Textual's `Pilot` for the widgets where that
is cheap.

## Definition of done

`make lint test` green before every commit, one concern per commit, conventional messages,
no em dashes. `docs/LAB.md` with the keys and the file conventions, a row in
`prompts/README.md`, a `docs/DECISIONS.md` entry naming Textual and the subprocess design,
and an SVG screenshot of each screen under `docs/lab/`. Report the commit list and the
branch name; the control chat merges.
