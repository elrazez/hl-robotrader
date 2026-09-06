"""`liqfade lab`: a terminal workbench for running and comparing backtests.

The lab is a client of the CLI and of the files it writes. It never imports the engine to
run a replay: it spawns `liqfade backtest` as a subprocess, so a crash in the lab cannot
take a run down and closing the lab leaves the run going. Nothing here can start live
trading or write `config/mainnet.yaml`.
"""
