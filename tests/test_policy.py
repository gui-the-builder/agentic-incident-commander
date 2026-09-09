from uuid import uuid4

import pytest
from pydantic import ValidationError

from commander.domain import (
    Approval,
    DatabaseQuery,
    Diagnosis,
    Evidence,
    Hypothesis,
    Incident,
    RemediationPlan,
    RemediationProposal,
    Risk,
    utcnow,
)
from commander.policy import Policy, PolicyViolation, validate_diagnosis


def proposal(action: str = "restart_demo_api") -> RemediationProposal:
    return RemediationProposal.model_validate(
        {
            "summary": "Recover service",
            "actions": [
                {"action_type": action, "arguments": {"reason": "Confirmed failure"}},
            ],
            "verification_checks": [{"signal": "http_5xx_rate", "target": "<= 0.02"}],
        }
    )


def plan(policy: Policy, action: str = "restart_demo_api") -> RemediationPlan:
    proposed = proposal(action)
    risk, required = policy.classify(proposed)
    return RemediationPlan(
        **proposed.model_dump(),
        incident_id=uuid4(),
        diagnosis_hypothesis_id=uuid4(),
        risk=risk,
        approval_required=required,
    )


def approve(remediation: RemediationPlan) -> Approval:
    return Approval(
        decision="APPROVED",
        actor="test-operator",
        incident_id=remediation.incident_id,
        remediation_plan_id=remediation.id,
    )


def test_medium_actions_require_explicit_matching_approval() -> None:
    policy = Policy()
    remediation = plan(policy)
    with pytest.raises(PolicyViolation, match="Explicit"):
        policy.authorize(remediation, None)
    policy.authorize(remediation, approve(remediation))


@pytest.mark.parametrize("risk", [Risk.HIGH, Risk.READ_ONLY])
def test_backend_risk_overrides_cannot_silently_downgrade_mutations(risk: Risk) -> None:
    policy = Policy(action_risks={"restart_demo_api": risk})
    with pytest.raises(PolicyViolation):
        policy.classify(proposal())


@pytest.mark.parametrize("field", ["incident_id", "remediation_plan_id"])
def test_approval_cannot_be_reused_for_another_plan(field: str) -> None:
    policy = Policy()
    remediation = plan(policy)
    approval = approve(remediation).model_copy(update={field: uuid4()})
    with pytest.raises(PolicyViolation, match="different"):
        policy.authorize(remediation, approval)


def test_model_cannot_override_stored_risk() -> None:
    policy = Policy()
    remediation = plan(policy).model_copy(update={"approval_required": False})
    with pytest.raises(PolicyViolation, match="current policy"):
        policy.authorize(remediation, None)


def test_rejected_low_risk_plan_never_executes() -> None:
    policy = Policy()
    remediation = plan(policy, "restart_demo_worker")
    rejected = approve(remediation).model_copy(update={"decision": "REJECTED"})
    with pytest.raises(PolicyViolation, match="Rejected"):
        policy.authorize(remediation, rejected)


def test_low_risk_autoapproval_is_configurable() -> None:
    automatic = Policy()
    automatic.authorize(plan(automatic, "restart_demo_worker"), None)
    manual = Policy(auto_approve_low_risk=False)
    with pytest.raises(PolicyViolation, match="Explicit"):
        manual.authorize(plan(manual, "restart_demo_worker"), None)


def test_medium_actions_can_be_disabled() -> None:
    with pytest.raises(PolicyViolation, match="disabled"):
        Policy(allow_medium=False).classify(proposal())


@pytest.mark.parametrize("action", ["exec", "run_shell", "query_database_write", "delete_database"])
def test_unknown_actions_cannot_enter_a_plan(action: str) -> None:
    with pytest.raises(ValidationError):
        proposal(action)


def test_plan_requires_verification_before_execution() -> None:
    with pytest.raises(ValidationError):
        RemediationProposal.model_validate({**proposal().model_dump(), "verification_checks": []})


def test_database_tool_does_not_accept_sql_or_parameters() -> None:
    with pytest.raises(ValidationError):
        DatabaseQuery(query_name="pending_jobs_count", parameters={"sql": "DELETE FROM jobs"})
    with pytest.raises(ValidationError):
        DatabaseQuery.model_validate({"query_name": "DELETE FROM jobs"})


def test_diagnosis_rejects_missing_foreign_and_failed_evidence(incident: Incident) -> None:
    evidence = Evidence(
        incident_id=incident.id,
        tool_call_id=uuid4(),
        evidence_type="logs",
        source="query_logs",
        summary="connection acquisition timed out",
        payload={},
        observed_at=utcnow(),
    )
    hypothesis = Hypothesis(
        incident_id=incident.id,
        statement="Pool exhaustion",
        confidence=0.8,
        supporting_evidence_ids=[evidence.id],
    )
    diagnosis = Diagnosis(hypothesis_id=hypothesis.id, rationale="Connection errors")
    assert validate_diagnosis(diagnosis, [hypothesis], [evidence], set()) == hypothesis
    with pytest.raises(PolicyViolation, match="evidence"):
        validate_diagnosis(diagnosis, [hypothesis], [], set())
    with pytest.raises(PolicyViolation, match="evidence"):
        validate_diagnosis(
            diagnosis,
            [hypothesis],
            [
                evidence.model_copy(update={"incident_id": uuid4()}),
            ],
            set(),
        )
    with pytest.raises(PolicyViolation, match="acknowledge"):
        validate_diagnosis(diagnosis, [hypothesis], [evidence], {uuid4()})
