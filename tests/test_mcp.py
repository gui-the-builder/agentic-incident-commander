from typing import Any
from uuid import uuid4

import pytest
from mcp import Client

from commander.actions import ActionGateway
from commander.config import Settings
from commander.domain import ApprovalRequest, Contract, Incident, State
from commander.mcp_transport import (
    MCPActions,
    MCPReads,
    action_server,
    read_server,
    server_parameters,
)
from commander.policy import Policy, PolicyViolation
from commander.storage import Repository, ToolCallRow
from commander.tools import READ_TOOLS, ReadGateway
from tests.test_actions import approved_executing_plan
from tests.test_storage import persisted_plan
from tests.test_workflow import FixtureLab

READ_CASES = [
    ("get_service_health", {"service": "demo-api"}, {"status": "healthy", "instances": []}),
    ("get_service_config", {"service": "demo-api"}, {"slow_db": False}),
    ("get_recent_deployments", {"service": "demo-api"}, {"deployments": []}),
    ("query_logs", {"service": "demo-api"}, {"entries": []}),
    (
        "query_metrics",
        {"service": "demo-api", "metric": "http_5xx_rate"},
        {"metric": "http_5xx_rate", "value": 0.4, "unit": "ratio", "window_minutes": 10},
    ),
    (
        "query_database_readonly",
        {"query_name": "pending_jobs_count"},
        {"query_name": "pending_jobs_count", "values": {"count": 12}},
    ),
]


@pytest.mark.parametrize("name,arguments,output", READ_CASES)
async def test_read_contract_parity(
    repository: Repository,
    incident: Incident,
    name: str,
    arguments: dict[str, Any],
    output: dict[str, Any],
) -> None:
    async def handler(query: Contract) -> dict[str, Any]:
        return output

    direct = await ReadGateway(repository, {name: handler}).call(incident.id, name, arguments)
    # Legacy mode exercises framed messages rather than SDK direct dispatch.
    async with Client(read_server({name: handler}), mode="legacy") as client:
        catalog = await client.list_tools()
        assert {t.name for t in catalog.tools} == set(READ_TOOLS)
        tool = next(t for t in catalog.tools if t.name == name)
        assert tool.input_schema == READ_TOOLS[name].input_schema.model_json_schema()
        assert tool.output_schema == READ_TOOLS[name].output_schema.model_json_schema()
        remote = await ReadGateway(repository, MCPReads(client).handlers()).call(
            incident.id, name, arguments
        )
    assert remote.ok == direct.ok and remote.data == direct.data
    assert len(repository.evidence(incident.id)) == 2


async def test_read_boundary_rejects_injection() -> None:
    async with Client(read_server({}), mode="legacy") as client:
        for name, arguments in [
            ("execute_plan", {}),
            ("activate_scenario", {}),
            ("query_database_readonly", {"query_name": "DELETE FROM jobs"}),
        ]:
            assert (await client.call_tool(name, arguments)).is_error


async def test_mcp_transient_failure_keeps_audit_and_retry(
    repository: Repository,
    incident: Incident,
) -> None:
    attempts = 0

    async def handler(query: Contract) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return {"status": "healthy", "instances": []}

    async with Client(read_server({"get_service_health": handler}), mode="legacy") as client:
        result = await ReadGateway(repository, MCPReads(client).handlers()).call(
            incident.id, "get_service_health", {"service": "demo-api"}
        )
    assert result.ok and attempts == 2
    assert [r["status"] for r in repository.records(ToolCallRow, incident.id)] == [
        "TIMED_OUT",
        "SUCCEEDED",
    ]


@pytest.mark.parametrize("reject", [False, True])
async def test_action_boundary_requires_persisted_approval(
    repository: Repository,
    incident: Incident,
    reject: bool,
) -> None:
    plan = persisted_plan(repository, incident)
    checkpoint = {"plan_id": str(plan.id)}
    repository.advance(incident.id, 0, State.AWAITING_APPROVAL, checkpoint, "review")
    if reject:
        repository.approve(incident.id, ApprovalRequest(decision="REJECTED", actor="operator"))
    repository.advance(incident.id, 1, State.EXECUTING, checkpoint, "test gate")
    lab = FixtureLab()
    async with Client(action_server(ActionGateway(repository, Policy(), lab)), mode="legacy") as c:
        with pytest.raises(PolicyViolation):
            await MCPActions(c).execute(incident.id, plan.id)
        assert (
            await c.call_tool(
                "execute_plan",
                {
                    "incident_id": str(incident.id),
                    "plan_id": str(plan.id),
                    "approved": True,
                },
            )
        ).is_error
    assert not lab.executed


async def test_action_boundary_enforces_current_plan_and_no_replay(
    repository: Repository,
    incident: Incident,
) -> None:
    plan_id = approved_executing_plan(repository, incident)
    lab = FixtureLab()
    async with Client(action_server(ActionGateway(repository, Policy(), lab)), mode="legacy") as c:
        assert [t.name for t in (await c.list_tools()).tools] == ["execute_plan"]
        with pytest.raises(PolicyViolation):
            await MCPActions(c).execute(incident.id, uuid4())
        assert (await MCPActions(c).execute(incident.id, plan_id))[0].ok
        assert (await MCPActions(c).execute(incident.id, plan_id))[0].ok
    assert len(lab.executed) == 1


async def test_stdio_servers_start_with_separate_credentials() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        investigation_database_url="sqlite://",
        control_token="action-only",
        operator_token="never-inherit",
    )
    for boundary in ("reads", "actions"):
        params = server_parameters(settings, boundary)
        assert params.env is not None and "OPERATOR_TOKEN" not in params.env
        assert ("CONTROL_TOKEN" in params.env) == (boundary == "actions")
        assert ("DATABASE_URL" in params.env) == (boundary == "actions")
        async with Client(params, read_timeout_seconds=15) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == (set(READ_TOOLS) if boundary == "reads" else {"execute_plan"})
            if boundary == "reads":
                result = await client.call_tool(
                    "query_database_readonly", {"query_name": "database_health"}
                )
                assert not result.is_error
                assert result.structured_content == {
                    "query_name": "database_health",
                    "values": {"reachable": True},
                }


async def test_mcp_uncertain_action_cannot_be_replayed(
    repository: Repository,
    incident: Incident,
) -> None:
    plan_id = approved_executing_plan(repository, incident)
    repository.claim_action(plan_id, 0)
    lab = FixtureLab()
    async with Client(action_server(ActionGateway(repository, Policy(), lab)), mode="legacy") as c:
        with pytest.raises(PolicyViolation, match="uncertain"):
            await MCPActions(c).execute(incident.id, plan_id)
    assert not lab.executed


async def test_mcp_validates_arguments_and_outputs_at_server_boundary() -> None:
    calls = 0

    async def malformed(query: Contract) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "healthy", "scenario_root_cause": "hidden"}

    async with Client(read_server({"get_service_health": malformed}), mode="legacy") as c:
        assert (
            await c.call_tool(
                "get_service_health",
                {
                    "service": "demo-api",
                    "command": "whoami",
                },
            )
        ).is_error
        assert calls == 0
        result = await c.call_tool("get_service_health", {"service": "demo-api"})
        assert result.is_error and result.structured_content is None
    assert calls == 1
