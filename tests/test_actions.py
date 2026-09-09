from uuid import UUID

import pytest

from commander.actions import ActionGateway
from commander.domain import ApprovalRequest, Incident, State
from commander.policy import Policy, PolicyViolation
from commander.storage import Repository
from tests.test_storage import persisted_plan
from tests.test_workflow import FixtureLab


def approved_executing_plan(repository: Repository, incident: Incident) -> UUID:
    plan = persisted_plan(repository, incident)
    checkpoint = {"plan_id": str(plan.id)}
    repository.advance(incident.id, 0, State.AWAITING_APPROVAL, checkpoint, "Review plan")
    repository.approve(incident.id, ApprovalRequest(decision="APPROVED", actor="operator"))
    repository.advance(incident.id, 1, State.EXECUTING, checkpoint, "Execute approved plan")
    return plan.id


async def test_successful_action_is_not_replayed(
    repository: Repository, incident: Incident
) -> None:
    plan_id = approved_executing_plan(repository, incident)
    lab = FixtureLab()
    gateway = ActionGateway(repository, Policy(), lab)
    assert (await gateway.execute(incident.id, plan_id))[0].ok
    assert (await gateway.execute(incident.id, plan_id))[0].ok
    assert len(lab.executed) == 1


async def test_interrupted_action_requires_operator_inspection(
    repository: Repository,
    incident: Incident,
) -> None:
    plan_id = approved_executing_plan(repository, incident)
    repository.claim_action(plan_id, 0)  # Simulate process exit after claim, before response.
    lab = FixtureLab()
    with pytest.raises(PolicyViolation, match="uncertain"):
        await ActionGateway(repository, Policy(), lab).execute(incident.id, plan_id)
    assert not lab.executed
