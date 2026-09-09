import asyncio
from dataclasses import replace
from typing import Any

import pytest

from commander.domain import Contract, Incident
from commander.storage import Repository, ToolCallRow
from commander.tools import READ_TOOLS, ReadGateway


async def healthy(_: Contract) -> dict[str, Any]:
    return {"status": "healthy", "instances": [{"id": "api-1", "healthy": True, "ready": True}]}


async def test_success_is_audited_and_becomes_evidence(
    repository: Repository,
    incident: Incident,
) -> None:
    gateway = ReadGateway(repository, {"get_service_health": healthy})
    result = await gateway.call(incident.id, "get_service_health", {"service": "demo-api"})
    assert result.ok
    calls = repository.records(ToolCallRow, incident.id)
    assert calls[0]["status"] == "SUCCEEDED" and calls[0]["duration_ms"] >= 0
    assert str(repository.evidence(incident.id)[0].tool_call_id) == calls[0]["id"]


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("exec", {"command": "rm -rf /"}),
        ("get_service_health", {"service": "http://attacker"}),
        ("get_service_health", {"service": "demo-api", "scenario_id": "hidden"}),
        ("restart_demo_api", {"reason": "skip approval"}),
    ],
)
async def test_invalid_tool_requests_are_blocked_and_audited(
    repository: Repository,
    incident: Incident,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    gateway = ReadGateway(repository, {"get_service_health": healthy})
    assert not (await gateway.call(incident.id, tool, arguments)).ok
    assert repository.records(ToolCallRow, incident.id)[0]["status"] == "BLOCKED"
    assert not repository.evidence(incident.id)


async def test_transient_failure_records_each_attempt(
    repository: Repository,
    incident: Incident,
) -> None:
    attempts = 0

    async def flaky(query: Contract) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return await healthy(query)

    gateway = ReadGateway(repository, {"get_service_health": flaky})
    assert (await gateway.call(incident.id, "get_service_health", {"service": "demo-api"})).ok
    # Wall-clock timestamps can tie on Windows. The persisted attempt number,
    # not UUID ordering within a timestamp, identifies the retry sequence.
    calls = sorted(repository.records(ToolCallRow, incident.id), key=lambda call: call["attempt"])
    assert [call["attempt"] for call in calls] == [1, 2]
    assert [r["status"] for r in calls] == [
        "TIMED_OUT",
        "SUCCEEDED",
    ]
    assert len(repository.evidence(incident.id)) == 1


async def test_timeout_is_bounded_and_persisted(repository: Repository, incident: Incident) -> None:
    async def stuck(_: Contract) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    definition = replace(READ_TOOLS["get_service_health"], timeout_seconds=0.01)
    gateway = ReadGateway(repository, {"get_service_health": stuck}, {definition.name: definition})
    assert not (await gateway.call(incident.id, definition.name, {"service": "demo-api"})).ok
    calls = repository.records(ToolCallRow, incident.id)
    assert len(calls) == 2 and all(c["status"] == "TIMED_OUT" for c in calls)


async def test_malformed_output_fails_without_retry(
    repository: Repository,
    incident: Incident,
) -> None:
    async def malformed(_: Contract) -> dict[str, Any]:
        return {"status": "healthy", "scenario_root_cause": "hidden"}

    gateway = ReadGateway(repository, {"get_service_health": malformed})
    assert not (await gateway.call(incident.id, "get_service_health", {"service": "demo-api"})).ok
    assert len(repository.records(ToolCallRow, incident.id)) == 1
    assert not repository.evidence(incident.id)
