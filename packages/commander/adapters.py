"""Concrete allowlisted operational reads and constrained service controls."""

import asyncio
import math
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from commander.domain import (
    Action,
    Contract,
    DatabaseQuery,
    DeploymentsQuery,
    LogsQuery,
    MetricsQuery,
    ServiceQuery,
    ToolResult,
    utcnow,
)
from commander.lab_storage import DemoChange, DemoConfig, DemoJob, DemoLog
from commander.tools import ToolHandler


class OperationalReads:
    def __init__(
        self,
        client: httpx.AsyncClient,
        database: Engine,
        prometheus_url: str,
        demo_api_url: str,
        demo_worker_url: str,
    ):
        self.client = client
        self.database = database
        self.prometheus_url = prometheus_url.rstrip("/")
        self.services = {
            "demo-api": demo_api_url.rstrip("/"),
            "demo-worker": demo_worker_url.rstrip("/"),
        }

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            "get_service_health": self.health,
            "query_metrics": self.metrics,
            "query_logs": self.logs,
            "get_service_config": self.config,
            "get_recent_deployments": self.deployments,
            "query_database_readonly": self.database_query,
        }

    async def health(self, arguments: Contract) -> dict[str, Any]:
        query = ServiceQuery.model_validate(arguments.model_dump())
        base = self.services[query.service]
        health, ready = await asyncio.gather(
            self.client.get(base + "/health"), self.client.get(base + "/ready")
        )
        healthy = health.is_success
        ready_ok = ready.is_success
        return {
            "status": "healthy" if healthy and ready_ok else "degraded",
            "instances": [{"id": query.service + "-1", "healthy": healthy, "ready": ready_ok}],
        }

    async def metrics(self, arguments: Contract) -> dict[str, Any]:
        query = MetricsQuery.model_validate(arguments.model_dump())
        selector = f'job="{query.service}"'
        window = f"{query.window_minutes}m"
        if query.metric in {"http_request_duration_seconds", "db_pool_wait_seconds"}:
            if query.aggregation in {"p95", "latest"}:
                expression = (
                    f"histogram_quantile(0.95, sum by (le) "
                    f"(rate({query.metric}_bucket{{{selector}}}[{window}])))"
                )
            else:
                expression = (
                    f"sum(rate({query.metric}_sum{{{selector}}}[{window}])) / "
                    f"sum(rate({query.metric}_count{{{selector}}}[{window}]))"
                )
            unit = "seconds"
        elif query.metric == "http_5xx_rate":
            expression = (
                f"sum(rate(http_5xx_total{{{selector}}}[{window}])) / "
                f"sum(rate(http_requests_total{{{selector}}}[{window}]))"
            )
            unit = "ratio"
        else:
            expression = f"{query.metric}{{{selector}}}"
            if query.aggregation == "average":
                expression = f"avg_over_time({expression}[{window}])"
            elif query.aggregation == "rate":
                expression = f"rate({expression}[{window}])"
            unit = "unix_seconds" if query.metric.endswith("timestamp") else "count"
        response = await self.client.get(
            self.prometheus_url + "/api/v1/query", params={"query": expression}
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise ValueError("Prometheus query did not succeed")
        series = body["data"]["result"]
        if len(series) != 1:
            raise ValueError("Metric is unavailable or ambiguous; check scrape health and traffic")
        value = float(series[0]["value"][1])
        if not math.isfinite(value):
            raise ValueError("Metric has insufficient recent observations")
        return {
            "metric": query.metric,
            "value": value,
            "unit": unit,
            "window_minutes": query.window_minutes,
        }

    async def logs(self, arguments: Contract) -> dict[str, Any]:
        query = LogsQuery.model_validate(arguments.model_dump())

        def read() -> dict[str, Any]:
            with Session(self.database) as session:
                rows = session.scalars(
                    select(DemoLog)
                    .where(
                        DemoLog.service == query.service,
                        DemoLog.level == query.level,
                        DemoLog.timestamp >= utcnow() - timedelta(minutes=query.window_minutes),
                        or_(
                            DemoLog.message.contains(query.contains, autoescape=True),
                            DemoLog.attributes["error_type"]
                            .as_string()
                            .contains(query.contains, autoescape=True),
                        ),
                    )
                    .order_by(DemoLog.timestamp.desc())
                    .limit(query.limit)
                )
                return {
                    "entries": [
                        {
                            "timestamp": row.timestamp.isoformat(),
                            "level": row.level,
                            "message": row.message,
                            "attributes": row.attributes,
                        }
                        for row in rows
                    ]
                }

        return await asyncio.to_thread(read)

    async def config(self, arguments: Contract) -> dict[str, Any]:
        query = ServiceQuery.model_validate(arguments.model_dump())

        def read() -> dict[str, Any]:
            allowed = (
                ("new_checkout_path", "slow_db")
                if query.service == "demo-api"
                else ("concurrency",)
            )
            with Session(self.database) as session:
                values = {
                    row.key: row.value["value"]
                    for row in session.scalars(
                        select(DemoConfig).where(DemoConfig.key.in_(allowed)),
                    )
                }
            if query.service == "demo-api":
                values["pool_size"] = 4
            return values

        return await asyncio.to_thread(read)

    async def deployments(self, arguments: Contract) -> dict[str, Any]:
        query = DeploymentsQuery.model_validate(arguments.model_dump())

        def read() -> dict[str, Any]:
            with Session(self.database) as session:
                rows = session.scalars(
                    select(DemoChange)
                    .where(
                        DemoChange.service == query.service,
                        DemoChange.timestamp >= utcnow() - timedelta(minutes=query.window_minutes),
                    )
                    .order_by(DemoChange.timestamp.desc())
                    .limit(100)
                )
                return {
                    "deployments": [
                        {
                            "timestamp": row.timestamp.isoformat(),
                            "service": row.service,
                            "version": row.version,
                            "changes": row.changes,
                        }
                        for row in rows
                    ]
                }

        return await asyncio.to_thread(read)

    async def database_query(self, arguments: Contract) -> dict[str, Any]:
        query = DatabaseQuery.model_validate(arguments.model_dump())

        def read() -> dict[str, Any]:
            with Session(self.database) as session:
                if query.query_name == "pending_jobs_count":
                    count = session.scalar(
                        select(func.count()).select_from(DemoJob).where(DemoJob.status == "PENDING")
                    )
                    values: dict[str, int | bool] = {"pending_jobs_count": count or 0}
                elif query.query_name == "database_health":
                    values = {"reachable": session.scalar(text("SELECT 1")) == 1}
                else:
                    count = session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database()"
                        )
                    )
                    values = {"database_connections": int(count or 0), "application_pool_size": 4}
                return {"query_name": query.query_name, "values": values}

        return await asyncio.to_thread(read)


class ServiceActions:
    def __init__(self, client: httpx.AsyncClient, api_url: str, worker_url: str, token: str):
        self.client = client
        self.api_url = api_url.rstrip("/")
        self.worker_url = worker_url.rstrip("/")
        self.token = token

    async def execute(self, action: Action, idempotency_key: UUID) -> ToolResult:
        match action.action_type:
            case "restart_demo_api":
                url = self.api_url + "/internal/restart"
            case "restart_demo_worker":
                url = self.worker_url + "/internal/restart"
            case "set_demo_feature_flag":
                url = self.api_url + "/internal/flag"
            case "scale_demo_worker":
                url = self.worker_url + "/internal/scale"
        response = await self.client.post(
            url,
            json=action.arguments.model_dump(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Idempotency-Key": str(idempotency_key),
            },
        )
        response.raise_for_status()
        return ToolResult.model_validate(response.json())
