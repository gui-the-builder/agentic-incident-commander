"""Backend selection shared by direct runtime and isolated MCP entrypoints."""

import ssl
from collections.abc import Generator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

import httpx

from commander.actions import ActionBackend
from commander.adapters import ServiceActions
from commander.config import Settings
from commander.kubernetes import (
    KUBERNETES_ACTION_RISKS,
    KUBERNETES_READ_TOOLS,
    KubernetesActions,
    KubernetesReads,
)
from commander.policy import Policy
from commander.tools import READ_TOOLS, ToolDefinition, ToolHandler


class FileTokenAuth(httpx.Auth):
    def __init__(self, path: str):
        self.path = Path(path)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = self.path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Kubernetes service-account token is empty")
        request.headers["Authorization"] = "Bearer " + token
        yield request


def kubernetes_client(
    settings: Settings, boundary: Literal["reads", "actions"]
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.kubernetes_api_url,
        timeout=5,
        verify=ssl.create_default_context(cafile=settings.kubernetes_ca_file),
        auth=FileTokenAuth(
            settings.kubernetes_read_token_file
            if boundary == "reads"
            else settings.kubernetes_action_token_file
        ),
    )


def backend_policy(settings: Settings) -> Policy:
    return Policy(
        settings.auto_approve_low_risk,
        settings.allow_medium_risk_actions,
        KUBERNETES_ACTION_RISKS if settings.runtime_backend == "kubernetes" else None,
    )


def read_definitions(settings: Settings) -> dict[str, ToolDefinition]:
    return {
        **READ_TOOLS,
        **(KUBERNETES_READ_TOOLS if settings.runtime_backend == "kubernetes" else {}),
    }


async def configured_reads(
    stack: AsyncExitStack, settings: Settings, handlers: dict[str, ToolHandler]
) -> dict[str, ToolHandler]:
    if settings.runtime_backend == "kubernetes":
        client = await stack.enter_async_context(kubernetes_client(settings, "reads"))
        return {**handlers, "get_kubernetes_workload": KubernetesReads(client).workload}
    return handlers


async def configured_actions(
    stack: AsyncExitStack, settings: Settings, client: httpx.AsyncClient
) -> ActionBackend:
    service = ServiceActions(
        client, settings.demo_api_url, settings.demo_worker_url, settings.control_token
    )
    if settings.runtime_backend == "kubernetes":
        kube = await stack.enter_async_context(kubernetes_client(settings, "actions"))
        return KubernetesActions(kube, service)
    return service
