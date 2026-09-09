"""Fixed-namespace Kubernetes adapters. Call action backend only through ActionGateway."""

from typing import Any
from uuid import UUID

import httpx
from pydantic import Field

from commander.actions import ActionBackend
from commander.domain import Action, Contract, Risk, ServiceQuery, ToolResult
from commander.policy import ACTION_RISKS, PolicyViolation
from commander.tools import ToolDefinition

NAMESPACE = "incident-lab"
KUBERNETES_ACTION_RISKS = {**ACTION_RISKS, "restart_demo_worker": Risk.MEDIUM}


class PodStatus(Contract):
    name: str
    phase: str
    ready: bool
    restarts: int = Field(ge=0)
    waiting_reasons: list[str]


class RolloutCondition(Contract):
    type: str
    status: str
    reason: str


class WorkloadStatus(Contract):
    service: str
    namespace: str
    desired_replicas: int = Field(ge=0)
    ready_replicas: int = Field(ge=0)
    available_replicas: int = Field(ge=0)
    generation: int = Field(ge=0)
    observed_generation: int = Field(ge=0)
    conditions: list[RolloutCondition]
    pods: list[PodStatus]
    truncated: bool


KUBERNETES_READ_TOOLS = {
    "get_kubernetes_workload": ToolDefinition(
        "get_kubernetes_workload",
        "Inspect demo deployment replicas, rollout and pod health",
        ServiceQuery,
        WorkloadStatus,
    ),
}


class KubernetesReads:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def workload(self, arguments: Contract) -> dict[str, Any]:
        query = ServiceQuery.model_validate(arguments.model_dump())
        deployment = await self.client.get(
            f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{query.service}"
        )
        deployment.raise_for_status()
        pods = await self.client.get(
            f"/api/v1/namespaces/{NAMESPACE}/pods",
            params={
                "labelSelector": f"app.kubernetes.io/name={query.service}",
                "limit": 100,
            },
        )
        pods.raise_for_status()
        data = deployment.json()
        if (
            data["metadata"].get("name") != query.service
            or data["metadata"].get("namespace") != NAMESPACE
        ):
            raise PolicyViolation("Kubernetes workload identity does not match the allowed target")
        items = pods.json()
        result = WorkloadStatus(
            service=query.service,
            namespace=NAMESPACE,
            desired_replicas=data["spec"].get("replicas", 1),
            ready_replicas=data.get("status", {}).get("readyReplicas", 0),
            available_replicas=data.get("status", {}).get("availableReplicas", 0),
            generation=data["metadata"].get("generation", 0),
            observed_generation=data.get("status", {}).get("observedGeneration", 0),
            conditions=[
                RolloutCondition(type=c["type"], status=c["status"], reason=c.get("reason", ""))
                for c in data.get("status", {}).get("conditions", [])
            ],
            pods=[
                PodStatus(
                    name=p["metadata"]["name"],
                    phase=p.get("status", {}).get("phase", "Unknown"),
                    ready=any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in p.get("status", {}).get("conditions", [])
                    ),
                    restarts=sum(
                        c.get("restartCount", 0)
                        for c in p.get("status", {}).get("containerStatuses", [])
                    ),
                    waiting_reasons=[
                        c["state"]["waiting"].get("reason", "Unknown")
                        for c in p.get("status", {}).get("containerStatuses", [])
                        if "waiting" in c.get("state", {})
                    ],
                )
                for p in items.get("items", [])
                if p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == query.service
            ],
            truncated=bool(items.get("metadata", {}).get("continue")),
        )
        return result.model_dump(mode="json")


class KubernetesActions:
    def __init__(self, client: httpx.AsyncClient, service_actions: ActionBackend):
        self.client = client
        self.service_actions = service_actions

    async def execute(self, action: Action, idempotency_key: UUID) -> ToolResult:
        # Revalidate discriminated action data even when constructed outside Pydantic.
        from pydantic import TypeAdapter

        action = TypeAdapter(Action).validate_python(action.model_dump())
        if action.action_type == "set_demo_feature_flag":
            return await self.service_actions.execute(action, idempotency_key)
        service = "demo-api" if action.action_type == "restart_demo_api" else "demo-worker"
        base = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{service}"
        scaling: bool = action.action_type == "scale_demo_worker"
        path = base + "/scale" if scaling else base
        current = await self.client.get(path)
        current.raise_for_status()
        metadata = current.json()["metadata"]
        if metadata.get("name") != service or metadata.get("namespace") != NAMESPACE:
            raise PolicyViolation("Kubernetes resource identity does not match the allowed target")
        body: dict[str, Any] = {"metadata": {"resourceVersion": metadata["resourceVersion"]}}
        if action.action_type == "scale_demo_worker":
            body["spec"] = {"replicas": action.arguments.concurrency}
        else:
            body["spec"] = {
                "template": {
                    "metadata": {
                        "annotations": {
                            "incident-commander/action-id": str(idempotency_key),
                        }
                    }
                }
            }
        result = await self.client.patch(
            path, json=body, headers={"Content-Type": "application/merge-patch+json"}
        )
        result.raise_for_status()
        return ToolResult(
            ok=True,
            data={
                "namespace": NAMESPACE,
                "service": service,
                "operation": "scale" if scaling else "restart",
                "resource_version": result.json()["metadata"]["resourceVersion"],
            },
        )
