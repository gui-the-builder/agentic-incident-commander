import time
from typing import Any
from uuid import UUID

import pytest

from commander.actions import ActionGateway
from commander.domain import (
    Action,
    ApprovalRequest,
    Contract,
    Incident,
    State,
    Status,
    ToolResult,
    utcnow,
)
from commander.models import T
from commander.policy import Policy, PolicyViolation
from commander.storage import PostmortemRow, Repository, VerificationRow
from commander.tools import ReadGateway
from commander.workflow import Workflow, report_observations, verification_summary


class FixtureLab:
    def __init__(self, recovery: bool = True):
        self.healthy = False
        self.recovery = recovery
        self.executed: list[Action] = []

    async def health(self, query: Contract) -> dict[str, Any]:
        return {"status": "healthy", "instances": []}

    async def config(self, query: Contract) -> dict[str, Any]:
        return {"new_checkout_path": not self.healthy}

    async def changes(self, query: Contract) -> dict[str, Any]:
        return {
            "deployments": [
                {
                    "timestamp": utcnow().isoformat(),
                    "service": "demo-api",
                    "version": "fixture-v1",
                    "changes": {"new_checkout_path": True},
                }
            ]
        }

    async def logs(self, query: Contract) -> dict[str, Any]:
        return {
            "entries": [
                {
                    "timestamp": utcnow().isoformat(),
                    "level": "ERROR",
                    "message": "CheckoutPathError",
                    "attributes": {"error_type": "CheckoutPathError"},
                }
            ]
        }

    async def metrics(self, query: Contract) -> dict[str, Any]:
        metric = query.model_dump()["metric"]
        values = {
            "http_5xx_rate": 0 if self.healthy else 0.5,
            "http_request_duration_seconds": 0.1,
            "db_pool_wait_seconds": 0.001,
            "db_pool_in_use": 1,
            "worker_jobs_pending": 0,
            "worker_last_success_timestamp": time.time(),
        }
        return {"metric": metric, "value": values[metric], "unit": "value", "window_minutes": 3}

    async def execute(self, action: Action, idempotency_key: UUID) -> ToolResult:
        self.executed.append(action)
        self.healthy = self.recovery
        return ToolResult(ok=True, data={"changed": True})

    def gateway(self, repository: Repository) -> ReadGateway:
        return ReadGateway(
            repository,
            {
                "get_service_health": self.health,
                "get_service_config": self.config,
                "get_recent_deployments": self.changes,
                "query_logs": self.logs,
                "query_metrics": self.metrics,
            },
        )


class ContextualFixtureModel:
    """Cites generated UUIDs from test context; not a production reasoning implementation."""

    def __init__(self, confidence: float = 0.9):
        self.confidence = confidence
        self.contexts: list[dict[str, Any]] = []

    async def complete_structured(
        self,
        task: str,
        context: dict[str, Any],
        response_model: type[T],
    ) -> T:
        self.contexts.append(context)
        response: dict[str, Any]
        if task in {"hypotheses", "update_hypotheses"}:
            response = {
                "hypotheses": [
                    {
                        "statement": "Bad checkout feature flag",
                        "mechanism": "checkout_flag_regression",
                        "confidence": self.confidence,
                        "supporting_evidence_ids": [
                            e["id"]
                            for e in context["evidence"]
                            if e["source"] in {"get_recent_deployments", "query_logs"}
                            or e["payload"].get("metric") == "http_5xx_rate"
                        ],
                    }
                ]
            }
        elif task == "investigate":
            response = {
                "tool": "query_logs",
                "arguments": {"service": "demo-api"},
                "rationale": "Check the checkout exception",
            }
        elif task == "diagnosis":
            response = {
                "hypothesis_id": context["hypotheses"][0]["id"],
                "rationale": "Error follows enabled feature flag",
                "acknowledged_failed_tool_call_ids": [
                    c["id"] for c in context["failed_tool_calls"]
                ],
            }
        elif task == "remediation":
            response = {
                "summary": "Disable new checkout path",
                "actions": [
                    {
                        "action_type": "set_demo_feature_flag",
                        "arguments": {
                            "flag": "new_checkout_path",
                            "value": False,
                            "reason": "CheckoutPathError",
                        },
                    }
                ],
                "verification_checks": [{"signal": "http_5xx_rate", "target": "<= 0.99"}],
            }
        else:
            response = {"markdown": "Review the regression and the recorded observations."}
        return response_model.model_validate(response)


def workflow(
    repository: Repository,
    lab: FixtureLab,
    model: ContextualFixtureModel | None = None,
    max_iterations: int = 3,
) -> Workflow:
    policy = Policy()
    return Workflow(
        repository,
        model or ContextualFixtureModel(),
        lab.gateway(repository),
        ActionGateway(repository, policy, lab),
        policy,
        max_iterations,
        verification_interval_seconds=0,
    )


class MissingCitationModel(ContextualFixtureModel):
    def __init__(self, repair: bool):
        super().__init__()
        self.repair = repair

    async def complete_structured(
        self,
        task: str,
        context: dict[str, Any],
        response_model: type[T],
    ) -> T:
        response = await super().complete_structured(task, context, response_model)
        if task in {"hypotheses", "update_hypotheses"} and (
            not self.repair or not context["diagnosis_feedback"]
        ):
            data = response.model_dump()
            data["hypotheses"][0]["supporting_evidence_ids"] = [
                next(e["id"] for e in context["evidence"] if e["source"] == "get_service_config")
            ]
            return response_model.model_validate(data)
        return response


async def test_missing_diagnostic_citations_escalate_without_planning(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    model = MissingCitationModel(repair=False)
    await workflow(repository, lab, model, max_iterations=2).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    assert final.status == Status.ESCALATED and checkpoint["iterations"] == 2
    assert checkpoint["diagnosis_feedback"] and "plan_id" not in checkpoint
    assert not lab.executed
    report = repository.records(PostmortemRow, incident.id)[0]["markdown"]
    assert "Unconfirmed; investigation incomplete" in report
    assert "## Model synthesis" not in report
    assert any(
        "Diagnosis evidence incomplete" in event["summary"]
        for event in repository.timeline(incident.id)
    )


async def test_investigation_repairs_citations_before_approval(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    model = MissingCitationModel(repair=True)
    await workflow(repository, lab, model).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    assert final.status == Status.WAITING_FOR_APPROVAL
    assert checkpoint["iterations"] == 2 and checkpoint["diagnosis_feedback"] == []
    assert any(context["diagnosis_feedback"] for context in model.contexts)
    assert not lab.executed


async def test_approval_pause_and_new_workflow_resume(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    model = ContextualFixtureModel()
    await workflow(repository, lab, model).run(incident.id)
    paused, _, checkpoint = repository.load(incident.id)
    assert paused.status == Status.WAITING_FOR_APPROVAL
    assert checkpoint["next_node"] == "await_approval"
    assert not lab.executed
    assert all("scenario_id" not in context["alert"] for context in model.contexts)
    plan = repository.plan(UUID(checkpoint["plan_id"]))
    assert (
        next(c.target for c in plan.verification_checks if c.signal == "http_5xx_rate") == "<= 0.02"
    )
    repository.approve(incident.id, ApprovalRequest(decision="APPROVED", actor="operator"))
    await workflow(repository, lab).run(incident.id)
    completed, _, _ = repository.load(incident.id)
    assert completed.status == Status.RESOLVED
    assert len(lab.executed) == 1
    verification = repository.records(VerificationRow, incident.id)
    assert verification[0]["success"] and len(verification[0]["checks"]) == 9
    report = repository.records(PostmortemRow, incident.id)[0]["markdown"]
    assert "## Evidence" in report and "## Verification" in report
    await workflow(repository, lab).run(incident.id)
    assert len(lab.executed) == 1


async def test_rejection_generates_report_without_execution(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    run = workflow(repository, lab)
    await run.run(incident.id)
    repository.approve(incident.id, ApprovalRequest(decision="REJECTED", actor="operator"))
    await run.run(incident.id)
    assert repository.load(incident.id)[0].status == Status.ESCALATED
    assert not lab.executed
    assert "rejected" in repository.records(PostmortemRow, incident.id)[0]["markdown"]


async def test_failed_recovery_never_resolves_and_iteration_budget_is_global(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab(recovery=False)
    run = workflow(repository, lab, max_iterations=1)
    await run.run(incident.id)
    repository.approve(incident.id, ApprovalRequest(decision="APPROVED", actor="operator"))
    await run.run(incident.id)
    assert repository.load(incident.id)[0].status == Status.ESCALATED
    assert not repository.records(VerificationRow, incident.id)[0]["success"]
    assert len(lab.executed) == 1


async def test_failed_recovery_context_survives_restart_and_distinguishes_pending_plan(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab(recovery=False)
    initial = workflow(repository, lab)
    await initial.run(incident.id)
    first_plan = repository.load(incident.id)[2]["plan_id"]
    pending = initial.context(incident.id)["remediation_history"][0]
    assert pending["actions"][0]["status"] == "PENDING"
    assert pending["verification"] == []
    repository.approve(incident.id, ApprovalRequest(decision="APPROVED", actor="operator"))
    model = ContextualFixtureModel()
    await workflow(repository, lab, model).run(incident.id)
    attempts = workflow(repository, lab).context(incident.id)["remediation_history"]
    assert len(attempts) == 2
    failed, pending = attempts
    assert failed["plan_id"] == first_plan
    assert failed["actions"][0]["action_type"] == "set_demo_feature_flag"
    assert failed["actions"][0]["status"] == "SUCCEEDED"
    assert failed["actions"][0]["result"]["ok"] is True
    assert failed["verification"][0]["success"] is False
    error_check = next(
        c for c in failed["verification"][0]["checks"] if c["signal"] == "http_5xx_rate"
    )
    assert error_check["after"] == 0.5 and error_check["target"] == 0.02
    assert any(c["signal"] == "http_5xx_rate" for c in failed["expected_checks"])
    # The next investigation actually received the previous action and failed checks.
    investigate_context = next(c for c in model.contexts if "tools" in c)
    assert investigate_context["remediation_history"] == [failed]
    assert pending["plan_id"] != first_plan
    assert pending["actions"][0]["status"] == "PENDING"
    assert pending["verification"] == []
    assert len(lab.executed) == 1
    assert repository.load(incident.id)[0].status == Status.WAITING_FOR_APPROVAL
    repository.approve(incident.id, ApprovalRequest(decision="REJECTED", actor="operator"))
    await workflow(repository, lab).run(incident.id)
    report = repository.records(PostmortemRow, incident.id)[0]["markdown"]
    assert first_plan in report and pending["plan_id"] in report
    assert "set_demo_feature_flag: SUCCEEDED" in report
    assert "set_demo_feature_flag: PENDING" in report
    assert "Approval decision: REJECTED" in report


async def test_low_confidence_investigation_stops_at_iteration_limit(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    await workflow(repository, lab, ContextualFixtureModel(confidence=0.3), 2).run(incident.id)
    completed, _, checkpoint = repository.load(incident.id)
    assert completed.status == Status.ESCALATED and checkpoint["iterations"] == 2
    assert not lab.executed


async def test_action_gateway_cannot_bypass_pause(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    run = workflow(repository, lab)
    await run.run(incident.id)
    _, _, checkpoint = repository.load(incident.id)
    with pytest.raises(PolicyViolation, match="current executing"):
        await run.actions.execute(incident.id, UUID(checkpoint["plan_id"]))
    assert not lab.executed


def test_workflow_lock_excludes_concurrent_workers(
    repository: Repository,
    incident: Incident,
) -> None:
    from commander.storage import Conflict

    with repository.workflow_lock(incident.id), pytest.raises(Conflict, match="active worker"):
        with repository.workflow_lock(incident.id):
            pytest.fail("Second worker acquired the lock")
    with repository.workflow_lock(incident.id):
        pass


def test_report_keeps_cited_middle_observations_and_signal_endpoints() -> None:
    observations = [
        {"id": str(index), "source": "query_metrics", "payload": {"metric": "http_5xx_rate"}}
        for index in range(10)
    ]
    assert [item["id"] for item in report_observations(observations, {"4"})] == ["0", "4", "9"]


def test_verification_summary_preserves_failed_samples() -> None:
    checks = [
        {
            "name": "http_5xx_rate",
            "before": 0.8,
            "after": value,
            "operator": "<=",
            "target": 0.02,
            "passed": value <= 0.02,
        }
        for value in (0.4, 0.01, 0.0, 0.0)
    ]
    summary = verification_summary([{"success": True, "checks": checks}])[0]
    assert summary["success"]
    assert summary["checks"][0] == {
        "signal": "http_5xx_rate",
        "before": 0.8,
        "after": 0.0,
        "operator": "<=",
        "target": 0.02,
        "samples": 4,
        "passing_samples": 3,
        "last_check_passed": True,
    }


class UncitedHypothesisModel(ContextualFixtureModel):
    """Emits a hypothesis with no supporting evidence, which validate_diagnosis rejects."""

    def __init__(self, repair_after: int):
        super().__init__()
        self.repair_after = repair_after
        self.rejections = 0

    async def complete_structured(
        self,
        task: str,
        context: dict[str, Any],
        response_model: type[T],
    ) -> T:
        response = await super().complete_structured(task, context, response_model)
        if task in {"hypotheses", "update_hypotheses"}:
            feedback = " ".join(context["diagnosis_feedback"])
            if "was rejected" in feedback:
                self.rejections += 1
            if self.rejections < self.repair_after:
                data = response.model_dump()
                data["hypotheses"][0]["supporting_evidence_ids"] = []
                return response_model.model_validate(data)
        return response


async def test_rejected_diagnosis_returns_to_investigation_instead_of_failing(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    model = UncitedHypothesisModel(repair_after=1)
    await workflow(repository, lab, model, max_iterations=3).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    # The rejection is recoverable: the incident continues and reaches its approval gate.
    assert final.status == Status.WAITING_FOR_APPROVAL
    assert final.current_state != State.FAILED
    assert model.rejections >= 1
    timeline = repository.timeline(incident.id)
    assert any("Diagnosis rejected" in event["summary"] for event in timeline)
    assert not any(event["summary"].startswith("PolicyViolation") for event in timeline)
    assert not lab.executed


async def test_persistently_rejected_diagnosis_escalates_without_planning(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FixtureLab()
    model = UncitedHypothesisModel(repair_after=99)
    await workflow(repository, lab, model, max_iterations=2).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    # A model that never repairs its citations exhausts the budget and escalates safely.
    assert final.status == Status.ESCALATED and checkpoint["iterations"] == 2
    assert "plan_id" not in checkpoint and not lab.executed
    assert any(
        "Diagnosis rejected" in event["summary"] for event in repository.timeline(incident.id)
    )
