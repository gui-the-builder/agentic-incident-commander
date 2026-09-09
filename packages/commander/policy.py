"""Model-independent validation; no caller-supplied risk or approval flags."""

from uuid import UUID

from commander.domain import (
    Approval,
    Diagnosis,
    Evidence,
    Hypothesis,
    RemediationPlan,
    RemediationProposal,
    Risk,
)

ACTION_RISKS = {
    "restart_demo_worker": Risk.LOW,
    "restart_demo_api": Risk.MEDIUM,
    "set_demo_feature_flag": Risk.MEDIUM,
    "scale_demo_worker": Risk.MEDIUM,
}


class PolicyViolation(ValueError):
    pass


def validate_diagnosis(
    diagnosis: Diagnosis,
    hypotheses: list[Hypothesis],
    evidence: list[Evidence],
    failed_tool_call_ids: set[UUID],
) -> Hypothesis:
    selected = next((h for h in hypotheses if h.id == diagnosis.hypothesis_id), None)
    if selected is None:
        raise PolicyViolation("Diagnosis must select a persisted hypothesis")
    available = {e.id for e in evidence if e.incident_id == selected.incident_id}
    references = set(selected.supporting_evidence_ids + selected.contradicting_evidence_ids)
    if not selected.supporting_evidence_ids or not references <= available:
        raise PolicyViolation("Diagnosis requires valid incident-scoped evidence references")
    if not failed_tool_call_ids <= set(diagnosis.acknowledged_failed_tool_call_ids):
        raise PolicyViolation("Diagnosis must acknowledge failed investigation calls")
    return selected


class Policy:
    def __init__(
        self,
        auto_approve_low_risk: bool = True,
        allow_medium: bool = True,
        action_risks: dict[str, Risk] | None = None,
    ):
        self.auto_approve_low_risk = auto_approve_low_risk
        self.allow_medium = allow_medium
        self.action_risks = dict(ACTION_RISKS if action_risks is None else action_risks)

    def classify(self, proposal: RemediationProposal) -> tuple[Risk, bool]:
        # Revalidate even if a caller bypassed Pydantic via model_construct/model_copy.
        checked = RemediationProposal.model_validate(proposal.model_dump())
        if any(action.action_type not in self.action_risks for action in checked.actions):
            raise PolicyViolation("Action is not supported by the configured backend")
        risks = [self.action_risks[action.action_type] for action in checked.actions]
        if Risk.HIGH in risks:
            raise PolicyViolation("High-risk remediation is disabled")
        if any(risk not in {Risk.LOW, Risk.MEDIUM} for risk in risks):
            raise PolicyViolation("Mutating actions must have a supported low or medium risk")
        risk = Risk.MEDIUM if Risk.MEDIUM in risks else Risk.LOW
        if risk == Risk.MEDIUM and not self.allow_medium:
            raise PolicyViolation("Medium-risk actions are disabled")
        return risk, risk == Risk.MEDIUM or not self.auto_approve_low_risk

    def authorize(self, plan: RemediationPlan, approval: Approval | None) -> None:
        proposal = RemediationProposal.model_validate(
            plan.model_dump(include=set(RemediationProposal.model_fields))
        )
        risk, required = self.classify(proposal)
        if plan.risk != risk or plan.approval_required != required:
            raise PolicyViolation("Stored plan does not match the current policy")
        if approval is not None:
            if approval.incident_id != plan.incident_id or approval.remediation_plan_id != plan.id:
                raise PolicyViolation("Approval belongs to a different incident or plan")
            if approval.decision != "APPROVED":
                raise PolicyViolation("Rejected plans cannot execute")
        if required and approval is None:
            raise PolicyViolation("Explicit operator approval is required")
