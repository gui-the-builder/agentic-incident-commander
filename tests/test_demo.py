import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from commander.adapters import OperationalReads
from commander.config import Settings
from commander.demo import create_app
from commander.domain import LogsQuery, ServiceQuery
from commander.lab_storage import DemoStore
from commander.scenarios import ScenarioController, load_scenarios
from commander.security import token_dependency
from commander.storage import Base, make_engine
from commander.worker import create_app as create_worker


@pytest.fixture
def lab_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine("sqlite:///" + str(tmp_path / "lab.db"))
    Base.metadata.create_all(engine)
    DemoStore(engine).seed()
    yield engine
    engine.dispose()


def settings() -> Settings:
    return Settings(
        _env_file=None,
        control_token="test-control",
        operator_token="test-operator",
        worker_poll_seconds=0.01,
    )


def test_checkout_persists_a_job_and_emits_metrics(lab_engine: Engine) -> None:
    with TestClient(create_app(settings(), lab_engine)) as client:
        assert client.get("/ready").status_code == 200
        response = client.post("/checkout")
        assert response.status_code == 200
        assert client.get("/jobs").json()["worker_jobs_pending"] == 1
        metrics = client.get("/metrics").text
        assert 'http_requests_total{status="200"} 1.0' in metrics
        assert "db_pool_wait_seconds_count" in metrics


def test_bad_feature_flag_is_real_failure_and_reset_recovers(lab_engine: Engine) -> None:
    store = DemoStore(lab_engine)
    controller = ScenarioController(store, load_scenarios())
    with TestClient(create_app(settings(), lab_engine)) as client:
        controller.activate("checkout-bad-feature-flag")
        assert client.post("/checkout").status_code == 500
        assert client.get("/jobs").json()["worker_jobs_pending"] == 0
        store.reset()
        assert client.post("/checkout").status_code == 200


def test_slow_database_holds_a_real_connection(lab_engine: Engine) -> None:
    store = DemoStore(lab_engine)
    ScenarioController(store, load_scenarios()).activate("checkout-db-pool-exhaustion")
    with TestClient(create_app(settings(), lab_engine)) as client:
        started = time.monotonic()
        assert client.post("/checkout").status_code == 200
        assert time.monotonic() - started >= 2.5
        assert "http_request_duration_seconds_count 1.0" in client.get("/metrics").text


def test_stalled_worker_stays_live_and_restart_drains_backlog(lab_engine: Engine) -> None:
    store = DemoStore(lab_engine)
    ScenarioController(store, load_scenarios()).activate("worker-stalled")
    with TestClient(create_worker(settings(), lab_engine)) as client:
        assert client.get("/health").status_code == 200
        assert store.consume() == 0 and store.worker_metrics()["worker_jobs_pending"] == 40
        assert client.post("/internal/restart", json={"reason": "test"}).status_code == 401
        response = client.post(
            "/internal/restart",
            json={"reason": "test"},
            headers={"Authorization": "Bearer test-control"},
        )
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while store.worker_metrics()["worker_jobs_pending"] and time.monotonic() < deadline:
            time.sleep(0.01)
        metrics = store.worker_metrics()
        assert metrics["worker_jobs_pending"] == 0 and metrics["worker_jobs_processed_total"] == 40
        assert time.time() - metrics["worker_last_success_timestamp"] < 5


@pytest.mark.parametrize("scenario_id", list(load_scenarios()))
def test_scenarios_can_be_repeated_and_reset(lab_engine: Engine, scenario_id: str) -> None:
    store = DemoStore(lab_engine)
    controller = ScenarioController(store, load_scenarios())
    for _ in range(2):
        scenario = controller.activate(scenario_id)
        assert scenario.distractor_signals
        for key, value in scenario.injection.items():
            assert store.config()[key] == value
        store.reset()
        assert not store.config()["new_checkout_path"]
        assert not store.config()["slow_db"]
        assert not store.config()["worker_stalled"]
        assert store.worker_metrics()["worker_jobs_pending"] == 0


async def test_read_adapter_never_reveals_worker_stall_flag(lab_engine: Engine) -> None:
    store = DemoStore(lab_engine)
    ScenarioController(store, load_scenarios()).activate("worker-stalled")
    async with httpx.AsyncClient() as client:
        reads = OperationalReads(
            client, lab_engine, "http://prometheus", "http://api", "http://worker"
        )
        assert await reads.config(ServiceQuery(service="demo-worker")) == {"concurrency": 1}


def test_control_routes_reject_unauthorized_mutation(lab_engine: Engine) -> None:
    with TestClient(create_app(settings(), lab_engine)) as client:
        body = {"flag": "slow_db", "value": True, "reason": "test"}
        assert client.post("/internal/flag", json=body).status_code == 401
        assert not DemoStore(lab_engine).config()["slow_db"]
        response = client.post(
            "/internal/flag",
            json={**body, "flag": "arbitrary"},
            headers={"Authorization": "Bearer test-control"},
        )
        assert response.status_code == 422


def test_unconfigured_control_token_fails_closed() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        token_dependency("")("Bearer ")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_log_search_matches_error_type_and_preserves_literal_filters(
    lab_engine: Engine,
) -> None:
    store = DemoStore(lab_engine)
    store.log(
        "demo-api", "ERROR", "database connection acquisition timed out", error_type="PoolTimeout"
    )
    store.log("demo-worker", "ERROR", "worker failure", error_type="PoolTimeout")
    store.log("demo-api", "INFO", "informational event", error_type="PoolTimeout")
    store.log("demo-api", "ERROR", "ordinary message")
    store.log("demo-api", "ERROR", "literal marker", error_type="Fault_100%")
    async with httpx.AsyncClient() as client:
        reads = OperationalReads(
            client, lab_engine, "http://prometheus", "http://api", "http://worker"
        )

        async def search(term: str) -> list[dict]:
            result = await reads.logs(LogsQuery(service="demo-api", contains=term))
            return result["entries"]

        matches = await search("PoolTimeout")
        assert len(matches) == 1
        assert matches[0]["message"] == "database connection acquisition timed out"
        assert matches[0]["attributes"]["error_type"] == "PoolTimeout"
        assert len(await search("ordinary")) == 1
        assert len(await search("")) == 3
        assert len(await search("_100%")) == 1
        assert await search("Fault%") == []


def test_api_restart_targets_only_its_supervised_process(lab_engine: Engine) -> None:
    restarts = []
    app = create_app(settings(), lab_engine, restart_process=lambda: restarts.append("self"))
    with TestClient(app) as client:
        response = client.post(
            "/internal/restart",
            json={"reason": "Reviewed service restart"},
            headers={"Authorization": "Bearer test-control"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["restart_scheduled"] == "demo-api"
    assert restarts == ["self"]
