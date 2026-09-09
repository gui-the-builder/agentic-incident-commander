"""A live worker with a supervised consumer task and independent health endpoint."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from commander.config import Settings
from commander.domain import RestartArguments, ScaleArguments, ToolResult, utcnow
from commander.lab_storage import DemoStore
from commander.security import token_dependency
from commander.storage import make_engine
from commander.telemetry import instrument


class Consumer:
    def __init__(self, store: DemoStore, interval: float):
        self.store = store
        self.interval = interval
        self.task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.store.consume)
            except Exception as exc:
                # Database outages must not kill the consumer by retrying the same
                # unavailable database merely to log the original failure.
                entry = {
                    "timestamp": utcnow().isoformat(),
                    "service": "demo-worker",
                    "level": "ERROR",
                    "message": "Worker database operation failed",
                    "error_type": type(exc).__name__,
                }
                logging.getLogger(__name__).error(json.dumps(entry), extra={"event": entry})
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def restart(self) -> None:
        await self.stop()
        await asyncio.to_thread(self.store.change, {"worker_stalled": False}, "demo-worker")
        self.start()
        await asyncio.to_thread(self.store.log, "demo-worker", "INFO", "Worker consumer restarted")


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    database = engine or make_engine(settings.demo_database_url)
    store = DemoStore(database)
    consumer = Consumer(store, settings.worker_poll_seconds)
    registry = CollectorRegistry()
    gauges = {
        name: Gauge(name, name.replace("_", " "), registry=registry)
        for name in (
            "worker_jobs_pending",
            "worker_jobs_processed_total",
            "worker_last_success_timestamp",
        )
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        consumer.start()
        yield
        await consumer.stop()
        if app.state.tracer_provider:
            app.state.tracer_provider.shutdown()
        if engine is None:
            database.dispose()

    app = FastAPI(title="Demo worker", lifespan=lifespan)
    app.state.consumer = consumer
    app.state.tracer_provider = instrument(app, "demo-worker", [database])
    authorized = Depends(token_dependency(settings.control_token))

    @app.get("/health")
    def health() -> dict[str, str]:
        if consumer.task is not None and consumer.task.done():
            raise HTTPException(503, "Worker consumer task stopped")
        return {"status": "healthy"}

    @app.get("/ready")
    def ready() -> dict[str, bool]:
        with database.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ready": True}

    @app.get("/metrics")
    def metrics() -> Response:
        for name, value in store.worker_metrics().items():
            gauges[name].set(value)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/restart", dependencies=[authorized])
    async def restart(body: RestartArguments) -> ToolResult:
        await consumer.restart()
        return ToolResult(ok=True, data={"restarted": "worker-consumer"})

    @app.post("/internal/scale", dependencies=[authorized])
    def scale(body: ScaleArguments) -> ToolResult:
        store.change({"concurrency": body.concurrency}, "demo-worker")
        return ToolResult(ok=True, data={"concurrency": body.concurrency})

    return app
