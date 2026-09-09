"""Operator-only scenario definitions and deterministic fault injection."""

from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import Depends, FastAPI, HTTPException
from pydantic import Field
from sqlalchemy.engine import Engine

from commander.config import Settings
from commander.domain import Contract, Service
from commander.lab_storage import DemoStore
from commander.logging_config import configure_logging
from commander.security import token_dependency
from commander.storage import make_engine


class Scenario(Contract):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    service: Service
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"]
    injection: dict[Literal["slow_db", "new_checkout_path", "worker_stalled"], bool]
    reset: str
    expected_signals: list[str] = Field(min_length=1)
    distractor_signals: list[str] = Field(min_length=1)
    expected_root_cause: str
    allowed_remediations: list[str] = Field(min_length=1)
    verification: list[str] = Field(min_length=1)


def load_scenarios(directory: Path = Path("scenarios")) -> dict[str, Scenario]:
    scenarios = {}
    for path in sorted(directory.glob("*.yaml")):
        scenario = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if scenario.id in scenarios:
            raise ValueError(f"Duplicate scenario: {scenario.id}")
        scenarios[scenario.id] = scenario
    if not scenarios:
        raise RuntimeError(f"No scenario fixtures found in {directory}")
    return scenarios


class ScenarioController:
    def __init__(self, store: DemoStore, scenarios: dict[str, Scenario]):
        self.store = store
        self.scenarios = scenarios

    def activate(self, scenario_id: str) -> Scenario:
        scenario = self.scenarios[scenario_id]
        self.store.reset()
        self.store.change(
            {str(key): value for key, value in scenario.injection.items()}, scenario.service
        )
        if scenario_id == "worker-stalled":
            self.store.enqueue(40)
            self.store.change({"new_checkout_path": False}, "demo-api")
        elif scenario_id == "checkout-db-pool-exhaustion":
            self.store.change({"concurrency": 2}, "demo-worker")
        else:
            self.store.log("demo-worker", "WARNING", "Worker retry succeeded after transient delay")
        return scenario


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    configure_logging("scenario-controller")
    settings = settings or Settings()
    store = DemoStore(engine or make_engine(settings.demo_database_url))
    controller = ScenarioController(store, load_scenarios())
    app = FastAPI(title="Operator scenario controller")
    authorized = Depends(token_dependency(settings.operator_token))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/api/v1/scenarios", dependencies=[authorized])
    def list_scenarios() -> list[dict[str, Any]]:
        return [s.model_dump() for s in controller.scenarios.values()]

    @app.post("/api/v1/scenarios/reset", dependencies=[authorized])
    def reset() -> dict[str, str]:
        store.reset()
        return {"status": "reset"}

    @app.post("/api/v1/scenarios/seed", dependencies=[authorized])
    def seed() -> dict[str, str]:
        store.seed()
        return {"status": "seeded"}

    @app.post("/api/v1/scenarios/{scenario_id}/activate", dependencies=[authorized])
    def activate(scenario_id: str) -> dict[str, str]:
        if scenario_id not in controller.scenarios:
            raise HTTPException(404, "Scenario not found")
        controller.activate(scenario_id)
        return {"status": "activated", "scenario_id": scenario_id}

    return app
