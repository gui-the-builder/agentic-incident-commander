"""Validated contracts shared by the API, workflow and tool gateway."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError


def utcnow() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Status(StrEnum):
    OPEN = "OPEN"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class State(StrEnum):
    RECEIVED = "RECEIVED"
    CONTEXT_GATHERING = "CONTEXT_GATHERING"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    INVESTIGATION = "INVESTIGATION"
    DIAGNOSED = "DIAGNOSED"
    REMEDIATION_PLANNED = "REMEDIATION_PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    POSTMORTEM_GENERATED = "POSTMORTEM_GENERATED"


class Risk(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


Service = Literal["demo-api", "demo-worker"]
Metric = Literal[
    "http_request_duration_seconds",
    "http_5xx_rate",
    "db_pool_in_use",
    "db_pool_wait_seconds",
    "worker_jobs_pending",
    "worker_jobs_processed_total",
    "worker_last_success_timestamp",
]


class Alert(Contract):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    service: Service
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] = "SEV2"


class Incident(Alert):
    id: UUID = Field(default_factory=uuid4)
    status: Status = Status.OPEN
    current_state: State = State.RECEIVED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    resolved_at: datetime | None = None
    scenario_id: str | None = None

    def agent_alert(self) -> Alert:
        return Alert(**self.model_dump(include=set(Alert.model_fields)))


class ToolResult(Contract):
    ok: bool
    observed_at: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @model_validator(mode="after")
    def consistent_error(self) -> "ToolResult":
        if self.ok == (self.error is not None):
            raise ValueError("successful results have no error; failed results require an error")
        return self


class Evidence(Contract):
    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    tool_call_id: UUID
    evidence_type: str
    source: str
    summary: str
    payload: dict[str, Any]
    observed_at: datetime
    relevance: float | None = Field(default=None, ge=0, le=1)
    created_at: datetime = Field(default_factory=utcnow)


class HypothesisDraft(Contract):
    statement: str = Field(min_length=1, max_length=2000)
    mechanism: Literal[
        "connection_pool_exhaustion",
        "checkout_flag_regression",
        "worker_consumer_stall",
        "worker_replicas_zero",
        "unknown",
    ] = "unknown"
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list)

    @field_validator("supporting_evidence_ids", "contradicting_evidence_ids")
    @classmethod
    def known_evidence_ids(cls, value: list[UUID], info: ValidationInfo) -> list[UUID]:
        # Loading historical rows remains independent of the current prompt.
        # Model output validation supplies the exact incident evidence IDs.
        if info.context is not None and "evidence_ids" in info.context:
            if not {str(reference) for reference in value} <= info.context["evidence_ids"]:
                raise PydanticCustomError(
                    "unknown_evidence_reference",
                    "Evidence references must match IDs from the supplied context",
                )
        return value


class Hypothesis(HypothesisDraft):
    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    status: Literal["ACTIVE", "REJECTED", "SELECTED"] = "ACTIVE"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class HypothesisSet(Contract):
    hypotheses: list[HypothesisDraft] = Field(min_length=1, max_length=5)


class InvestigationChoice(Contract):
    tool: str
    arguments: dict[str, Any]
    rationale: str = Field(min_length=1)


class Diagnosis(Contract):
    hypothesis_id: UUID
    acknowledged_failed_tool_call_ids: list[UUID] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class RestartArguments(Contract):
    reason: str = Field(min_length=1, max_length=1000)


class FlagArguments(RestartArguments):
    flag: Literal["new_checkout_path", "slow_db"] = Field(
        description=(
            "demo-api operational flag. new_checkout_path selects the alternative checkout "
            "code path. slow_db makes each checkout hold its pooled database connection for "
            "extra time. Flags are read on every request."
        )
    )
    value: bool


class ScaleArguments(RestartArguments):
    concurrency: int = Field(ge=1, le=4, strict=True)


# Docstrings below are published as factual action semantics in the model's
# remediation schema. They describe what each control does, never which
# scenario is active or what its expected root cause is.


class RestartWorker(Contract):
    """Restart the demo-worker job consumer task in place. It replaces a consumer that stopped
    draining jobs; it changes no demo-api behaviour and no configuration flag."""

    action_type: Literal["restart_demo_worker"]
    arguments: RestartArguments


class RestartAPI(Contract):
    """Restart the demo-api process under its supervisor. Configuration flags persist
    across restarts, so a restart cannot undo a flag change."""

    action_type: Literal["restart_demo_api"]
    arguments: RestartArguments


class SetFlag(Contract):
    """Set one allowlisted demo-api flag to a boolean value. This is the only action that
    changes demo-api configuration."""

    action_type: Literal["set_demo_feature_flag"]
    arguments: FlagArguments


class ScaleWorker(Contract):
    """Change demo-worker job-consumer capacity (1-4). It changes how fast the worker drains
    its backlog and has no effect on demo-api or its database connection pool."""

    action_type: Literal["scale_demo_worker"]
    arguments: ScaleArguments


Action = Annotated[
    RestartWorker | RestartAPI | SetFlag | ScaleWorker,
    Field(discriminator="action_type"),
]


class VerificationCheck(Contract):
    signal: Metric
    target: str = Field(pattern=r"^(<=|>=|<|>) [0-9]+(\.[0-9]+)?$")
    window_minutes: int = Field(default=3, ge=1, le=30)
    consecutive_checks: int = Field(default=3, ge=1, le=5)


class RemediationProposal(Contract):
    summary: str = Field(min_length=1, max_length=2000)
    actions: list[Action] = Field(min_length=1, max_length=4)
    verification_checks: list[VerificationCheck] = Field(min_length=1, max_length=8)


class RemediationPlan(RemediationProposal):
    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    diagnosis_hypothesis_id: UUID
    risk: Risk
    approval_required: bool
    created_at: datetime = Field(default_factory=utcnow)


class ApprovalRequest(Contract):
    decision: Literal["APPROVED", "REJECTED"]
    actor: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=2000)


class Approval(ApprovalRequest):
    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    remediation_plan_id: UUID
    created_at: datetime = Field(default_factory=utcnow)


class ServiceQuery(Contract):
    service: Service


class MetricsQuery(ServiceQuery):
    metric: Metric
    window_minutes: int = Field(default=10, ge=1, le=30)
    aggregation: Literal["p95", "average", "latest", "rate"] = "latest"


class LogsQuery(ServiceQuery):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "ERROR"
    contains: str = Field(
        default="",
        max_length=200,
        description="Literal substring of the message or structured error_type; empty matches all.",
    )
    window_minutes: int = Field(default=10, ge=1, le=120)
    limit: int = Field(default=100, ge=1, le=100)


class DeploymentsQuery(ServiceQuery):
    window_minutes: int = Field(default=120, ge=1, le=1440)


class DatabaseQuery(Contract):
    query_name: Literal["pending_jobs_count", "database_health", "pool_activity"]
    parameters: dict[str, str] = Field(default_factory=dict, max_length=0)
