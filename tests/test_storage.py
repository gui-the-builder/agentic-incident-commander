from pathlib import Path
from uuid import uuid4

import pytest

from commander.domain import (
    Alert,
    ApprovalRequest,
    Hypothesis,
    Incident,
    RemediationPlan,
    State,
    Status,
)
from commander.policy import Policy
from commander.storage import Base, Conflict, HypothesisRow, Repository, make_engine
from tests.test_policy import proposal


def persisted_plan(repository: Repository, incident: Incident) -> RemediationPlan:
    hypothesis = Hypothesis(incident_id=incident.id, statement="Failure", confidence=0.8)
    repository.add_record(HypothesisRow, incident.id, hypothesis.model_dump(mode="json"))
    proposed = proposal()
    risk, required = Policy().classify(proposed)
    remediation = RemediationPlan(
        **proposed.model_dump(),
        incident_id=incident.id,
        diagnosis_hypothesis_id=hypothesis.id,
        risk=risk,
        approval_required=required,
    )
    repository.save_plan(remediation)
    return remediation


def test_remediation_history_is_incident_scoped_and_preserves_uncertain_status(
    repository: Repository,
    incident: Incident,
) -> None:
    owned = persisted_plan(repository, incident)
    other = repository.create_incident(Alert(title="Other incident", service="demo-api"))
    persisted_plan(repository, other)
    repository.claim_action(owned.id, 0)
    history = repository.remediation_history(incident.id)
    assert [attempt["plan_id"] for attempt in history] == [str(owned.id)]
    assert history[0]["actions"][0]["status"] == "STARTED"
    assert "result" not in history[0]["actions"][0]
    assert history[0]["verification"] == []


def test_checkpoint_and_timeline_survive_new_engine(tmp_path: Path) -> None:
    url = "sqlite:///" + str(tmp_path / "restart.db")
    first = make_engine(url)
    Base.metadata.create_all(first)
    repository = Repository(first)
    incident = repository.create_incident(Alert(title="Latency", service="demo-api"), "hidden")
    repository.advance(
        incident.id,
        0,
        State.CONTEXT_GATHERING,
        {"next_node": "gather", "iterations": 1},
        "Investigate latency",
    )
    first.dispose()
    second = make_engine(url)
    restarted = Repository(second)
    restored, revision, checkpoint = restarted.load(incident.id)
    assert restored.current_state == State.CONTEXT_GATHERING
    assert revision == 1 and checkpoint["iterations"] == 1
    assert len(restarted.timeline(incident.id)) == 2
    assert "scenario_id" not in restored.agent_alert().model_dump()
    second.dispose()


def test_stale_worker_cannot_overwrite_checkpoint(
    repository: Repository,
    incident: Incident,
) -> None:
    repository.advance(incident.id, 0, State.CONTEXT_GATHERING, {}, "first worker")
    with pytest.raises(Conflict):
        repository.advance(incident.id, 0, State.FAILED, {}, "stale worker", Status.FAILED)
    restored, revision, _ = repository.load(incident.id)
    assert revision == 1 and restored.status == Status.OPEN
    assert len(repository.timeline(incident.id)) == 2


def test_approval_requires_waiting_state_and_is_immutable(
    repository: Repository,
    incident: Incident,
) -> None:
    remediation = persisted_plan(repository, incident)
    request = ApprovalRequest(decision="APPROVED", actor="operator")
    with pytest.raises(Conflict, match="not waiting"):
        repository.approve(incident.id, request)
    repository.advance(
        incident.id,
        0,
        State.AWAITING_APPROVAL,
        {"plan_id": str(remediation.id)},
        "approval required",
        Status.WAITING_FOR_APPROVAL,
    )
    approval = repository.approve(incident.id, request)
    assert repository.approval(remediation.id) == approval
    with pytest.raises(Conflict, match="already"):
        repository.approve(incident.id, request.model_copy(update={"decision": "REJECTED"}))


def test_plan_cannot_reference_another_incidents_hypothesis(
    repository: Repository,
    incident: Incident,
) -> None:
    remediation = persisted_plan(repository, incident)
    another = repository.create_incident(Alert(title="Worker", service="demo-worker"))
    with pytest.raises(Conflict, match="same incident"):
        repository.save_plan(
            remediation.model_copy(update={"id": uuid4(), "incident_id": another.id})
        )
