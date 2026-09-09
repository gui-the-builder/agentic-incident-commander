"""Action execution accepts only a persisted current plan, never model arguments."""

import asyncio
import time
from typing import Protocol
from uuid import UUID

from commander.domain import Action, State, ToolResult, utcnow
from commander.policy import Policy, PolicyViolation
from commander.storage import Repository, ToolCallRow


class ActionBackend(Protocol):
    async def execute(self, action: Action, idempotency_key: UUID) -> ToolResult: ...


class ActionExecutor(Protocol):
    async def execute(self, incident_id: UUID, plan_id: UUID) -> list[ToolResult]: ...


class ActionGateway:
    def __init__(
        self,
        repository: Repository,
        policy: Policy,
        backend: ActionBackend,
        timeout_seconds: float = 10,
    ):
        self.repository = repository
        self.policy = policy
        self.backend = backend
        self.timeout_seconds = timeout_seconds

    async def execute(self, incident_id: UUID, plan_id: UUID) -> list[ToolResult]:
        incident, _, checkpoint = self.repository.load(incident_id)
        if incident.current_state != State.EXECUTING or checkpoint.get("plan_id") != str(plan_id):
            raise PolicyViolation("Only the current executing plan may invoke action tools")
        plan = self.repository.plan(plan_id)
        if plan.incident_id != incident_id:
            raise PolicyViolation("Plan belongs to another incident")
        self.policy.authorize(plan, self.repository.approval(plan.id))
        results = []
        for index, action in enumerate(plan.actions):
            action_id, previous, stored = self.repository.claim_action(plan.id, index)
            if previous == "SUCCEEDED":
                results.append(ToolResult.model_validate(stored["result"]))
                continue
            if previous != "PENDING":
                raise PolicyViolation(
                    "Action outcome is failed or uncertain; automatic replay is forbidden"
                )
            started = time.monotonic()
            call_id = self.repository.add_record(
                ToolCallRow,
                incident_id,
                {
                    "tool_name": action.action_type,
                    "arguments": action.arguments.model_dump(),
                    "attempt": 1,
                    "status": "STARTED",
                    "action_id": str(action_id),
                    "plan_id": str(plan.id),
                    "risk": plan.risk,
                    "approval_required": plan.approval_required,
                    "started_at": utcnow().isoformat(),
                    "idempotent": False,
                    "max_retries": 0,
                    "timeout_seconds": self.timeout_seconds,
                },
                status="STARTED",
            )
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    result = await self.backend.execute(action, action_id)
                status = "SUCCEEDED" if result.ok else "FAILED"
            except Exception as exc:
                status = "TIMED_OUT" if isinstance(exc, TimeoutError) else "FAILED"
                result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            self.repository.finish_action(action_id, status, result.model_dump(mode="json"))
            self.repository.finish_tool(
                call_id,
                status,
                {
                    "completed_at": utcnow().isoformat(),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "result": result.model_dump(mode="json"),
                    "error": result.error,
                },
            )
            results.append(result)
            if not result.ok:
                break
        return results
