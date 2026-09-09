"""Durable records and optimistic workflow checkpoints.

PostgreSQL is the application backend. SQLite is supported solely for isolated
unit tests. JSON payloads retain the complete validated domain contracts.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from commander.domain import (
    Alert,
    Approval,
    ApprovalRequest,
    Evidence,
    Hypothesis,
    Incident,
    RemediationPlan,
    State,
    Status,
    utcnow,
)

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")
_TEST_LOCKS: dict[UUID, Lock] = {}


class Base(DeclarativeBase):
    pass


class IncidentRow(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_state: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)


class Record(Base):
    __abstract__ = True
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class TransitionRow(Record):
    __tablename__ = "state_transitions"
    __table_args__ = (UniqueConstraint("incident_id", "revision"),)
    revision: Mapped[int] = mapped_column(Integer)


class ToolCallRow(Record):
    __tablename__ = "tool_calls"
    status: Mapped[str] = mapped_column(String(20))


class ModelCallRow(Record):
    __tablename__ = "model_calls"
    status: Mapped[str] = mapped_column(String(20))


class EvidenceRow(Record):
    __tablename__ = "evidence"
    tool_call_id: Mapped[str] = mapped_column(ForeignKey("tool_calls.id"))


class HypothesisRow(Record):
    __tablename__ = "hypotheses"


class PlanRow(Record):
    __tablename__ = "remediation_plans"
    diagnosis_hypothesis_id: Mapped[str] = mapped_column(ForeignKey("hypotheses.id"))


class ActionRow(Record):
    __tablename__ = "remediation_actions"
    __table_args__ = (UniqueConstraint("plan_id", "order_index"),)
    plan_id: Mapped[str] = mapped_column(ForeignKey("remediation_plans.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")


class ApprovalRow(Record):
    __tablename__ = "approvals"
    plan_id: Mapped[str] = mapped_column(ForeignKey("remediation_plans.id"), unique=True)


class VerificationRow(Record):
    __tablename__ = "verification_results"
    plan_id: Mapped[str] = mapped_column(ForeignKey("remediation_plans.id"))


class PostmortemRow(Record):
    __tablename__ = "postmortems"


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(connection: Any, _: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


class Conflict(ValueError):
    """Another worker advanced the incident, or the requested operation is stale."""


class NotFound(LookupError):
    pass


class Repository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def workflow_lock(self, incident_id: UUID) -> Iterator[None]:
        """Hold a session advisory lock across one run, released even after failure."""
        if self.engine.dialect.name == "postgresql":
            key = incident_id.int & ((1 << 63) - 1)
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                )
                if not acquired:
                    raise Conflict("Incident already has an active worker")
                try:
                    yield
                finally:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        else:
            lock = _TEST_LOCKS.setdefault(incident_id, Lock())
            if not lock.acquire(blocking=False):
                raise Conflict("Incident already has an active worker")
            try:
                yield
            finally:
                lock.release()

    def runnable_incidents(self) -> list[UUID]:
        with Session(self.engine) as session:
            return [
                UUID(value)
                for value in session.scalars(
                    select(IncidentRow.id).where(
                        IncidentRow.checkpoint["next_node"].as_string() != "done",
                    )
                )
            ]

    def claim_action(self, plan_id: UUID, index: int) -> tuple[UUID, str, dict[str, Any]]:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(ActionRow)
                .where(
                    ActionRow.plan_id == str(plan_id),
                    ActionRow.order_index == index,
                )
                .with_for_update()
            )
            if row is None:
                raise NotFound("Remediation action not found")
            previous = row.status
            if previous == "PENDING":
                row.status = "STARTED"
            return UUID(row.id), previous, row.data

    def finish_action(self, action_id: UUID, status: str, result: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(ActionRow, str(action_id))
            if row is None:
                raise NotFound("Remediation action not found")
            row.status = status
            row.data = {**row.data, "result": result, "executed_at": utcnow().isoformat()}

    def create_incident(self, alert: Alert, scenario_id: str | None = None) -> Incident:
        incident = Incident(**alert.model_dump(), scenario_id=scenario_id)
        with Session(self.engine) as session, session.begin():
            session.add(
                IncidentRow(
                    id=str(incident.id),
                    status=incident.status,
                    current_state=incident.current_state,
                    data=incident.model_dump(mode="json"),
                    checkpoint={"next_node": "normalize_alert"},
                )
            )
            session.flush()
            session.add(
                TransitionRow(
                    incident_id=str(incident.id),
                    revision=0,
                    data={"from_state": None, "to_state": "RECEIVED", "reason": "Alert received"},
                )
            )
        return incident

    def load(self, incident_id: UUID) -> tuple[Incident, int, dict[str, Any]]:
        with Session(self.engine) as session:
            row = session.get(IncidentRow, str(incident_id))
            if row is None:
                raise NotFound("Incident not found")
            return Incident.model_validate(row.data), row.revision, row.checkpoint

    def advance(
        self,
        incident_id: UUID,
        revision: int,
        state: State,
        checkpoint: dict[str, Any],
        reason: str,
        status: Status | None = None,
    ) -> int:
        with Session(self.engine) as session, session.begin():
            row = session.get(IncidentRow, str(incident_id))
            if row is None:
                raise NotFound("Incident not found")
            incident = Incident.model_validate(row.data)
            previous = incident.current_state
            incident.current_state = state
            incident.updated_at = utcnow()
            if incident.started_at is None:
                incident.started_at = incident.updated_at
            if status is not None:
                incident.status = status
            if status == Status.RESOLVED:
                incident.resolved_at = incident.updated_at
            updated_id = session.scalar(
                update(IncidentRow)
                .where(
                    IncidentRow.id == str(incident_id),
                    IncidentRow.revision == revision,
                )
                .values(
                    revision=revision + 1,
                    current_state=state,
                    status=incident.status,
                    data=incident.model_dump(mode="json"),
                    checkpoint=checkpoint,
                )
                .returning(IncidentRow.id)
            )
            if updated_id is None:
                raise Conflict("Workflow checkpoint has changed")
            session.add(
                TransitionRow(
                    incident_id=str(incident_id),
                    revision=revision + 1,
                    data={"from_state": previous, "to_state": state, "reason": reason},
                )
            )
        return revision + 1

    def add_record(
        self,
        row_type: type[Record],
        incident_id: UUID,
        data: dict[str, Any],
        **fields: Any,
    ) -> UUID:
        record_id = UUID(str(data.get("id", uuid4())))
        with Session(self.engine) as session, session.begin():
            session.add(
                row_type(
                    id=str(record_id),
                    incident_id=str(incident_id),
                    data=data,
                    **fields,
                )
            )
        return record_id

    def records(self, row_type: type[Record], incident_id: UUID) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(row_type)
                .where(
                    row_type.incident_id == str(incident_id),
                )
                .order_by(row_type.created_at, row_type.id)
            )
            return [{**row.data, "id": row.id} for row in rows]

    def remediation_history(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Read plan-bound action outcomes and checks, including interrupted attempts."""
        with Session(self.engine) as session:
            approvals = {
                row.plan_id: row.data
                for row in session.scalars(
                    select(ApprovalRow).where(ApprovalRow.incident_id == str(incident_id))
                )
            }
            plans = list(
                session.scalars(
                    select(PlanRow)
                    .where(PlanRow.incident_id == str(incident_id))
                    .order_by(PlanRow.created_at, PlanRow.id)
                )
            )
            actions = list(
                session.scalars(
                    select(ActionRow)
                    .where(ActionRow.incident_id == str(incident_id))
                    .order_by(ActionRow.order_index)
                )
            )
            checks = list(
                session.scalars(
                    select(VerificationRow)
                    .where(VerificationRow.incident_id == str(incident_id))
                    .order_by(VerificationRow.created_at, VerificationRow.id)
                )
            )
            return [
                {
                    "plan_id": plan.id,
                    "diagnosis_hypothesis_id": plan.diagnosis_hypothesis_id,
                    "created_at": plan.data["created_at"],
                    "risk": plan.data["risk"],
                    "approval_required": plan.data["approval_required"],
                    "approval": approvals.get(plan.id),
                    "summary": plan.data["summary"],
                    "expected_checks": plan.data["verification_checks"],
                    "actions": [
                        {**action.data, "action_id": action.id, "status": action.status}
                        for action in actions
                        if action.plan_id == plan.id
                    ],
                    "verification": [
                        {**check.data, "id": check.id}
                        for check in checks
                        if check.plan_id == plan.id
                    ],
                }
                for plan in plans
            ]

    def add_evidence(self, evidence: Evidence) -> None:
        with Session(self.engine) as session, session.begin():
            call = session.get(ToolCallRow, str(evidence.tool_call_id))
            if call is None or call.incident_id != str(evidence.incident_id):
                raise Conflict("Evidence tool call must belong to the same incident")
            if call.status != "SUCCEEDED":
                raise Conflict("Failed tool results cannot become supporting evidence")
            session.add(
                EvidenceRow(
                    id=str(evidence.id),
                    incident_id=str(evidence.incident_id),
                    tool_call_id=str(evidence.tool_call_id),
                    data=evidence.model_dump(mode="json"),
                )
            )

    def evidence(self, incident_id: UUID) -> list[Evidence]:
        return [Evidence.model_validate(r) for r in self.records(EvidenceRow, incident_id)]

    def hypotheses(self, incident_id: UUID) -> list[Hypothesis]:
        return [Hypothesis.model_validate(r) for r in self.records(HypothesisRow, incident_id)]

    def select_hypothesis(self, incident_id: UUID, hypothesis_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(HypothesisRow, str(hypothesis_id))
            if row is None or row.incident_id != str(incident_id):
                raise Conflict("Selected hypothesis must belong to this incident")
            row.data = {**row.data, "status": "SELECTED", "updated_at": utcnow().isoformat()}

    def save_plan(self, plan: RemediationPlan) -> None:
        with Session(self.engine) as session, session.begin():
            hypothesis = session.get(HypothesisRow, str(plan.diagnosis_hypothesis_id))
            if hypothesis is None or hypothesis.incident_id != str(plan.incident_id):
                raise Conflict("Plan diagnosis must belong to the same incident")
            session.add(
                PlanRow(
                    id=str(plan.id),
                    incident_id=str(plan.incident_id),
                    diagnosis_hypothesis_id=str(plan.diagnosis_hypothesis_id),
                    data=plan.model_dump(mode="json"),
                )
            )
            session.flush()
            for index, action in enumerate(plan.actions):
                session.add(
                    ActionRow(
                        incident_id=str(plan.incident_id),
                        plan_id=str(plan.id),
                        order_index=index,
                        data=action.model_dump(mode="json"),
                    )
                )

    def plan(self, plan_id: UUID) -> RemediationPlan:
        with Session(self.engine) as session:
            row = session.get(PlanRow, str(plan_id))
            if row is None:
                raise NotFound("Remediation plan not found")
            return RemediationPlan.model_validate(row.data)

    def approve(self, incident_id: UUID, request: ApprovalRequest) -> Approval:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(IncidentRow)
                .where(
                    IncidentRow.id == str(incident_id),
                )
                .with_for_update()
            )
            if row is None:
                raise NotFound("Incident not found")
            if row.current_state != State.AWAITING_APPROVAL:
                raise Conflict("Incident is not waiting for approval")
            plan_id = row.checkpoint.get("plan_id")
            plan_row = session.get(PlanRow, plan_id)
            if plan_row is None or plan_row.incident_id != str(incident_id):
                raise Conflict("No current remediation plan")
            if session.scalar(select(ApprovalRow).where(ApprovalRow.plan_id == plan_id)):
                raise Conflict("This plan already has an operator decision")
            approval = Approval(
                **request.model_dump(),
                incident_id=incident_id,
                remediation_plan_id=UUID(plan_id),
            )
            session.add(
                ApprovalRow(
                    id=str(approval.id),
                    incident_id=str(incident_id),
                    plan_id=plan_id,
                    data=approval.model_dump(mode="json"),
                )
            )
            return approval

    def approval(self, plan_id: UUID) -> Approval | None:
        with Session(self.engine) as session:
            row = session.scalar(select(ApprovalRow).where(ApprovalRow.plan_id == str(plan_id)))
            return Approval.model_validate(row.data) if row else None

    def finish_tool(self, call_id: UUID, status: str, result: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(ToolCallRow, str(call_id))
            if row is None:
                raise NotFound("Tool call not found")
            row.status = status
            row.data = {**row.data, **result, "status": status}

    def finish_model(self, call_id: UUID, status: str, result: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(ModelCallRow, str(call_id))
            if row is None:
                raise NotFound("Model call not found")
            row.status = status
            row.data = {**row.data, **result, "status": status}

    def timeline(self, incident_id: UUID) -> list[dict[str, Any]]:
        self.load(incident_id)
        events = []
        with Session(self.engine) as session:
            for row_type, kind in (
                (TransitionRow, "state_transition"),
                (ToolCallRow, "tool_call"),
                (ModelCallRow, "model_call"),
                (ApprovalRow, "approval"),
                (VerificationRow, "verification"),
            ):
                for row in session.scalars(
                    select(row_type).where(
                        row_type.incident_id == str(incident_id),
                    )
                ):
                    events.append(
                        {
                            "id": row.id,
                            "timestamp": row.created_at.isoformat(),
                            "type": kind,
                            "summary": row.data.get("reason", row.data.get("tool_name", kind)),
                            "data": {
                                **row.data,
                                **(
                                    {"plan_id": row.plan_id}
                                    if isinstance(row, VerificationRow)
                                    else {}
                                ),
                            },
                        }
                    )
        return sorted(events, key=lambda e: (str(e["timestamp"]), str(e["id"])))
