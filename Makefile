# All targets go through `uv run` so they behave identically on the laptop and the VPS.
.PHONY: setup lint test test-int test-lake record backfill data-quality backtest paper panel status sync clean frontend

CFG ?= config/backtest/v0.yaml

setup:
	uv sync --all-extras
	uv run pre-commit install

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

# -n auto: one worker per core. The suite is 1,300 tests of a few ms each, so the
# 30s CLAUDE.md asks for is only reachable in parallel. `uv run pytest` still works
# serially when a failure needs reading in order.
test:
	uv run pytest -n auto

# Needs a .env with testnet keys. Never run against mainnet.
test-int:
	uv run pytest -m integration

test-lake:
	uv run pytest -m lake

record:
	uv run liqfade record

# Jobs: s3-fills rederive l2 candles tardis. JOB and the dates are
# required; every S3 job estimates the egress and refuses over $5 without --yes.
backfill:
	uv run liqfade backfill $(JOB) --start $(START) --end $(END)

data-quality:
	uv run python scripts/research/data_quality.py

backtest:
	uv run liqfade backtest --config $(CFG)

paper:
	uv run liqfade paper

panel:
	uv run liqfade panel

status:
	uv run liqfade status

sync:
	uv run liqfade sync

# Rebuilds the panel bundle into liqfade/panel/static/. Needs Node 20+; the VPS never runs it.
frontend:
	cd liqfade/panel/frontend && npm ci && npm run build

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
