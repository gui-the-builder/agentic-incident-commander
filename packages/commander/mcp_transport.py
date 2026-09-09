"""Private stdio MCP boundaries; action authority remains in the persisted policy gate."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any, Literal
from uuid import UUID

import httpx
import mcp_types as mt
from mcp import Client, StdioServerParameters, stdio_server
from mcp.server import Server, ServerRequestContext

from commander.actions import ActionExecutor, ActionGateway
from commander.adapters import OperationalReads
from commander.backends import (
    backend_policy,
    configured_actions,
    configured_reads,
    read_definitions,
)
from commander.config import Settings
from commander.domain import Contract, ToolResult
from commander.logging_config import configure_logging
from commander.policy import PolicyViolation
from commander.storage import Repository, make_engine
from commander.tools import READ_TOOLS, ToolDefinition, ToolHandler


class ExecutePlan(Contract):
    incident_id: UUID
    plan_id: UUID


class ActionResults(Contract):
    results: list[ToolResult]


def response(data: dict[str, Any]) -> mt.CallToolResult:
    return mt.CallToolResult(
        content=[mt.TextContent(type="text", text=json.dumps(data))], structured_content=data
    )


def failure(exc: Exception) -> mt.CallToolResult:
    kind = (
        "timeout"
        if isinstance(exc, TimeoutError)
        else ("transport" if isinstance(exc, httpx.TransportError) else "invalid")
    )
    return mt.CallToolResult(
        content=[mt.TextContent(type="text", text=str(exc))],
        is_error=True,
        _meta={"commander_error": kind},
    )


def read_server(
    handlers: dict[str, ToolHandler], definitions: dict[str, ToolDefinition] | None = None
) -> Server[Any]:
    definitions = READ_TOOLS if definitions is None else definitions

    async def catalog(
        ctx: ServerRequestContext[Any], params: mt.PaginatedRequestParams | None
    ) -> mt.ListToolsResult:
        return mt.ListToolsResult(
            tools=[
                mt.Tool(
                    name=d.name,
                    description=d.description,
                    input_schema=d.input_schema.model_json_schema(),
                    output_schema=d.output_schema.model_json_schema(),
                    annotations=mt.ToolAnnotations(read_only_hint=True, destructive_hint=False),
                )
                for d in definitions.values()
            ]
        )

    async def call(
        ctx: ServerRequestContext[Any], params: mt.CallToolRequestParams
    ) -> mt.CallToolResult:
        try:
            if params.name not in definitions or params.name not in handlers:
                raise ValueError("Tool is not in the read allowlist")
            definition = definitions[params.name]
            query = definition.input_schema.model_validate(params.arguments or {})
            async with asyncio.timeout(definition.timeout_seconds):
                data = await handlers[params.name](query)
            return response(definition.output_schema.model_validate(data).model_dump(mode="json"))
        except Exception as exc:
            return failure(exc)

    return Server("commander-reads", on_list_tools=catalog, on_call_tool=call)


def action_server(gateway: ActionExecutor) -> Server[Any]:
    async def catalog(
        ctx: ServerRequestContext[Any], params: mt.PaginatedRequestParams | None
    ) -> mt.ListToolsResult:
        return mt.ListToolsResult(
            tools=[
                mt.Tool(
                    name="execute_plan",
                    description="Execute the current persisted, authorized plan",
                    input_schema=ExecutePlan.model_json_schema(),
                    output_schema=ActionResults.model_json_schema(),
                    annotations=mt.ToolAnnotations(read_only_hint=False, destructive_hint=True),
                )
            ]
        )

    async def call(
        ctx: ServerRequestContext[Any], params: mt.CallToolRequestParams
    ) -> mt.CallToolResult:
        try:
            if params.name != "execute_plan":
                raise ValueError("Tool is not in the action allowlist")
            request = ExecutePlan.model_validate(params.arguments or {})
            results = await gateway.execute(request.incident_id, request.plan_id)
            return response(ActionResults(results=results).model_dump(mode="json"))
        except Exception as exc:
            return failure(exc)

    return Server("commander-actions", on_list_tools=catalog, on_call_tool=call)


async def call_data(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    if result.is_error:
        message = "; ".join(c.text for c in result.content if isinstance(c, mt.TextContent))
        kind = (result.meta or {}).get("commander_error")
        if kind == "timeout":
            raise TimeoutError(message)
        if kind == "transport":
            raise httpx.TransportError(message)
        raise PolicyViolation(message)
    if not isinstance(result.structured_content, dict):
        raise ValueError("MCP server omitted structured output")
    return result.structured_content


class MCPReads:
    def __init__(self, client: Client, definitions: dict[str, ToolDefinition] | None = None):
        self.client = client
        self.definitions = READ_TOOLS if definitions is None else definitions

    def handlers(self) -> dict[str, ToolHandler]:
        def handler(name: str) -> ToolHandler:
            async def invoke(query: Contract) -> dict[str, Any]:
                return await call_data(self.client, name, query.model_dump(mode="json"))

            return invoke

        return {name: handler(name) for name in self.definitions}


class MCPActions:
    def __init__(self, client: Client):
        self.client = client

    async def execute(self, incident_id: UUID, plan_id: UUID) -> list[ToolResult]:
        data = await call_data(
            self.client,
            "execute_plan",
            {
                "incident_id": str(incident_id),
                "plan_id": str(plan_id),
            },
        )
        return ActionResults.model_validate(data).results


def server_parameters(
    settings: Settings, boundary: Literal["reads", "actions"]
) -> StdioServerParameters:
    # The SDK inherits only essential OS variables, then merges this explicit environment.
    env = {"DEMO_API_URL": settings.demo_api_url, "DEMO_WORKER_URL": settings.demo_worker_url}
    env["RUNTIME_BACKEND"] = settings.runtime_backend
    env["LOG_FORMAT"] = os.getenv("LOG_FORMAT", "plain")
    if settings.runtime_backend == "kubernetes":
        env.update(
            KUBERNETES_API_URL=settings.kubernetes_api_url,
            KUBERNETES_CA_FILE=settings.kubernetes_ca_file,
        )
        if boundary == "reads":
            env["KUBERNETES_READ_TOKEN_FILE"] = settings.kubernetes_read_token_file
        else:
            env["KUBERNETES_ACTION_TOKEN_FILE"] = settings.kubernetes_action_token_file
    if boundary == "reads":
        env.update(
            INVESTIGATION_DATABASE_URL=settings.investigation_database_url,
            PROMETHEUS_URL=settings.prometheus_url,
        )
    else:
        env.update(
            DATABASE_URL=settings.database_url,
            CONTROL_TOKEN=settings.control_token,
            AUTO_APPROVE_LOW_RISK=str(settings.auto_approve_low_risk),
            ALLOW_MEDIUM_RISK_ACTIONS=str(settings.allow_medium_risk_actions),
        )
    return StdioServerParameters(
        command=sys.executable, args=["-m", "commander.mcp_transport", boundary], env=env
    )


async def serve(boundary: Literal["reads", "actions"]) -> None:
    configure_logging(f"mcp-{boundary}")
    # Never discover the parent's credential file; Pydantic's mypy plugin omits this option.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    database = make_engine(
        settings.investigation_database_url if boundary == "reads" else settings.database_url
    )
    try:
        async with AsyncExitStack() as stack, httpx.AsyncClient(timeout=5) as client:
            if boundary == "reads":
                server = read_server(
                    await configured_reads(
                        stack,
                        settings,
                        OperationalReads(
                            client,
                            database,
                            settings.prometheus_url,
                            settings.demo_api_url,
                            settings.demo_worker_url,
                        ).handlers(),
                    ),
                    read_definitions(settings),
                )
            else:
                server = action_server(
                    ActionGateway(
                        Repository(database),
                        backend_policy(settings),
                        await configured_actions(stack, settings, client),
                    )
                )
            async with stdio_server() as (reader, writer):
                await server.run(reader, writer, server.create_initialization_options())
    finally:
        database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boundary", choices=["reads", "actions"])
    asyncio.run(serve(parser.parse_args().boundary))
