# Repository Guidelines

## Project Structure & Module Organization

`packages/commander/` contains domain contracts, persistence, policy, tools, model adapters, and the LangGraph workflow. `tests/` contains deterministic tests; `migrations/` contains Alembic revisions. Read the specifications under `specs/001-local-incident-commander/` before implementing features; use `tasks.md` for implementation order and `acceptance.md` for completion criteria. Consult `docs/implementation-status.md` for outstanding work.

`apps/README.md` lists service entrypoints. Compose and observability configuration live in `infra/`, deterministic scenario fixtures in `scenarios/`, and supporting documentation in `docs/`.

## Build, Test, and Development Commands

Use the `uv` forms on Windows without Make:

- `make bootstrap`: install locked dependencies and generate local credentials. Without Make, run `uv sync --frozen --link-mode copy`, then `uv run python -m commander.cli bootstrap`.
- `make test` / `uv run pytest -q`: run deterministic tests.
- `make lint` / `uv run ruff check packages tests migrations`: lint code.
- `make typecheck` / `uv run mypy`: run strict type checking.
- `make migrate` / `uv run python -m commander.cli migrate`: apply Compose database migrations.
- `make build` / `uv build`: build the Python package.
- `make up` / `make down`: start or stop Compose; `make reset` restores fixtures.
- `make incident SCENARIO=worker-stalled`: investigate a previously activated fixture.

Use Python 3.12+ and local Ollama `gemma4:12b`. Bootstrap writes credentials to `.env`.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, and `PascalCase` classes. Run Ruff formatting with a 100-character line limit. Use Pydantic contracts and typed tools; keep vendor calls behind the model adapter. Follow hyphenated scenario identifiers such as `worker-stalled`.

## Testing Guidelines

Use pytest/pytest-asyncio and `test_*.py` naming. Unit tests use fake models/tools and SQLite. Cover transitions, approval, retries, timeouts, and restarts. Set `RUN_LIVE_TESTS=1` for Compose integration tests; these reset shared fixtures, so run them without active incidents. Real Ollama evaluations run through `python -m commander.evals`. No coverage percentage is mandated; evaluation targets are at least 90% diagnosis/recovery and 100% safety checks passing.

## Commit & Pull Request Guidelines

History contains only `Initial commit`; no commit convention is established. Use concise, imperative subjects. PRs should describe behavior changes, reference relevant spec tasks or issues, report validation and limitations, and update affected contracts/docs. Include screenshots for UI changes.

## Security & Configuration

Keep secrets out of commits and prompts. Preserve deterministic tool allowlists and approval gates. Never expose arbitrary shell execution, SQL writes, or the Docker socket to the incident agent. Keep scenario ground truth hidden from agent context.
