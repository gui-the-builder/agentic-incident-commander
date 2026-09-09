"""Versioned evidence rubric: selected citations must prove pre-diagnosis signals."""

import math
from datetime import datetime, timedelta
from typing import Any

from commander.domain import Evidence
from commander.kubernetes import KUBERNETES_READ_TOOLS
from commander.tools import READ_TOOLS

SCORER_VERSION = "2.1"
READ_DEFINITIONS = {**READ_TOOLS, **KUBERNETES_READ_TOOLS}


def evidence_assessment(
    scenario: str,
    incident: dict[str, Any],
    selected: dict[str, Any] | None,
    timeline: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    citations = list(dict.fromkeys((selected or {}).get("supporting_evidence_ids", [])))
    records = {record["id"]: record for record in evidence}
    calls = {event["id"]: event["data"] for event in timeline if event["type"] == "tool_call"}
    valid: list[Evidence] = []
    invalid: list[str] = []
    for citation in citations:
        try:
            record = Evidence.model_validate(records[citation])
            call = calls[str(record.tool_call_id)]
            name = call["tool_name"]
            cutoff = min(
                datetime.fromisoformat((selected or {})["updated_at"]),
                datetime.fromisoformat(incident["remediation_plan"]["created_at"]),
            )
            if (
                str(record.incident_id) != incident["id"]
                or (selected or {}).get("incident_id") != incident["id"]
                or name not in READ_DEFINITIONS
                or record.source != name
                or call["status"] != "SUCCEEDED"
                or not call["result"]["ok"]
                or record.payload != call["result"]["data"]
                or record.observed_at != datetime.fromisoformat(call["result"]["observed_at"])
                or record.observed_at > cutoff
                or datetime.fromisoformat(call["completed_at"]) > cutoff
                or (
                    name != "query_database_readonly"
                    and call["arguments"].get("service") != incident["service"]
                )
            ):
                raise ValueError("Citation lacks valid pre-diagnosis provenance")
            READ_DEFINITIONS[name].input_schema.model_validate(call["arguments"])
            READ_DEFINITIONS[name].output_schema.model_validate(record.payload)
            valid.append(record)
        except (KeyError, TypeError, ValueError):
            invalid.append(citation)

    def metrics(name: str) -> list[tuple[datetime, float]]:
        result = []
        for record in valid:
            if record.source == "query_metrics" and record.payload["metric"] == name:
                call = calls[str(record.tool_call_id)]
                value = float(record.payload["value"])
                if call["arguments"].get("metric") == name and math.isfinite(value):
                    result.append((record.observed_at, value))
        return sorted(result)

    def above(name: str, threshold: float) -> bool:
        return any(value > threshold for _, value in metrics(name))

    def log_error(error_type: str) -> bool:
        for record in valid:
            if record.source != "query_logs":
                continue
            window = calls[str(record.tool_call_id)]["arguments"].get("window_minutes", 10)
            for entry in record.payload["entries"]:
                try:
                    at = datetime.fromisoformat(entry["timestamp"])
                    if (
                        record.observed_at - timedelta(minutes=window) <= at <= record.observed_at
                        and entry.get("attributes", {}).get("error_type") == error_type
                    ):
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    checks: dict[str, bool]
    if scenario == "checkout-db-pool-exhaustion":
        checks = {
            "latency_degraded": above("http_request_duration_seconds", 0.5),
            "pool_saturated_or_waiting": above("db_pool_in_use", 3.5)
            or above("db_pool_wait_seconds", 0.05),
            "connection_timeout_logs": log_error("PoolTimeout"),
        }
    elif scenario == "checkout-bad-feature-flag":
        flag_enabled = False
        for record in valid:
            if record.source != "get_recent_deployments":
                continue
            window = calls[str(record.tool_call_id)]["arguments"].get("window_minutes", 120)
            for change in record.payload["deployments"]:
                try:
                    at = datetime.fromisoformat(change["timestamp"])
                    flag_enabled |= (
                        change["service"] == "demo-api"
                        and change["changes"].get("new_checkout_path") is True
                        and record.observed_at - timedelta(minutes=window)
                        <= at
                        <= record.observed_at
                    )
                except (TypeError, ValueError):
                    continue
        checks = {
            "checkout_errors": above("http_5xx_rate", 0.02),
            "checkout_exception_logs": log_error("CheckoutPathError"),
            "flag_enabled_in_change_history": flag_enabled,
        }
    elif scenario == "worker-stalled":
        processed = metrics("worker_jobs_processed_total")
        checks = {
            "backlog_accumulated": above("worker_jobs_pending", 10),
            "processed_counter_stopped": len(processed) >= 2
            and processed[-1][0] > processed[0][0]
            and all(value == processed[0][1] for _, value in processed),
            "last_success_stale": any(
                at.timestamp() - value > 30
                for at, value in metrics("worker_last_success_timestamp")
            ),
            "worker_alive": any(
                record.source == "get_service_health"
                and record.payload["status"] == "healthy"
                and bool(record.payload["instances"])
                and all(instance["healthy"] for instance in record.payload["instances"])
                for record in valid
            ),
        }
    elif scenario == "worker-zero-replicas":
        workloads = sorted(
            (record for record in valid if record.source == "get_kubernetes_workload"),
            key=lambda record: record.observed_at,
        )
        backlogs = sorted(
            (
                record
                for record in valid
                if record.source == "query_database_readonly"
                and record.payload["query_name"] == "pending_jobs_count"
                and calls[str(record.tool_call_id)]["arguments"].get("query_name")
                == "pending_jobs_count"
            ),
            key=lambda record: record.observed_at,
        )
        workload = workloads[-1] if workloads else None
        backlog = backlogs[-1] if backlogs else None
        checks = {
            "worker_incident": incident.get("service") == "demo-worker",
            "observed_zero_replica_deployment": workload is not None
            and workload.payload["namespace"] == "incident-lab"
            and workload.payload["service"] == "demo-worker"
            and workload.payload["desired_replicas"] == 0
            and workload.payload["ready_replicas"] == 0
            and workload.payload["available_replicas"] == 0
            and workload.payload["generation"] > 0
            and workload.payload["observed_generation"] >= workload.payload["generation"],
            "complete_empty_worker_pod_list": workload is not None
            and not workload.payload["truncated"]
            and workload.payload["pods"] == [],
            "database_backlog": backlog is not None
            and backlog.payload["values"].get("pending_jobs_count", 0) > 10,
            "recent_workload_and_backlog": workload is not None
            and backlog is not None
            and min(workload.observed_at, backlog.observed_at)
            >= datetime.fromisoformat((selected or {})["updated_at"]) - timedelta(minutes=3),
        }
    else:
        checks = {"supported_scenario": False}
    return {
        "passed": bool(citations) and not invalid and all(checks.values()),
        "checks": checks,
        "cited_evidence_ids": citations,
        "invalid_citation_ids": invalid,
        "missing_checks": [name for name, passed in checks.items() if not passed],
    }
