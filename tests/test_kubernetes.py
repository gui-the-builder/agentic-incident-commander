import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from commander.actions import ActionGateway
from commander.domain import (
    Action,
    ApprovalRequest,
    Incident,
    ScaleArguments,
    ScaleWorker,
    State,
)
from commander.kubernetes import (
    KUBERNETES_ACTION_RISKS,
    KUBERNETES_READ_TOOLS,
    KubernetesActions,
    KubernetesReads,
)
from commander.policy import Policy, PolicyViolation
from commander.storage import Repository
from commander.tools import ReadGateway
from tests.test_policy import plan
from tests.test_workflow import FixtureLab


def metadata(service: str = "demo-worker") -> dict[str, str]:
    return {"name": service, "namespace": "incident-lab", "resourceVersion": "17"}


async def test_workload_reads_are_allowlisted_sanitized_and_audited(
    repository: Repository,
    incident: Incident,
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/deployments/demo-worker"):
            return httpx.Response(
                200,
                json={
                    "metadata": metadata(),
                    "spec": {
                        "replicas": 1,
                        "template": {"spec": {"containers": [{"env": [{"value": "secret"}]}]}},
                    },
                    "status": {
                        "conditions": [
                            {
                                "type": "Available",
                                "status": "False",
                                "reason": "MinimumReplicasUnavailable",
                                "message": "private detail",
                            }
                        ]
                    },
                },
            )
        assert request.url.path == "/api/v1/namespaces/incident-lab/pods"
        assert request.url.params["labelSelector"] == "app.kubernetes.io/name=demo-worker"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "worker-1",
                            "labels": {"app.kubernetes.io/name": "demo-worker"},
                        },
                        "spec": {"containers": [{"env": [{"value": "secret"}]}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "restartCount": 4,
                                    "state": {
                                        "waiting": {
                                            "reason": "CrashLoopBackOff",
                                            "message": "private detail",
                                        }
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://kubernetes", transport=httpx.MockTransport(respond)
    ) as c:
        reads = KubernetesReads(c)
        gateway = ReadGateway(
            repository, {"get_kubernetes_workload": reads.workload}, KUBERNETES_READ_TOOLS
        )
        result = await gateway.call(
            incident.id, "get_kubernetes_workload", {"service": "demo-worker"}
        )
        assert result.ok and result.data["pods"][0]["restarts"] == 4
        assert result.data["pods"][0]["waiting_reasons"] == ["CrashLoopBackOff"]
        assert "secret" not in json.dumps(result.data) and "private detail" not in json.dumps(
            result.data
        )
        assert not (
            await gateway.call(
                incident.id,
                "get_kubernetes_workload",
                {"service": "demo-worker", "namespace": "kube-system"},
            )
        ).ok
    assert len(calls) == 2 and len(repository.evidence(incident.id)) == 1


@pytest.mark.parametrize(
    "action_type", ["restart_demo_api", "restart_demo_worker", "scale_demo_worker"]
)
async def test_kubernetes_mutations_have_fixed_paths_bodies_and_resource_preconditions(
    action_type: str,
) -> None:
    arguments: dict[str, Any] = {"reason": "Approved recovery"}
    if action_type == "scale_demo_worker":
        arguments["concurrency"] = 3
    action = TypeAdapter(Action).validate_python(
        {"action_type": action_type, "arguments": arguments}
    )
    action_id = uuid4()
    service = "demo-api" if action_type == "restart_demo_api" else "demo-worker"
    path = f"/apis/apps/v1/namespaces/incident-lab/deployments/{service}"
    scaling = action_type == "scale_demo_worker"
    if scaling:
        path += "/scale"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        if request.method == "PATCH":
            data = json.loads(request.content)
            assert data["metadata"] == {"resourceVersion": "17"}
            assert request.headers["Content-Type"] == "application/merge-patch+json"
            assert data["spec"] == (
                {"replicas": 3}
                if scaling
                else {
                    "template": {
                        "metadata": {
                            "annotations": {"incident-commander/action-id": str(action_id)}
                        }
                    }
                }
            )
        else:
            assert request.method == "GET"
        return httpx.Response(200, json={"metadata": metadata(service)})

    async with httpx.AsyncClient(
        base_url="https://kubernetes", transport=httpx.MockTransport(respond)
    ) as c:
        assert (await KubernetesActions(c, FixtureLab()).execute(action, action_id)).ok


async def test_forged_out_of_bounds_scale_never_reaches_kubernetes() -> None:
    calls = []
    async with httpx.AsyncClient(
        base_url="https://kubernetes",
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    ) as c:
        forged = ScaleWorker.model_construct(
            arguments=ScaleArguments.model_construct(concurrency=0, reason="x")
        )
        with pytest.raises(ValidationError):
            await KubernetesActions(c, FixtureLab()).execute(forged, uuid4())
    assert not calls


@pytest.mark.parametrize("patch_status", [200, 409])
async def test_kubernetes_restart_requires_plan_bound_approval(
    repository: Repository,
    incident: Incident,
    patch_status: int,
) -> None:
    policy = Policy(action_risks=KUBERNETES_ACTION_RISKS)
    remediation = plan(policy, "restart_demo_worker").model_copy(
        update={"incident_id": incident.id}
    )
    # Persist a matching hypothesis before saving the plan.
    from commander.domain import Hypothesis
    from commander.storage import HypothesisRow

    hypothesis = Hypothesis(
        id=remediation.diagnosis_hypothesis_id,
        incident_id=incident.id,
        statement="Worker unavailable",
        confidence=0.9,
    )
    repository.add_record(HypothesisRow, incident.id, hypothesis.model_dump(mode="json"))
    repository.save_plan(remediation)
    checkpoint = {"plan_id": str(remediation.id)}
    repository.advance(incident.id, 0, State.EXECUTING, checkpoint, "Test approval boundary")
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            patch_status if request.method == "PATCH" else 200, json={"metadata": metadata()}
        )

    async with httpx.AsyncClient(
        base_url="https://kubernetes", transport=httpx.MockTransport(respond)
    ) as c:
        gateway = ActionGateway(repository, policy, KubernetesActions(c, FixtureLab()))
        with pytest.raises(PolicyViolation, match="approval"):
            await gateway.execute(incident.id, remediation.id)
        assert not calls
        repository.advance(incident.id, 1, State.AWAITING_APPROVAL, checkpoint, "Review")
        repository.approve(incident.id, ApprovalRequest(decision="APPROVED", actor="operator"))
        repository.advance(incident.id, 2, State.EXECUTING, checkpoint, "Approved")
        assert (await gateway.execute(incident.id, remediation.id))[0].ok == (patch_status == 200)
        if patch_status == 200:
            assert (await gateway.execute(incident.id, remediation.id))[0].ok
        else:
            with pytest.raises(PolicyViolation, match="uncertain"):
                await gateway.execute(incident.id, remediation.id)
    assert calls == ["GET", "PATCH"]


def test_kubernetes_rbac_never_grants_cluster_secrets_exec_or_arbitrary_workload_writes() -> None:
    objects = list(yaml.safe_load_all(Path("infra/kubernetes/rbac.yaml").read_text()))
    assert all(o["kind"] not in {"ClusterRole", "ClusterRoleBinding"} for o in objects)
    for obj in objects:
        for rule in obj.get("rules", []):
            assert not ({"secrets", "pods/exec", "*"} & set(rule["resources"]))
            if set(rule["verbs"]) - {"get", "list"}:
                assert rule["verbs"] == ["get", "patch"]
                assert set(rule["resourceNames"]) <= {"demo-api", "demo-worker"}
    for obj in objects:
        if obj["kind"] != "Namespace":
            assert obj["metadata"]["namespace"] == "incident-lab"
