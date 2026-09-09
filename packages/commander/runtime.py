"""Durable polling worker. Restarting discovers unfinished incident checkpoints."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, HTTPException, Response
from mcp import Client
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from commander.actions import ActionExecutor, ActionGateway
from commander.adapters import OperationalReads
from commander.backends import (
    backend_policy,
    configured_actions,
    configured_reads,
    read_definitions,
)
from commander.config import Settings
from commander.logging_config import configure_logging
from commander.mcp_transport import MCPActions, MCPReads, server_parameters
from commander.models import AuditedModel, OllamaModel
from commander.storage import Conflict, Repository, make_engine
from commander.tools import ReadGateway
from commander.workflow import Workflow

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging("agent-runtime")
    settings = settings or Settings()
    database = make_engine(settings.database_url)
    investigation = make_engine(settings.investigation_database_url)
    repository = Repository(database)
    registry = CollectorRegistry()
    runs = Counter("agent_runs_total", "Workflow invocations", ["outcome"], registry=registry)
    duration = Histogram(
        "agent_run_duration_seconds",
        "Workflow invocation duration",
        registry=registry,
        buckets=(1, 5, 10, 30, 60, 120, 300, 600),
    )

    async def poll() -> None:
        async with (
            AsyncExitStack() as stack,
            httpx.AsyncClient(timeout=5) as client,
            httpx.AsyncClient(
                base_url=settings.ollama_base_url, timeout=settings.model_timeout_seconds
            ) as model_client,
        ):
            policy = backend_policy(settings)
            definitions = read_definitions(settings)
            reads = OperationalReads(
                client,
                investigation,
                settings.prometheus_url,
                settings.demo_api_url,
                settings.demo_worker_url,
            )
            actions: ActionExecutor
            if settings.tool_transport == "mcp":
                read_client = await stack.enter_async_context(
                    Client(server_parameters(settings, "reads"), read_timeout_seconds=10)
                )
                action_client = await stack.enter_async_context(
                    Client(server_parameters(settings, "actions"), read_timeout_seconds=60)
                )
                handlers = MCPReads(read_client, definitions).handlers()
                actions = MCPActions(action_client)
            else:
                handlers = await configured_reads(stack, settings, reads.handlers())
                actions = ActionGateway(
                    repository, policy, await configured_actions(stack, settings, client)
                )
            while True:
                try:
                    for incident_id in repository.runnable_incidents():
                        _, _, checkpoint = repository.load(incident_id)
                        if checkpoint.get(
                            "next_node"
                        ) == "await_approval" and not repository.approval(
                            checkpoint["plan_id"],
                        ):
                            continue
                        started = time.monotonic()
                        try:
                            workflow = Workflow(
                                repository,
                                AuditedModel(
                                    OllamaModel(model_client, settings.ollama_model),
                                    repository,
                                    incident_id,
                                ),
                                ReadGateway(repository, handlers, definitions),
                                actions,
                                policy,
                                settings.max_investigation_iterations,
                                settings.verification_interval_seconds,
                                runtime_backend=settings.runtime_backend,
                            )
                            await workflow.run(incident_id)
                            runs.labels(outcome=repository.load(incident_id)[0].status).inc()
                        except Conflict:
                            pass  # Another runtime owns the PostgreSQL advisory lock.
                        finally:
                            duration.observe(time.monotonic() - started)
                except Exception:
                    logger.exception("Incident polling failed; durable work will be retried")
                await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.poller = asyncio.create_task(poll())
        yield
        app.state.poller.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.poller
        database.dispose()
        investigation.dispose()

    app = FastAPI(title="Agent runtime", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        if app.state.poller.done():
            raise HTTPException(503, "Incident poller stopped")
        return {"status": "healthy"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app
