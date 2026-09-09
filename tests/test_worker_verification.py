from typing import Any
from uuid import uuid4

import pytest

from commander.domain import Alert, Contract, State, Status, VerificationCheck
from commander.storage import Repository, VerificationRow
from tests.test_storage import persisted_plan
from tests.test_workflow import FixtureLab, workflow


@pytest.mark.parametrize(
    "readings,expected",
    [
        ([20.0, 21.0, 22.0, 23.0], True),
        ([20.0, 20.0, 20.0, 20.0], False),
        ([None, None, None, None], False),
        ([20.0, 19.0, 19.0, 19.0], False),
    ],
)
async def test_missing_pre_action_counter_requires_observed_progress(
    repository: Repository,
    readings: list[float | None],
    expected: bool,
) -> None:
    incident = repository.create_incident(Alert(title="Worker unavailable", service="demo-worker"))
    original = persisted_plan(repository, incident)
    plan = original.model_copy(
        update={
            "id": uuid4(),
            "verification_checks": [
                VerificationCheck(signal="worker_jobs_processed_total", target=">= 0")
            ],
        }
    )
    repository.save_plan(plan)
    checkpoint = {"plan_id": str(plan.id), "before": {"worker_jobs_processed_total": None}}
    repository.advance(incident.id, 0, State.VERIFYING, checkpoint, "Test recovery observations")
    sequence = iter(readings)

    class CounterLab(FixtureLab):
        async def metrics(self, query: Contract) -> dict[str, Any]:
            value = next(sequence)
            if value is None:
                raise ValueError("Worker scrape unavailable")
            return {
                "metric": "worker_jobs_processed_total",
                "value": value,
                "unit": "count",
                "window_minutes": 3,
            }

    result = await workflow(repository, CounterLab()).verify(incident.id, checkpoint)
    record = repository.records(VerificationRow, incident.id)[0]
    assert record["success"] is expected
    assert result == ("postmortem" if expected else "investigate")
    assert (repository.load(incident.id)[0].status == Status.RESOLVED) is expected
    assert all(check["before"] is None for check in record["checks"])
    assert all(check["progress_baseline"] == readings[0] for check in record["checks"])
    assert record["checks"][0]["passed"] is False
    if expected:
        assert sum(check["passed"] for check in record["checks"]) == 3
