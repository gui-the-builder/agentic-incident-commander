import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest

from commander.domain import Contract, Incident, Status
from commander.models import T
from commander.storage import Repository, ToolCallRow
from commander.tools import READ_TOOLS, ReadGateway
from tests.test_workflow import ContextualFixtureModel, FixtureLab, workflow


class FailingLab(FixtureLab):
    def __init__(self, fault: str):
        super().__init__()
        self.fault = fault
        self.database_attempts = 0

    async def metrics(self, query: Contract) -> dict[str, Any]:
        if self.fault == "metrics_timeout" and query.model_dump()["metric"] == "http_5xx_rate":
            await asyncio.Event().wait()
        return await super().metrics(query)

    async def logs(self, query: Contract) -> dict[str, Any]:
        if self.fault == "malformed_logs":
            return {"entries": "invalid"}
        if self.fault == "empty_logs":
            return {"entries": []}
        return await super().logs(query)

    async def database(self, query: Contract) -> dict[str, Any]:
        self.database_attempts += 1
        if self.database_attempts == 1:
            raise httpx.ConnectError("transient database connection failure")
        return {"query_name": "database_health", "values": {"reachable": True}}

    def gateway(self, repository: Repository) -> ReadGateway:
        gateway = super().gateway(repository)
        gateway.handlers["query_database_readonly"] = self.database
        # Shorten the metrics timeout only for the fault that exercises it. Applying it
        # to every fault made unrelated metric reads time out on a loaded machine.
        if self.fault == "metrics_timeout":
            gateway.definitions = {
                **READ_TOOLS,
                "query_metrics": replace(READ_TOOLS["query_metrics"], timeout_seconds=0.01),
            }
        return gateway


@pytest.mark.parametrize("fault", ["metrics_timeout", "malformed_logs", "empty_logs"])
async def test_missing_or_broken_observations_escalate_safely(
    repository: Repository,
    incident: Incident,
    fault: str,
) -> None:
    lab = FailingLab(fault)
    await workflow(repository, lab, max_iterations=2).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    assert final.status == Status.ESCALATED and checkpoint["iterations"] == 2
    assert checkpoint["diagnosis_feedback"] and "plan_id" not in checkpoint
    assert not lab.executed
    calls = repository.records(ToolCallRow, incident.id)
    if fault == "metrics_timeout":
        timed_out = [call for call in calls if call["status"] == "TIMED_OUT"]
        assert len(timed_out) == 2
        assert not any(
            e.payload.get("metric") == "http_5xx_rate" for e in repository.evidence(incident.id)
        )
    elif fault == "malformed_logs":
        assert len([call for call in calls if call["status"] == "FAILED"]) == 2
        assert not any(e.source == "query_logs" for e in repository.evidence(incident.id))
    else:
        assert all(call["status"] == "SUCCEEDED" for call in calls)


class DatabaseThenLogsModel(ContextualFixtureModel):
    def __init__(self) -> None:
        super().__init__()
        self.investigations = 0

    async def complete_structured(
        self,
        task: str,
        context: dict[str, Any],
        response_model: type[T],
    ) -> T:
        if task == "investigate":
            self.investigations += 1
            if self.investigations == 1:
                return response_model.model_validate(
                    {
                        "tool": "query_database_readonly",
                        "arguments": {"query_name": "database_health"},
                        "rationale": "Check database reachability",
                    }
                )
        return await super().complete_structured(task, context, response_model)


async def test_transient_database_failure_retries_then_alternate_tool_completes_evidence(
    repository: Repository,
    incident: Incident,
) -> None:
    lab = FailingLab("transient_database")
    model = DatabaseThenLogsModel()
    await workflow(repository, lab, model).run(incident.id)
    final, _, checkpoint = repository.load(incident.id)
    assert final.status == Status.WAITING_FOR_APPROVAL and checkpoint["iterations"] == 2
    assert lab.database_attempts == 2 and not lab.executed
    calls = [
        call
        for call in repository.records(ToolCallRow, incident.id)
        if call["tool_name"] == "query_database_readonly"
    ]
    assert [call["status"] for call in calls] == ["FAILED", "SUCCEEDED"]
    assert any(context["failed_tool_calls"] for context in model.contexts)
