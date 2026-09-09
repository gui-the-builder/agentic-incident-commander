import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from commander import evals, kube_scenario
from commander.cli import OperatorClient
from tests.test_diagnosis import zero_replica_fixture


def test_kubernetes_rubric_requires_workload_and_database_citations() -> None:
    audit = zero_replica_fixture()
    assert audit.assess("worker-zero-replicas")["passed"]
    audit.selected["supporting_evidence_ids"].pop()
    assert not audit.assess("worker-zero-replicas")["passed"]
    assert evals.diagnosis_matches("worker-zero-replicas", "Worker deployment has zero replicas")
    assert not evals.diagnosis_matches("worker-zero-replicas", "Worker consumer is stalled")


def test_kubernetes_rubric_rejects_stale_fault_snapshot() -> None:
    audit = zero_replica_fixture()
    audit.selected["updated_at"] = (audit.at + timedelta(minutes=4)).isoformat()
    audit.incident["remediation_plan"]["created_at"] = (audit.at + timedelta(minutes=5)).isoformat()
    result = audit.assess("worker-zero-replicas")
    assert not result["passed"] and "recent_workload_and_backlog" in result["missing_checks"]


@pytest.mark.parametrize(
    "change",
    [
        {"desired_replicas": 1},
        {"ready_replicas": 1},
        {"observed_generation": 1},
        {"truncated": True},
        {"namespace": "other"},
    ],
)
def test_kubernetes_rubric_rejects_contradictory_workload(change: dict[str, Any]) -> None:
    audit = zero_replica_fixture()
    audit.add("get_kubernetes_workload", {**audit.evidence[0]["payload"], **change})
    assert not audit.assess("worker-zero-replicas")["passed"]


@pytest.mark.parametrize("approve", [False, True])
def test_recording_keeps_kubernetes_ports_and_requires_explicit_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approve: bool,
) -> None:
    incident_id = str(uuid4())
    approved = False
    requests: list[httpx.Request] = []
    base = {
        "id": incident_id,
        "created_at": "2026-09-08T12:00:00Z",
        "updated_at": "2026-09-08T12:00:10Z",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal approved
        requests.append(request)
        if request.url.path.endswith("/approval"):
            assert approve and request.method == "POST"
            assert json.loads(request.content)["decision"] == "APPROVED"
            approved = True
            return httpx.Response(200, json={})
        if request.url.path.endswith(incident_id):
            return httpx.Response(
                200,
                json={
                    **base,
                    "status": "RESOLVED" if approved else "WAITING_FOR_APPROVAL",
                    "current_state": "POSTMORTEM_GENERATED" if approved else "AWAITING_APPROVAL",
                },
            )
        if request.url.path.endswith("/postmortem"):
            return httpx.Response(200, text="# Fixture report")
        return httpx.Response(200, json=[])

    operator = OperatorClient("http://localhost:18000", "http://localhost:18003")
    operator.client.close()
    operator.client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(evals.time, "sleep", lambda _: None)
    try:
        if approve:
            result = evals.record_incident(
                operator, "worker-zero-replicas", base, tmp_path, True, backend="kubernetes"
            )
            assert result["runtime_backend"] == "kubernetes"
            # A claimed resolved status without verification evidence cannot pass recovery.
            assert not result["recovered"] and not result["evidence_passed"]
            assert (tmp_path / incident_id / "audit.json").exists()
            assert (
                json.loads((tmp_path / incident_id / "audit.json").read_text())["remediations"]
                == []
            )
            assert (tmp_path / incident_id / "postmortem.md").read_text() == "# Fixture report"
        else:
            with pytest.raises(RuntimeError, match="approve-fixtures"):
                evals.record_incident(
                    operator, "worker-zero-replicas", base, tmp_path, False, backend="kubernetes"
                )
            assert not approved
            assert not (tmp_path / incident_id / "audit.json").exists()
        run = json.loads((tmp_path / incident_id / "run.json").read_text())
        assert run["backend"] == "kubernetes" and run["incident_id"] == incident_id
        assert all(request.url.port == 18000 for request in requests)
        assert not any("reset" in request.url.path for request in requests)
    finally:
        operator.close()


def test_kubernetes_trial_uses_its_prometheus_and_operator_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages: list[str] = []
    incident = {"id": str(uuid4())}

    def metric(request: httpx.Request) -> httpx.Response:
        assert request.url.port == 19090
        stages.append("healthy")
        return httpx.Response(200, json={"data": {"result": [{"value": [0, "0"]}]}})

    def create(operator: OperatorClient) -> dict[str, Any]:
        stages.append("incident")
        return incident

    def record(
        operator: OperatorClient,
        scenario: str,
        supplied: dict[str, Any],
        output: Path,
        approve: bool,
        backend: str,
    ) -> dict[str, Any]:
        assert supplied == incident and scenario == "worker-zero-replicas"
        assert backend == "kubernetes" and not approve
        stages.append("follow")
        return {"run_id": incident["id"]}

    monkeypatch.setattr(kube_scenario, "reset", lambda _: stages.append("reset"))
    monkeypatch.setattr(kube_scenario, "activate", lambda _: stages.append("fault"))
    monkeypatch.setattr(kube_scenario, "create_incident", create)
    monkeypatch.setattr(evals, "record_incident", record)
    operator = OperatorClient("http://localhost:18000", "http://localhost:18003")
    operator.client.close()
    operator.client = httpx.Client(transport=httpx.MockTransport(metric))
    try:
        evals.run_kubernetes_trial(operator, tmp_path, False)
        assert stages == ["reset", "healthy", "fault", "incident", "follow"]
    finally:
        operator.close()


@pytest.mark.parametrize(
    "backend,scenario",
    [
        ("compose", "worker-zero-replicas"),
        ("kubernetes", "worker-stalled"),
    ],
)
def test_mismatched_backend_is_rejected_before_creating_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    scenario: str,
) -> None:
    output = tmp_path / "new-run"
    monkeypatch.setattr(
        sys,
        "argv",
        ["evals", "--backend", backend, "--scenario", scenario, "--output", str(output)],
    )
    with pytest.raises(SystemExit) as exc:
        evals.main()
    assert exc.value.code == 2
    assert not output.exists()
