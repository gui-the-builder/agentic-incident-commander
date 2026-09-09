"""Checkout API with real bounded connection-pool pressure and job production."""

import os
import signal
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session

from commander.config import Settings
from commander.domain import Contract, FlagArguments, RestartArguments, ToolResult
from commander.lab_storage import DemoJob, DemoStore
from commander.security import token_dependency
from commander.storage import make_engine
from commander.telemetry import instrument


class JobsRequest(Contract):
    pass


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    restart_process: Callable[[], None] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    control_engine = engine or make_engine(settings.demo_database_url)
    checkout_engine = engine or create_engine(
        settings.demo_database_url, pool_size=4, max_overflow=0, pool_timeout=3, pool_pre_ping=True
    )
    store = DemoStore(control_engine)
    registry = CollectorRegistry()
    requests = Counter("http_requests_total", "Completed checkouts", ["status"], registry=registry)
    errors = Counter("http_5xx_total", "Failed checkouts", registry=registry)
    latency = Histogram(
        "http_request_duration_seconds",
        "Checkout latency",
        registry=registry,
        buckets=(0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5, 10),
    )
    wait = Histogram(
        "db_pool_wait_seconds",
        "Connection acquisition time",
        registry=registry,
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1, 2, 3, 5),
    )
    active = Gauge("db_pool_in_use", "Checked out DB connections", registry=registry)

    @event.listens_for(checkout_engine, "checkout")
    def on_checkout(*_: Any) -> None:
        active.inc()

    @event.listens_for(checkout_engine, "checkin")
    def on_checkin(*_: Any) -> None:
        active.dec()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        provider = app.state.tracer_provider
        if provider:
            provider.shutdown()
        if engine is None:
            checkout_engine.dispose()
            control_engine.dispose()

    app = FastAPI(title="Demo checkout API", lifespan=lifespan)
    app.state.store = store
    app.state.tracer_provider = instrument(app, "demo-api", [checkout_engine, control_engine])
    authorized = Depends(token_dependency(settings.control_token))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    def ready() -> dict[str, bool]:
        with control_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ready": True}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/checkout")
    def checkout() -> dict[str, str]:
        started = time.monotonic()
        status = 200
        acquired_at = started
        request_id = str(uuid4())
        try:
            config = store.config()
            acquiring = time.monotonic()
            with checkout_engine.connect() as connection:
                acquired_at = time.monotonic()
                duration = acquired_at - acquiring
                wait.observe(duration)
                if duration > 0.05:
                    store.log(
                        "demo-api",
                        "WARNING",
                        "database connection acquisition wait",
                        wait_seconds=duration,
                        request_id=request_id,
                    )
                if config.get("slow_db"):
                    time.sleep(2.5)  # Deliberately hold an actual checked-out DB connection.
                connection.execute(text("SELECT 1"))
                if config.get("new_checkout_path"):
                    status = 500
                    store.log(
                        "demo-api",
                        "ERROR",
                        "CheckoutPathError: new checkout path failed",
                        error_type="CheckoutPathError",
                        request_id=request_id,
                    )
                    raise HTTPException(500, "Checkout could not be completed")
                with Session(bind=connection) as session, session.begin():
                    job = DemoJob(id=str(uuid4()))
                    session.add(job)
                    job_id = job.id
                # The connection owns the implicit transaction from SELECT 1.
                connection.commit()
            return {"status": "accepted", "job_id": job_id, "request_id": request_id}
        except PoolTimeout as exc:
            status = 503
            wait.observe(time.monotonic() - acquired_at)
            store.log(
                "demo-api",
                "ERROR",
                "database connection acquisition timed out",
                error_type="PoolTimeout",
                request_id=request_id,
            )
            raise HTTPException(503, "Database connection acquisition timed out") from exc
        except HTTPException:
            raise
        except Exception:
            status = 503
            store.log(
                "demo-api",
                "ERROR",
                "Checkout database operation failed",
                error_type="DatabaseError",
                request_id=request_id,
            )
            raise
        finally:
            requests.labels(status=str(status)).inc()
            if status >= 500:
                errors.inc()
            latency.observe(time.monotonic() - started)

    @app.post("/jobs")
    def enqueue(body: JobsRequest) -> dict[str, list[str]]:
        return {"job_ids": store.enqueue()}

    @app.get("/jobs")
    def jobs() -> dict[str, float]:
        return store.worker_metrics()

    @app.post("/internal/flag", dependencies=[authorized])
    def flag(body: FlagArguments) -> ToolResult:
        store.change({body.flag: body.value}, "demo-api")
        return ToolResult(ok=True, data={"flag": body.flag, "value": body.value})

    @app.post("/internal/restart", dependencies=[authorized])
    def restart(body: RestartArguments, background: BackgroundTasks) -> ToolResult:
        def terminate_self() -> None:
            # The Compose restart policy supervises this one demo service process.
            os.kill(os.getpid(), signal.SIGTERM)

        store.log("demo-api", "INFO", "Demo API restart scheduled", reason=body.reason)
        background.add_task(restart_process or terminate_self)
        return ToolResult(ok=True, data={"restart_scheduled": "demo-api"})

    return app
