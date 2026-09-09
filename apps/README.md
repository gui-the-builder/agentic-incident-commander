# Service entrypoints

Service entrypoints live in the installable Python package and are wired by
`infra/compose/compose.yaml`. This avoids duplicating importable application code.

| Service | Entrypoint |
| --- | --- |
| Commander API | `uvicorn commander.api:create_app --factory --port 8000` |
| Agent runtime | `uvicorn commander.runtime:create_app --factory --port 8004` |
| Scenario controller | `uvicorn commander.scenarios:create_app --factory --port 8003` |
| Demo checkout API | `uvicorn commander.demo:create_app --factory --port 8001` |
| Demo worker | `uvicorn commander.worker:create_app --factory --port 8002` |
| Traffic generator | `python -m commander.traffic` |

Use Compose for integrated runs; these commands alone still need database, token,
model, and service URL configuration.
