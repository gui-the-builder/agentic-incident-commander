"""Operational diagnosis gates keyed by claimed mechanism, never hidden fixture identity."""

from datetime import datetime, timedelta
from typing import Any

from commander.domain import Evidence, Hypothesis, Service, utcnow

DIAGNOSIS_REQUIREMENTS = {
    "connection_pool_exhaustion": (
        "Cite checkout latency above 0.5 seconds, pool use at capacity (four) or acquisition "
        "wait above 0.05 seconds, and recent PoolTimeout logs. Explain how connection hold "
        "time or contention exhausts the pool."
    ),
    "checkout_flag_regression": (
        "Cite checkout error ratio above 2%, recent CheckoutPathError logs, and change "
        "history showing new_checkout_path enabled. Explain the failing application path."
    ),
    "worker_consumer_stall": (
        "Cite backlog above ten, two unchanged processed-counter observations at different "
        "times, last success older than 30 seconds, and a healthy worker instance. "
        "A single counter reading cannot show that processing stopped."
    ),
    "unknown": "Keep investigating or escalate; unclassified causes cannot authorize remediation.",
    "worker_replicas_zero": (
        "Cite a current Kubernetes demo-worker deployment with zero desired/ready/available "
        "replicas, its generation observed by the controller, and a complete empty pod list. "
        "Cite pending_jobs_count above ten from the read-only database tool. "
        "Missing worker metrics alone do not establish why processing stopped."
    ),
}


def diagnosis_gaps(
    service: Service,
    hypothesis: Hypothesis,
    evidence: list[Evidence],
    now: datetime | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> list[str]:
    now = now or utcnow()
    recent_evidence = [
        e
        for e in evidence
        if e.incident_id == hypothesis.incident_id
        and now - timedelta(minutes=3) <= e.observed_at <= now
    ]
    if tool_calls is not None:
        calls = {call["id"]: call for call in tool_calls}
        recent_evidence = [
            e
            for e in recent_evidence
            if (call := calls.get(str(e.tool_call_id))) is not None
            and call.get("status") == "SUCCEEDED"
            and call.get("tool_name") == e.source
            and (
                call.get("arguments", {}).get("service") == service
                or (
                    service == "demo-worker"
                    and e.source == "query_database_readonly"
                    and call.get("arguments", {}).get("query_name") == "pending_jobs_count"
                    and e.payload.get("query_name") == "pending_jobs_count"
                )
            )
        ]
    cited = [e for e in recent_evidence if e.id in hypothesis.supporting_evidence_ids]

    def metric(name: str) -> list[tuple[datetime, float]]:
        return sorted(
            (e.observed_at, float(e.payload["value"]))
            for e in cited
            if e.source == "query_metrics" and e.payload.get("metric") == name
        )

    def current_metric(name: str) -> tuple[datetime, float] | None:
        readings = sorted(
            (e.observed_at, float(e.payload["value"]))
            for e in recent_evidence
            if e.source == "query_metrics" and e.payload.get("metric") == name
        )
        return readings[-1] if readings else None

    def above(name: str, threshold: float) -> bool:
        current = current_metric(name)
        return (
            current is not None
            and current[1] > threshold
            and any(value > threshold for _, value in metric(name))
        )

    def recent(entry: dict[str, Any], observation: Evidence, minutes: int = 3) -> bool:
        try:
            at = datetime.fromisoformat(entry["timestamp"])
            return now - timedelta(minutes=minutes) <= at <= observation.observed_at
        except (KeyError, TypeError, ValueError):
            return False

    def log(error: str) -> bool:
        return any(
            entry.get("attributes", {}).get("error_type") == error and recent(entry, e)
            for e in cited
            if e.source == "query_logs"
            for entry in e.payload.get("entries", [])
        )

    match hypothesis.mechanism:
        case "connection_pool_exhaustion":
            required = {
                "The claimed mechanism must apply to demo-api": service == "demo-api",
                "Cite measured checkout latency above 0.5 seconds": above(
                    "http_request_duration_seconds", 0.5
                ),
                "Cite a saturated pool or elevated connection acquisition wait": above(
                    "db_pool_in_use", 3.5
                )
                or above("db_pool_wait_seconds", 0.05),
                "Cite recent PoolTimeout log evidence": log("PoolTimeout"),
            }
        case "checkout_flag_regression":
            required = {
                "The claimed mechanism must apply to demo-api": service == "demo-api",
                "Cite measured checkout error ratio above 2%": above("http_5xx_rate", 0.02),
                "Cite recent CheckoutPathError log evidence": log("CheckoutPathError"),
                "Cite change history enabling new_checkout_path": any(
                    change.get("service") == "demo-api"
                    and change.get("changes", {}).get("new_checkout_path") is True
                    and recent(change, e, minutes=120)
                    for e in cited
                    if e.source == "get_recent_deployments"
                    for change in e.payload.get("deployments", [])
                ),
            }
        case "worker_consumer_stall":
            processed = metric("worker_jobs_processed_total")
            current_processed = current_metric("worker_jobs_processed_total")
            current_success = current_metric("worker_last_success_timestamp")
            health = sorted(
                (e for e in recent_evidence if e.source == "get_service_health"),
                key=lambda e: e.observed_at,
            )
            required = {
                "The claimed mechanism must apply to demo-worker": service == "demo-worker",
                "Cite measured backlog above ten": above("worker_jobs_pending", 10),
                "Cite two unchanged processed-counter observations at distinct times": len(
                    processed
                )
                >= 2
                and processed[-1][0] > processed[0][0]
                and all(value == processed[0][1] for _, value in processed)
                and current_processed is not None
                and current_processed[1] == processed[-1][1],
                "Cite a last-success timestamp older than 30 seconds": current_success is not None
                and current_success[0].timestamp() - current_success[1] > 30
                and any(
                    at.timestamp() - value > 30
                    for at, value in metric("worker_last_success_timestamp")
                ),
                "Cite a healthy worker instance": bool(health)
                and health[-1].payload.get("status") == "healthy"
                and any(
                    e.source == "get_service_health"
                    and e.payload.get("status") == "healthy"
                    and bool(e.payload.get("instances"))
                    and all(i.get("healthy") is True for i in e.payload["instances"])
                    for e in cited
                ),
            }
        case "worker_replicas_zero":
            workloads = sorted(
                (e for e in recent_evidence if e.source == "get_kubernetes_workload"),
                key=lambda e: e.observed_at,
            )
            backlogs = sorted(
                (
                    e
                    for e in recent_evidence
                    if e.source == "query_database_readonly"
                    and e.payload.get("query_name") == "pending_jobs_count"
                ),
                key=lambda e: e.observed_at,
            )
            workload = workloads[-1] if workloads else None
            backlog = backlogs[-1] if backlogs else None
            required = {
                "The claimed mechanism must apply to demo-worker": service == "demo-worker",
                "Cite the latest complete observation of an observed zero-replica worker": (
                    workload is not None
                    and workload in cited
                    and workload.payload.get("service") == "demo-worker"
                    and workload.payload.get("namespace") == "incident-lab"
                    and workload.payload.get("desired_replicas") == 0
                    and workload.payload.get("ready_replicas") == 0
                    and workload.payload.get("available_replicas") == 0
                    and workload.payload.get("generation", 0) > 0
                    and workload.payload.get("observed_generation", -1)
                    >= workload.payload["generation"]
                    and workload.payload.get("pods") == []
                    and workload.payload.get("truncated") is False
                ),
                "Cite the latest database pending_jobs_count above ten": (
                    backlog is not None
                    and backlog in cited
                    and backlog.payload.get("values", {}).get("pending_jobs_count", 0) > 10
                ),
            }
        case _:
            return ["Identify a supported causal mechanism from observations before remediation"]
    return [requirement for requirement, passed in required.items() if not passed]
