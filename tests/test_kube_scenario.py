import json
from typing import Any

import httpx
import pytest

from commander import kube_cli, kube_scenario
from commander.cli import OperatorClient
from commander.domain import Incident
from commander.scenarios import load_scenarios
from commander.storage import Repository
from tests.test_workflow import FixtureLab, workflow


def test_model_context_describes_selected_backend_without_fixture_metadata(
    repository: Repository,
    incident: Incident,
) -> None:
    run = workflow(repository, FixtureLab())
    assert "consumer concurrency" in run.context(incident.id)["action_semantics"]
    run.runtime_backend = "kubernetes"
    context = run.context(incident.id)
    assert context["runtime_backend"] == "kubernetes"
    assert "preserve its desired replicas" in context["action_semantics"]
    assert "worker-zero-replicas" not in json.dumps(context)


def test_kubernetes_fault_and_reset_are_isolated_from_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    requests: list[httpx.Request] = []

    def kubectl(*args: str, **kwargs: Any) -> str:
        commands.append(args)
        return '{"items": []}'

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "id": "incident-test"})

    operator = OperatorClient("http://localhost:18000", "http://localhost:18003")
    operator.client.close()
    operator.client = httpx.Client(transport=httpx.MockTransport(handle))
    load = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(kube_cli, "kubectl", kubectl)
    monkeypatch.setattr(kube_scenario.httpx, "Client", lambda **kwargs: load)
    try:
        kube_scenario.activate(operator)
        assert commands[0] == ("scale", "deployment/demo-worker", "--replicas=0")
        assert len([r for r in requests if r.url.port == 18001 and r.url.path == "/checkout"]) == 40
        assert requests[0].url.port == 18003
        assert requests[0].url.path == "/api/v1/scenarios/reset"
        assert all(r.url.port not in {8000, 8001, 8003} for r in requests)
        assert all("Authorization" not in r.headers for r in requests if r.url.port == 18001)
        kube_scenario.reset(operator)
        assert commands[-2] == ("scale", "deployment/demo-worker", "--replicas=1")
        assert commands[-1] == ("rollout", "status", "deployment/demo-worker", "--timeout=120s")
        kube_scenario.create_incident(operator)
        body = json.loads(requests[-1].content)
        assert set(body) == {"title", "description", "service", "severity"}
        assert "replica" not in requests[-1].content.decode()
        assert requests[-1].url.port == 18000
        assert "worker-zero-replicas" not in load_scenarios()
    finally:
        operator.close()


def test_failed_scale_does_not_create_load_or_incident(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    def fail(*args: str, **kwargs: Any) -> str:
        raise RuntimeError("Kubernetes unavailable")

    operator = OperatorClient("http://localhost:18000", "http://localhost:18003")
    operator.client.close()
    operator.client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(kube_cli, "kubectl", fail)
    try:
        with pytest.raises(RuntimeError, match="Kubernetes unavailable"):
            kube_scenario.activate(operator)
        assert len(requests) == 1 and requests[0].url.port == 18003
    finally:
        operator.close()
