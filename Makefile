.PHONY: bootstrap test lint format typecheck check build migrate up down health seed reset logs scenario incident demo eval model-pull
SCENARIO ?= checkout-db-pool-exhaustion

bootstrap:
	uv sync --frozen --link-mode copy
	uv run python -m commander.cli bootstrap

test:
	uv run pytest -q

lint:
	uv run ruff check packages tests migrations

format:
	uv run ruff format packages tests migrations

typecheck:
	uv run mypy

check: lint typecheck test

build:
	uv build

migrate:
	uv run python -m commander.cli migrate

up down health seed reset logs model-pull:
	uv run python -m commander.cli $@

scenario incident:
	uv run python -m commander.cli $@ $(SCENARIO)

demo:
	uv run python -m commander.cli demo

eval:
	uv run python -m commander.evals --trials 10 --approve-fixtures
