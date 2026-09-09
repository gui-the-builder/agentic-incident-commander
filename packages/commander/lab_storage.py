"""Actual demo workload data. Scenario expectations are never stored here."""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from commander.domain import utcnow
from commander.storage import JSON_TYPE, Base

logger = logging.getLogger("incident-lab")


class DemoConfig(Base):
    __tablename__ = "demo_config"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class DemoJob(Base):
    __tablename__ = "demo_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DemoLog(Base):
    __tablename__ = "demo_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(40), index=True)
    level: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(String(1000))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)


class DemoChange(Base):
    __tablename__ = "demo_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(80), default="local-v1")
    changes: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class DemoWorkerState(Base):
    __tablename__ = "demo_worker_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DemoStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def seed(self) -> None:
        with Session(self.engine) as session, session.begin():
            for key, value in (
                ("new_checkout_path", False),
                ("slow_db", False),
                ("worker_stalled", False),
                ("concurrency", 1),
            ):
                if session.get(DemoConfig, key) is None:
                    session.add(DemoConfig(key=key, value={"value": value}))
            if session.get(DemoWorkerState, 1) is None:
                session.add(DemoWorkerState(id=1, processed=0))

    def config(self) -> dict[str, Any]:
        with Session(self.engine) as session:
            return {row.key: row.value["value"] for row in session.scalars(select(DemoConfig))}

    def change(self, changes: dict[str, Any], service: str) -> None:
        allowed = {"new_checkout_path", "slow_db", "worker_stalled", "concurrency"}
        if not changes or not changes.keys() <= allowed:
            raise ValueError("Unknown operational configuration")
        with Session(self.engine) as session, session.begin():
            for key, value in changes.items():
                row = session.get(DemoConfig, key)
                if row is None:
                    raise RuntimeError("Demo database must be seeded before configuration changes")
                row.value = {"value": value}
            # Worker stall is an injected internal fault, not a public configuration flag.
            public = {key: value for key, value in changes.items() if key != "worker_stalled"}
            if public:
                session.add(DemoChange(service=service, changes=public))

    def reset(self) -> None:
        with Session(self.engine) as session, session.begin():
            for key, value in (
                ("new_checkout_path", False),
                ("slow_db", False),
                ("worker_stalled", False),
                ("concurrency", 1),
            ):
                row = session.get(DemoConfig, key)
                if row is None:
                    session.add(DemoConfig(key=key, value={"value": value}))
                else:
                    row.value = {"value": value}
            session.execute(delete(DemoJob))
            session.execute(delete(DemoLog))
            session.execute(delete(DemoChange))
            state = session.get(DemoWorkerState, 1)
            if state:
                state.processed = 0
                state.last_success = None
            else:
                session.add(DemoWorkerState(id=1, processed=0))

    def enqueue(self, count: int = 1) -> list[str]:
        with Session(self.engine) as session, session.begin():
            jobs = [DemoJob(id=str(uuid4())) for _ in range(count)]
            session.add_all(jobs)
            return [job.id for job in jobs]

    def consume(self) -> int:
        with Session(self.engine) as session, session.begin():
            stalled = session.get(DemoConfig, "worker_stalled")
            if stalled and stalled.value["value"]:
                return 0
            concurrency = session.get(DemoConfig, "concurrency")
            batch = int(concurrency.value["value"]) * 8 if concurrency else 8
            jobs = list(
                session.scalars(
                    select(DemoJob)
                    .where(DemoJob.status == "PENDING")
                    .order_by(DemoJob.created_at)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            )
            state = session.get(DemoWorkerState, 1, with_for_update=True)
            if state is None:
                raise RuntimeError("Worker state is not seeded")
            for job in jobs:
                job.status = "COMPLETED"
                job.completed_at = utcnow()
            if jobs:
                state.processed += len(jobs)
                state.last_success = utcnow()
            return len(jobs)

    def worker_metrics(self) -> dict[str, float]:
        with Session(self.engine) as session:
            state = session.get(DemoWorkerState, 1)
            pending = (
                session.scalar(
                    select(func.count()).select_from(DemoJob).where(DemoJob.status == "PENDING")
                )
                or 0
            )
            return {
                "worker_jobs_pending": float(pending),
                "worker_jobs_processed_total": float(state.processed) if state else 0,
                "worker_last_success_timestamp": (
                    state.last_success.replace(tzinfo=UTC).timestamp()
                    if state and state.last_success
                    else 0
                ),
            }

    def log(self, service: str, level: str, message: str, **attributes: Any) -> None:
        entry = {
            "timestamp": utcnow().isoformat(),
            "service": service,
            "level": level,
            "message": message,
            "attributes": attributes,
        }
        logger.log(getattr(logging, level), json.dumps(entry), extra={"event": entry})
        with Session(self.engine) as session, session.begin():
            session.add(
                DemoLog(service=service, level=level, message=message, attributes=attributes)
            )
