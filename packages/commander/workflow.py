"""Explicit LangGraph orchestration with repository-backed resumable checkpoints."""

import asyncio
import math
import operator
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from commander.actions import ActionExecutor
from commander.diagnosis import DIAGNOSIS_REQUIREMENTS, diagnosis_gaps
from commander.domain import (
    Contract,
    Diagnosis,
    Hypothesis,
    HypothesisSet,
    InvestigationChoice,
    RemediationPlan,
    RemediationProposal,
    State,
    Status,
    VerificationCheck,
)
from commander.models import ReasoningModel
from commander.policy import Policy, PolicyViolation, validate_diagnosis
from commander.storage import (
    HypothesisRow,
    PostmortemRow,
    Repository,
    ToolCallRow,
    VerificationRow,
)
from commander.tools import ReadGateway


class WorkflowState(TypedDict):
    incident_id: str
    next_node: str


class Report(Contract):
    markdown: str


def report_observations(
    evidence: list[dict[str, Any]],
    referenced: set[str],
) -> list[dict[str, Any]]:
    """Keep cited observations plus first/last per signal; the full audit stays in SQL."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in evidence:
        key = (item["source"], str(item["payload"].get("metric", "")))
        groups.setdefault(key, []).append(item)
    retained = set(referenced)
    for items in groups.values():
        retained.update((items[0]["id"], items[-1]["id"]))
    return [item for item in evidence if item["id"] in retained]


def verification_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for record in records:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for check in record["checks"]:
            grouped.setdefault(check["name"], []).append(check)
        summaries.append(
            {
                "success": record["success"],
                "checks": [
                    {
                        "signal": name,
                        "before": checks[0]["before"],
                        "after": checks[-1]["after"],
                        "operator": checks[-1]["operator"],
                        "target": checks[-1]["target"],
                        "samples": len(checks),
                        "passing_samples": sum(c["passed"] for c in checks),
                        "last_check_passed": checks[-1]["passed"],
                    }
                    for name, checks in grouped.items()
                ],
            }
        )
    return summaries


class Workflow:
    def __init__(
        self,
        repository: Repository,
        model: ReasoningModel,
        reads: ReadGateway,
        actions: ActionExecutor,
        policy: Policy,
        max_iterations: int = 8,
        verification_interval_seconds: float = 2,
        runtime_backend: Literal["compose", "kubernetes"] = "compose",
    ):
        self.repository = repository
        self.model = model
        self.reads = reads
        self.actions = actions
        self.policy = policy
        self.max_iterations = max_iterations
        self.verification_interval_seconds = verification_interval_seconds
        self.runtime_backend = runtime_backend
        nodes: dict[str, Callable[[UUID, dict[str, Any]], Awaitable[str]]] = {
            "normalize_alert": self.normalize,
            "gather": self.gather,
            "hypotheses": self.hypotheses,
            "investigate": self.investigate,
            "update_hypotheses": self.update_hypotheses,
            "diagnose": self.diagnose,
            "plan": self.plan,
            "await_approval": self.await_approval,
            "execute": self.execute,
            "verify": self.verify,
            "postmortem": self.postmortem,
        }
        self.states = {
            "normalize_alert": State.RECEIVED,
            "gather": State.CONTEXT_GATHERING,
            "hypotheses": State.HYPOTHESIS_GENERATION,
            "investigate": State.INVESTIGATION,
            "update_hypotheses": State.INVESTIGATION,
            "diagnose": State.INVESTIGATION,
            "plan": State.DIAGNOSED,
            "await_approval": State.AWAITING_APPROVAL,
            "execute": State.EXECUTING,
            "verify": State.VERIFYING,
            "postmortem": State.POSTMORTEM_GENERATED,
        }
        graph = StateGraph(WorkflowState)
        for name, operation in nodes.items():
            graph.add_node(name, RunnableLambda(self.node(name, operation)))
        routes: dict[Hashable, str] = {name: name for name in nodes}
        routes["done"] = END
        graph.add_conditional_edges(START, lambda state: state["next_node"], routes)
        for name in nodes:
            graph.add_conditional_edges(name, lambda state: state["next_node"], routes)
        self.graph = graph.compile()

    def node(
        self,
        name: str,
        operation: Callable[[UUID, dict[str, Any]], Awaitable[str]],
    ) -> Callable[[WorkflowState], Awaitable[WorkflowState]]:
        async def invoke(state: WorkflowState) -> WorkflowState:
            incident_id = UUID(state["incident_id"])
            _, revision, checkpoint = self.repository.load(incident_id)
            if name != "postmortem":
                self.repository.advance(
                    incident_id, revision, self.states[name], checkpoint, f"Enter {name}"
                )
            try:
                next_node = await operation(incident_id, checkpoint)
            except Exception as exc:
                checkpoint["terminal_reason"] = f"{type(exc).__name__}: {exc}"
                self.transition(
                    incident_id,
                    State.FAILED,
                    checkpoint,
                    checkpoint["terminal_reason"],
                    Status.FAILED,
                )
                next_node = "postmortem" if name != "postmortem" else "done"
            # A pause exits this invocation but retains the durable resume node.
            checkpoint["next_node"] = "await_approval" if next_node == "pause" else next_node
            incident, revision, _ = self.repository.load(incident_id)
            self.repository.advance(
                incident_id, revision, incident.current_state, checkpoint, f"Completed {name}"
            )
            return {
                "incident_id": str(incident_id),
                "next_node": "done" if next_node == "pause" else next_node,
            }

        return invoke

    async def run(self, incident_id: UUID) -> None:
        with self.repository.workflow_lock(incident_id):
            _, _, checkpoint = self.repository.load(incident_id)
            if checkpoint.get("next_node") == "done":
                return
            await self.graph.ainvoke(
                {"incident_id": str(incident_id), "next_node": checkpoint["next_node"]},
                config={"recursion_limit": self.max_iterations * 8 + 20},
            )

    def transition(
        self,
        incident_id: UUID,
        state: State,
        checkpoint: dict[str, Any],
        reason: str,
        status: Status | None = None,
    ) -> None:
        _, revision, _ = self.repository.load(incident_id)
        self.repository.advance(incident_id, revision, state, checkpoint, reason, status)

    def context(self, incident_id: UUID) -> dict[str, Any]:
        incident, _, checkpoint = self.repository.load(incident_id)
        return {
            "alert": incident.agent_alert().model_dump(mode="json"),
            "runtime_backend": self.runtime_backend,
            "action_semantics": (
                "scale_demo_worker.concurrency sets Kubernetes worker replicas (1–4). "
                "Restarts roll the existing deployment and preserve its desired replicas."
                if self.runtime_backend == "kubernetes"
                else (
                    "scale_demo_worker.concurrency changes the existing worker's "
                    "consumer concurrency (1–4)."
                )
            ),
            "diagnosis_requirements": DIAGNOSIS_REQUIREMENTS,
            "diagnosis_feedback": checkpoint.get("diagnosis_feedback", []),
            "remediation_history": [
                {**attempt, "verification": verification_summary(attempt["verification"])}
                for attempt in self.repository.remediation_history(incident_id)
            ],
            "evidence": [e.model_dump(mode="json") for e in self.repository.evidence(incident_id)],
            "hypotheses": [
                h.model_dump(mode="json")
                for h in self.repository.hypotheses(incident_id)
                if str(h.id) in checkpoint.get("hypothesis_ids", [])
            ],
            "failed_tool_calls": [
                {"id": call["id"], "tool_name": call["tool_name"], "status": call["status"]}
                for call in self.repository.records(ToolCallRow, incident_id)
                if call["status"] in {"FAILED", "TIMED_OUT", "BLOCKED"}
            ],
        }

    async def normalize(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        checkpoint.setdefault("iterations", 0)
        return "gather"

    async def gather(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        incident, _, _ = self.repository.load(incident_id)
        for tool in ("get_service_health", "get_service_config", "get_recent_deployments"):
            await self.reads.call(incident_id, tool, {"service": incident.service})
        metrics = (
            [
                "http_request_duration_seconds",
                "http_5xx_rate",
                "db_pool_in_use",
                "db_pool_wait_seconds",
            ]
            if incident.service == "demo-api"
            else [
                "worker_jobs_pending",
                "worker_jobs_processed_total",
                "worker_last_success_timestamp",
            ]
        )
        for metric in metrics:
            await self.reads.call(
                incident_id,
                "query_metrics",
                {
                    "service": incident.service,
                    "metric": metric,
                    "window_minutes": 1,
                },
            )
        return "hypotheses"

    async def save_hypotheses(
        self,
        incident_id: UUID,
        checkpoint: dict[str, Any],
        task: str,
    ) -> None:
        generated = await self.model.complete_structured(
            task, self.context(incident_id), HypothesisSet
        )
        checkpoint["hypothesis_ids"] = []
        valid_ids = {e.id for e in self.repository.evidence(incident_id)}
        for draft in generated.hypotheses:
            refs = set(draft.supporting_evidence_ids + draft.contradicting_evidence_ids)
            if not refs <= valid_ids:
                raise PolicyViolation("Model cited nonexistent evidence")
            hypothesis = Hypothesis(**draft.model_dump(), incident_id=incident_id)
            self.repository.add_record(
                HypothesisRow, incident_id, hypothesis.model_dump(mode="json")
            )
            checkpoint["hypothesis_ids"].append(str(hypothesis.id))

    async def hypotheses(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        await self.save_hypotheses(incident_id, checkpoint, "hypotheses")
        return "investigate"

    async def investigate(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        if checkpoint["iterations"] >= self.max_iterations:
            checkpoint["terminal_reason"] = "Maximum investigation iterations reached"
            self.transition(
                incident_id,
                State.ESCALATED,
                checkpoint,
                checkpoint["terminal_reason"],
                Status.ESCALATED,
            )
            return "postmortem"
        checkpoint["iterations"] += 1
        # Persist budget consumption before calling any external system.
        self.transition(
            incident_id, State.INVESTIGATION, checkpoint, "Consume investigation budget"
        )
        choice = await self.model.complete_structured(
            "investigate",
            {
                **self.context(incident_id),
                "tools": self.reads.catalog(),
            },
            InvestigationChoice,
        )
        await self.reads.call(incident_id, choice.tool, choice.arguments)
        return "update_hypotheses"

    async def update_hypotheses(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        await self.save_hypotheses(incident_id, checkpoint, "update_hypotheses")
        return "diagnose"

    async def diagnose(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        context = self.context(incident_id)
        diagnosis = await self.model.complete_structured("diagnosis", context, Diagnosis)
        try:
            selected = validate_diagnosis(
                diagnosis,
                [
                    h
                    for h in self.repository.hypotheses(incident_id)
                    if str(h.id) in checkpoint["hypothesis_ids"]
                ],
                self.repository.evidence(incident_id),
                {UUID(c["id"]) for c in context["failed_tool_calls"]},
            )
        except PolicyViolation as exc:
            # A rejected selection is a recoverable reasoning error, not an infrastructure
            # failure. Return to bounded investigation instead of ending the incident; the
            # existing iteration budget still escalates. No plan or action is created.
            checkpoint["diagnosis_feedback"] = [
                f"The previous diagnosis was rejected: {exc}. Select a persisted hypothesis "
                "whose supporting_evidence_ids are IDs of this incident's evidence, and "
                "acknowledge every failed tool call ID."
            ]
            self.transition(
                incident_id,
                State.INVESTIGATION,
                checkpoint,
                f"Diagnosis rejected: {exc}",
            )
            return "investigate"
        if selected.confidence < 0.65:
            return "investigate"
        incident, _, _ = self.repository.load(incident_id)
        gaps = diagnosis_gaps(
            incident.service,
            selected,
            self.repository.evidence(incident_id),
            tool_calls=self.repository.records(ToolCallRow, incident_id),
        )
        checkpoint["diagnosis_feedback"] = gaps
        if gaps:
            self.transition(
                incident_id,
                State.INVESTIGATION,
                checkpoint,
                "Diagnosis evidence incomplete: " + "; ".join(gaps),
            )
            return "investigate"
        checkpoint["selected_hypothesis_id"] = str(selected.id)
        self.repository.select_hypothesis(incident_id, selected.id)
        return "plan"

    async def plan(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        proposed = await self.model.complete_structured(
            "remediation",
            {
                **self.context(incident_id),
                "selected_hypothesis_id": checkpoint["selected_hypothesis_id"],
                "action_schema": RemediationProposal.model_json_schema(),
            },
            RemediationProposal,
        )
        # Add non-negotiable health criteria independent of the model's suggested targets.
        incident, _, _ = self.repository.load(incident_id)
        required = (
            [
                ("http_request_duration_seconds", "<= 0.5"),
                ("db_pool_wait_seconds", "<= 0.05"),
                ("http_5xx_rate", "<= 0.02"),
            ]
            if incident.service == "demo-api"
            else [
                ("worker_jobs_pending", "< 10"),
                ("worker_jobs_processed_total", ">= 0"),
                ("worker_last_success_timestamp", f">= {int(time.time()) - 30}"),
            ]
        )
        canonical = [
            VerificationCheck.model_validate({"signal": signal, "target": target})
            for signal, target in required
        ]
        checks = {check.signal: check for check in proposed.verification_checks}
        checks.update({check.signal: check for check in canonical})
        proposed = proposed.model_copy(update={"verification_checks": list(checks.values())})
        risk, approval_required = self.policy.classify(proposed)
        plan = RemediationPlan(
            **proposed.model_dump(),
            incident_id=incident_id,
            diagnosis_hypothesis_id=UUID(checkpoint["selected_hypothesis_id"]),
            risk=risk,
            approval_required=approval_required,
        )
        self.repository.save_plan(plan)
        checkpoint["plan_id"] = str(plan.id)
        checkpoint["before"] = await self.signals(incident_id, plan)
        self.transition(incident_id, State.REMEDIATION_PLANNED, checkpoint, plan.summary)
        return "await_approval" if approval_required else "execute"

    async def await_approval(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        approval = self.repository.approval(UUID(checkpoint["plan_id"]))
        if approval is None:
            self.transition(
                incident_id,
                State.AWAITING_APPROVAL,
                checkpoint,
                "Operator decision required",
                Status.WAITING_FOR_APPROVAL,
            )
            return "pause"
        if approval.decision == "REJECTED":
            checkpoint["terminal_reason"] = "Operator rejected remediation"
            self.transition(
                incident_id,
                State.ESCALATED,
                checkpoint,
                checkpoint["terminal_reason"],
                Status.ESCALATED,
            )
            return "postmortem"
        self.transition(
            incident_id,
            State.AWAITING_APPROVAL,
            checkpoint,
            "Operator approved remediation",
            Status.OPEN,
        )
        return "execute"

    async def execute(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        results = await self.actions.execute(incident_id, UUID(checkpoint["plan_id"]))
        if not all(result.ok for result in results):
            raise PolicyViolation("Remediation action failed; inspect the audit before retrying")
        return "verify"

    async def signals(self, incident_id: UUID, plan: RemediationPlan) -> dict[str, float | None]:
        incident, _, _ = self.repository.load(incident_id)
        values: dict[str, float | None] = {}
        for check in plan.verification_checks:
            result = await self.reads.call(
                incident_id,
                "query_metrics",
                {
                    "service": incident.service,
                    "metric": check.signal,
                    "window_minutes": check.window_minutes,
                    "aggregation": "p95"
                    if check.signal == "http_request_duration_seconds"
                    else "latest",
                },
            )
            values[check.signal] = float(result.data["value"]) if result.ok else None
        return values

    async def verify(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        plan = self.repository.plan(UUID(checkpoint["plan_id"]))
        comparisons = {"<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt}
        samples: list[dict[str, Any]] = []
        success = False
        consecutive = {check.signal: 0 for check in plan.verification_checks}
        required = max(check.consecutive_checks for check in plan.verification_checks)
        progress_baseline = checkpoint["before"].get("worker_jobs_processed_total")
        # Rate/histogram windows retain pre-remediation failures. Allow one full
        # observation window to clear, bounded independently of investigation loops.
        grace = (
            math.ceil(
                max(c.window_minutes for c in plan.verification_checks)
                * 60
                / self.verification_interval_seconds
            )
            if self.verification_interval_seconds
            else 0
        )
        # An absent worker may have no pre-action scrape. Reserve one observation
        # to establish a real counter baseline; never substitute an invented zero.
        baseline_samples = int(
            progress_baseline is None
            and any(c.signal == "worker_jobs_processed_total" for c in plan.verification_checks)
        )
        for sample in range(required + grace + baseline_samples):
            if sample:
                await asyncio.sleep(self.verification_interval_seconds)
            values = await self.signals(incident_id, plan)
            for check in plan.verification_checks:
                comparison, threshold = check.target.split(" ")
                value = values[check.signal]
                passed = value is not None and comparisons[comparison](value, float(threshold))
                if check.signal == "worker_last_success_timestamp" and value is not None:
                    passed = passed and 0 <= time.time() - value <= 30
                if check.signal == "worker_jobs_processed_total":
                    if progress_baseline is None and value is not None:
                        progress_baseline = value
                    passed = (
                        passed
                        and progress_baseline is not None
                        and value is not None
                        and value > progress_baseline
                    )
                consecutive[check.signal] = consecutive[check.signal] + 1 if passed else 0
                samples.append(
                    {
                        "name": check.signal,
                        "sample": sample + 1,
                        "before": checkpoint["before"].get(check.signal),
                        "after": value,
                        "operator": comparison,
                        "target": float(threshold),
                        "passed": passed,
                        **(
                            {"progress_baseline": progress_baseline}
                            if check.signal == "worker_jobs_processed_total"
                            else {}
                        ),
                    }
                )
            success = all(
                consecutive[check.signal] >= check.consecutive_checks
                for check in plan.verification_checks
            )
            if success:
                break
        self.repository.add_record(
            VerificationRow,
            incident_id,
            {
                "success": success,
                "checks": samples,
                "summary": "Recovery confirmed" if success else "Recovery criteria not met",
            },
            plan_id=str(plan.id),
        )
        if success:
            self.transition(
                incident_id,
                State.RESOLVED,
                checkpoint,
                "Recovery confirmed by consecutive observations",
                Status.RESOLVED,
            )
            return "postmortem"
        self.transition(
            incident_id,
            State.INVESTIGATION,
            checkpoint,
            "Recovery failed; gather more evidence",
            Status.OPEN,
        )
        return "investigate"

    async def postmortem(self, incident_id: UUID, checkpoint: dict[str, Any]) -> str:
        incident, _, _ = self.repository.load(incident_id)
        context = self.context(incident_id)
        verification = verification_summary(self.repository.records(VerificationRow, incident_id))
        timeline = self.repository.timeline(incident_id)
        referenced = {
            reference
            for hypothesis in context["hypotheses"]
            for reference in (
                hypothesis["supporting_evidence_ids"] + hypothesis["contradicting_evidence_ids"]
            )
        }
        context["evidence"] = report_observations(context["evidence"], referenced)
        timeline = [
            event
            for event in timeline
            if event["type"] == "approval"
            or (
                event["type"] == "state_transition"
                and event["data"].get("from_state") != event["data"].get("to_state")
            )
        ]
        # Deterministic factual sections survive model outages and preserve the audit.
        lines = [
            f"# Incident: {incident.title}",
            "",
            f"Status: {incident.status}",
            "",
            "## Summary",
            checkpoint.get("terminal_reason", "Recovery verified"),
            "",
            "## Impact",
            incident.description or f"Alert affected {incident.service}.",
            "",
            "## Timeline",
        ]
        lines.extend(f"- {event['timestamp']}: {event['summary']}" for event in timeline)
        lines.extend(["", "## Root cause"])
        selected = next(
            (
                h
                for h in context["hypotheses"]
                if h["id"] == checkpoint.get("selected_hypothesis_id")
            ),
            None,
        )
        lines.append(
            selected["statement"] if selected else "Unconfirmed; investigation incomplete."
        )
        lines.extend(["", "## Evidence"])
        lines.extend(f"- {e['id']} ({e['source']}): {e['summary']}" for e in context["evidence"])
        lines.extend(["", "## Remediation"])
        if context["remediation_history"]:
            for attempt in context["remediation_history"]:
                lines.append(f"Plan {attempt['plan_id']}: {attempt['summary']}")
                if attempt["approval"]:
                    lines.append(f"Approval decision: {attempt['approval']['decision']}.")
                for action in attempt["actions"]:
                    lines.append(f"- {action['action_type']}: {action['status']}.")
        else:
            lines.append("No remediation plan executed.")
        lines.extend(["", "## Verification"])
        for record in verification:
            lines.append(f"Recovery checks passed: {record['success']}.")
            lines.append("\n| Signal | Before | After | Target | Passing samples |")
            lines.append("| --- | --- | --- | --- | --- |")
            lines.extend(
                f"| {check['signal']} | {check['before']} | {check['after']} | "
                f"{check['operator']} {check['target']} | "
                f"{check['passing_samples']}/{check['samples']} |"
                for check in record["checks"]
            )
        if not verification:
            lines.append("Recovery has not been verified.")
        lines.extend(
            [
                "",
                "## Follow-up actions",
                "Review the evidence and add a regression fixture.",
                "",
                "## Confidence and unknowns",
                f"Diagnosis confidence: {selected['confidence'] if selected else 'unknown'}.",
            ]
        )
        if selected is not None:
            try:
                report = await self.model.complete_structured(
                    "postmortem",
                    {
                        **context,
                        "selected_diagnosis": selected,
                        "verified_status": incident.status,
                        "verification": verification,
                    },
                    Report,
                )
                lines.extend(["", "## Model synthesis", report.markdown])
            except Exception:
                lines.append(
                    "Model synthesis unavailable; the factual audit above remains available."
                )
        else:
            lines.append("No model synthesis requested because the root cause remains unconfirmed.")
        self.repository.add_record(PostmortemRow, incident_id, {"markdown": "\n".join(lines)})
        self.transition(
            incident_id, State.POSTMORTEM_GENERATED, checkpoint, "Incident report saved"
        )
        return "done"
