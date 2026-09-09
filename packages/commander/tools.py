"""Typed read-tool registry with per-attempt, durable audit records."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import Field, ValidationError

from commander.domain import (
    Contract,
    DatabaseQuery,
    DeploymentsQuery,
    Evidence,
    LogsQuery,
    MetricsQuery,
    ServiceQuery,
    ToolResult,
    utcnow,
)
from commander.storage import Repository, ToolCallRow


class Instance(Contract):
    id: str
    healthy: bool
    ready: bool


class HealthOutput(Contract):
    status: Literal["healthy", "degraded", "unavailable"]
    instances: list[Instance]


class MetricOutput(Contract):
    metric: str
    value: float = Field(allow_inf_nan=False)
    unit: str
    window_minutes: int


class LogEntry(Contract):
    timestamp: str
    level: str
    message: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class LogsOutput(Contract):
    entries: list[LogEntry]


class Deployment(Contract):
    timestamp: str
    service: str
    version: str
    changes: dict[str, str | bool | int]


class DeploymentsOutput(Contract):
    deployments: list[Deployment]


class ConfigOutput(Contract):
    new_checkout_path: bool | None = None
    slow_db: bool | None = None
    pool_size: int | None = None
    concurrency: int | None = None


class DatabaseOutput(Contract):
    query_name: str
    values: dict[str, int | float | bool]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: type[Contract]
    output_schema: type[Contract]
    timeout_seconds: float = 5
    max_retries: int = 1
    read_only: bool = True
    risk: str = "READ_ONLY"
    idempotent: bool = True
    retryable_errors: tuple[type[Exception], ...] = (TimeoutError, httpx.TransportError)


READ_TOOLS = {
    t.name: t
    for t in (
        ToolDefinition(
            "get_service_health", "Inspect liveness and readiness", ServiceQuery, HealthOutput
        ),
        ToolDefinition(
            "query_metrics", "Read an allowlisted operational metric", MetricsQuery, MetricOutput
        ),
        ToolDefinition("query_logs", "Search bounded operational logs", LogsQuery, LogsOutput),
        ToolDefinition(
            "get_recent_deployments",
            "Inspect recent build/config changes",
            DeploymentsQuery,
            DeploymentsOutput,
        ),
        ToolDefinition(
            "get_service_config",
            "Read public operational configuration",
            ServiceQuery,
            ConfigOutput,
        ),
        ToolDefinition(
            "query_database_readonly",
            "Run a named read-only database query",
            DatabaseQuery,
            DatabaseOutput,
        ),
    )
}

ToolHandler = Callable[[Contract], Awaitable[dict[str, Any]]]


class ReadGateway:
    def __init__(
        self,
        repository: Repository,
        handlers: dict[str, ToolHandler],
        definitions: dict[str, ToolDefinition] | None = None,
    ):
        self.repository = repository
        self.handlers = handlers
        self.definitions = READ_TOOLS if definitions is None else definitions

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema.model_json_schema(),
            }
            for tool in self.definitions.values()
        ]

    async def call(self, incident_id: UUID, name: str, arguments: dict[str, Any]) -> ToolResult:
        definition = self.definitions.get(name)
        try:
            if definition is None or not definition.read_only or name not in self.handlers:
                raise ValueError("Tool is not in the read allowlist")
            parsed = definition.input_schema.model_validate(arguments)
        except (ValueError, ValidationError) as exc:
            result = ToolResult(ok=False, error=str(exc))
            self.repository.add_record(
                ToolCallRow,
                incident_id,
                {
                    "tool_name": name,
                    "arguments": arguments,
                    "attempt": 0,
                    "status": "BLOCKED",
                    "started_at": utcnow().isoformat(),
                    "completed_at": utcnow().isoformat(),
                    "duration_ms": 0,
                    "result": result.model_dump(mode="json"),
                    "error": str(exc),
                },
                status="BLOCKED",
            )
            return result

        for attempt in range(1, definition.max_retries + 2):
            started = time.monotonic()
            call_id = self.repository.add_record(
                ToolCallRow,
                incident_id,
                {
                    "tool_name": name,
                    "arguments": parsed.model_dump(mode="json"),
                    "attempt": attempt,
                    "started_at": utcnow().isoformat(),
                    "status": "STARTED",
                },
                status="STARTED",
            )
            retryable = False
            try:
                async with asyncio.timeout(definition.timeout_seconds):
                    raw = await self.handlers[name](parsed)
                output = definition.output_schema.model_validate(raw)
                result = ToolResult(ok=True, data=output.model_dump(mode="json"))
                status = "SUCCEEDED"
            except Exception as exc:
                retryable = isinstance(exc, definition.retryable_errors)
                status = "TIMED_OUT" if isinstance(exc, TimeoutError) else "FAILED"
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            self.repository.finish_tool(
                call_id,
                status,
                {
                    "completed_at": utcnow().isoformat(),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "result": result.model_dump(mode="json"),
                    "error": result.error,
                },
            )
            if result.ok:
                self.repository.add_evidence(
                    Evidence(
                        incident_id=incident_id,
                        tool_call_id=call_id,
                        evidence_type=name,
                        source=name,
                        summary=json.dumps(result.data)[:1000],
                        payload=result.data,
                        observed_at=result.observed_at,
                    )
                )
                return result
            if not retryable or attempt > definition.max_retries:
                return result
        raise AssertionError("Tool retry loop must produce a result")
