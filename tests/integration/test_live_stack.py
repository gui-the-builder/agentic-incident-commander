"""Opt-in checks against this project's Compose lab, without model invocations."""

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from dotenv import dotenv_values

from commander.cli import OperatorClient

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="Local Compose opt-in")


def assert_error_logs_searchable(error_type: str) -> None:
    script = """
import asyncio
import sys
import httpx
from commander.adapters import OperationalReads
from commander.config import Settings
from commander.domain import LogsQuery
from commander.storage import make_engine

async def main():
    engine = make_engine(Settings().investigation_database_url)
    try:
        async with httpx.AsyncClient() as client:
            reads = OperationalReads(client, engine, "http://prometheus:9090",
                                     "http://demo-api:8001", "http://demo-worker:8002")
            result = await reads.logs(LogsQuery(service="demo-api", contains=sys.argv[1]))
            assert result["entries"], "Structured error was absent from filtered log search"
            assert all(row["attributes"]["error_type"] == sys.argv[1] for row in result["entries"])
    finally:
        engine.dispose()

asyncio.run(main())
"""
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "infra/compose/compose.yaml",
            "exec",
            "-T",
            "agent-runtime",
            "python",
            "-c",
            script,
            error_type,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_demo_api_restart_is_supervised_and_recovers() -> None:
    container_id = subprocess.check_output(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "infra/compose/compose.yaml",
            "ps",
            "-q",
            "demo-api",
        ],
        text=True,
    ).strip()
    assert container_id

    def restarts() -> int:
        return int(
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.RestartCount}}",
                    container_id,
                ],
                text=True,
            )
        )

    before = restarts()
    token = dotenv_values(".env")["CONTROL_TOKEN"]
    with httpx.Client(timeout=5) as client:
        response = client.post(
            "http://localhost:8001/internal/restart",
            json={"reason": "Verify supervised service restart"},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if restarts() > before and client.get("http://localhost:8001/ready").is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
    pytest.fail("Demo API did not restart and regain readiness")


def test_postgres_investigation_credentials_are_constrained() -> None:
    script = """
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from commander.config import Settings
from commander.storage import make_engine
engine = make_engine(Settings().investigation_database_url)
with engine.connect() as connection:
    hidden = text("SELECT key FROM demo_config WHERE key='worker_stalled'")
    assert not connection.execute(hidden).all()
statements = ("SELECT count(*) FROM incidents", "UPDATE demo_jobs SET status=status WHERE false")
for statement in statements:
    with engine.connect() as connection:
        try:
            connection.execute(text(statement))
        except DBAPIError:
            pass
        else:
            raise AssertionError("Investigation credential exceeded its permissions")
engine.dispose()
print("Investigation role cannot read private state or mutate jobs")
"""
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "infra/compose/compose.yaml",
            "exec",
            "-T",
            "agent-runtime",
            "python",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_postgres_advisory_lock_excludes_another_connection() -> None:
    script = """
from uuid import uuid4
from commander.config import Settings
from commander.storage import Conflict, Repository, make_engine
engine = make_engine(Settings().database_url)
first, second = Repository(engine), Repository(engine)
incident_id = uuid4()
with first.workflow_lock(incident_id):
    try:
        with second.workflow_lock(incident_id):
            raise AssertionError("Two database sessions acquired the same incident lock")
    except Conflict:
        pass
with second.workflow_lock(incident_id):
    pass
engine.dispose()
print("PostgreSQL excludes concurrent incident workers and releases the lock")
"""
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "infra/compose/compose.yaml",
            "exec",
            "-T",
            "agent-runtime",
            "python",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_prometheus_scrapes_services_and_grafana_is_provisioned() -> None:
    with httpx.Client(timeout=10) as client:
        response = client.get("http://localhost:9090/api/v1/targets")
        response.raise_for_status()
        targets = response.json()["data"]["activeTargets"]
        assert {target["labels"]["job"] for target in targets} >= {
            "demo-api",
            "demo-worker",
            "agent-runtime",
        }
        assert all(target["health"] == "up" for target in targets)
        dashboard = client.get("http://localhost:3000/api/dashboards/uid/incident-lab")
        dashboard.raise_for_status()
        assert len(dashboard.json()["dashboard"]["panels"]) == 6


@pytest.mark.parametrize(
    "scenario",
    [
        "checkout-bad-feature-flag",
        "checkout-db-pool-exhaustion",
        "worker-stalled",
    ],
)
def test_live_scenario_causes_measured_failure_and_resets(scenario: str) -> None:
    operator = OperatorClient()
    try:
        operator.request("POST", f"/api/v1/scenarios/{scenario}/activate", scenario=True)
        with httpx.Client(timeout=15) as client:
            if scenario == "checkout-bad-feature-flag":
                assert client.post("http://localhost:8001/checkout").status_code == 500
                assert_error_logs_searchable("CheckoutPathError")
            elif scenario == "checkout-db-pool-exhaustion":
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=12) as pool:
                    results = list(
                        pool.map(lambda _: client.post("http://localhost:8001/checkout"), range(12))
                    )
                assert time.monotonic() - started >= 2.5
                assert any(result.status_code == 503 for result in results)
                assert_error_logs_searchable("PoolTimeout")
            else:
                before = client.get("http://localhost:8001/jobs").json()
                time.sleep(2)
                after = client.get("http://localhost:8001/jobs").json()
                assert after["worker_jobs_pending"] > before["worker_jobs_pending"]
                assert after["worker_jobs_processed_total"] == before["worker_jobs_processed_total"]
            operator.request("POST", "/api/v1/scenarios/reset", scenario=True)
            # Reset clears the flag immediately, but checkouts already holding a pooled
            # connection keep it for their remaining sleep. Allow that backlog to drain.
            deadline = time.monotonic() + 15
            while client.post("http://localhost:8001/checkout").status_code != 200:
                assert time.monotonic() < deadline, "Reset did not restore successful checkouts"
                time.sleep(1)
    finally:
        operator.request("POST", "/api/v1/scenarios/reset", scenario=True)
        operator.close()
