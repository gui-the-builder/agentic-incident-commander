"""Operator-only Kubernetes fault. Never expose this fixture to reasoning tools."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from commander.cli import OperatorClient
from commander.domain import Alert

FIXTURE = Path("scenarios/kubernetes/worker-zero-replicas.yaml")


def alert() -> Alert:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return Alert.model_validate({key: data[key] for key in Alert.model_fields})


def activate(operator: OperatorClient) -> None:
    from commander.kube_cli import kubectl

    # This trusted operator command deliberately injects a fault. The model's
    # remediation adapter still permits only one through four replicas.
    operator.request("POST", "/api/v1/scenarios/reset", scenario=True)
    kubectl("scale", "deployment/demo-worker", "--replicas=0")
    deadline = time.monotonic() + 90
    while True:
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=demo-worker",
                "-o",
                "json",
                capture=True,
            )
        )
        if not pods["items"]:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Worker pods did not terminate; inspect the cluster before continuing"
            )
        time.sleep(0.5)
    # Generate real queued work through the public checkout API, not synthetic metrics.
    with httpx.Client(timeout=20) as load:
        for _ in range(40):
            response = load.post("http://localhost:18001/checkout")
            response.raise_for_status()


def reset(operator: OperatorClient) -> None:
    from commander.kube_cli import kubectl

    operator.request("POST", "/api/v1/scenarios/reset", scenario=True)
    kubectl("scale", "deployment/demo-worker", "--replicas=1")
    kubectl("rollout", "status", "deployment/demo-worker", "--timeout=120s")


def create_incident(operator: OperatorClient) -> dict[str, Any]:
    return dict(
        operator.request("POST", "/api/v1/incidents", alert().model_dump(mode="json")).json()
    )
