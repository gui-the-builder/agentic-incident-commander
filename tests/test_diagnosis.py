from datetime import timedelta
from uuid import uuid4

import pytest

from commander.diagnosis import diagnosis_gaps
from commander.domain import Evidence, Hypothesis
from tests.test_evaluation_quality import AuditFixture, pool_fixture


def zero_replica_fixture() -> AuditFixture:
    audit = AuditFixture()
    audit.incident["service"] = "demo-worker"
    audit.add(
        "get_kubernetes_workload",
        {
            "service": "demo-worker",
            "namespace": "incident-lab",
            "desired_replicas": 0,
            "ready_replicas": 0,
            "available_replicas": 0,
            "generation": 2,
            "observed_generation": 2,
            "conditions": [],
            "pods": [],
            "truncated": False,
        },
    )
    audit.add(
        "query_database_readonly",
        {"query_name": "pending_jobs_count", "values": {"pending_jobs_count": 40}},
        query_name="pending_jobs_count",
    )
    # Named SQL reads have no caller-provided service field.
    del audit.timeline[-1]["data"]["arguments"]["service"]
    return audit


def test_zero_replicas_requires_cited_current_workload_and_database_evidence() -> None:
    audit = zero_replica_fixture()
    selected = hypothesis(audit, "worker_replicas_zero")
    calls = [{"id": event["id"], **event["data"]} for event in audit.timeline]
    evidence = [Evidence.model_validate(e) for e in audit.evidence]
    assert not diagnosis_gaps("demo-worker", selected, evidence, audit.at, calls)
    calls[-1]["arguments"]["query_name"] = "database_health"
    assert diagnosis_gaps("demo-worker", selected, evidence, audit.at, calls)
    # A newer uncited observation overrides an old fault snapshot.
    audit.add("get_kubernetes_workload", {**audit.evidence[0]["payload"], "desired_replicas": 1})
    assert diagnosis_gaps(
        "demo-worker", selected, [Evidence.model_validate(e) for e in audit.evidence], audit.at
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_generation", 1),
        ("truncated", True),
        ("ready_replicas", 1),
        ("available_replicas", 1),
        ("pods", [{"name": "worker-still-terminating"}]),
        ("namespace", "other"),
        ("service", "demo-api"),
    ],
)
def test_zero_replica_gate_rejects_incomplete_or_contradictory_workload(
    field: str,
    value: object,
) -> None:
    audit = zero_replica_fixture()
    audit.evidence[0]["payload"][field] = value
    assert diagnosis_gaps(
        "demo-worker",
        hypothesis(audit, "worker_replicas_zero"),
        [Evidence.model_validate(e) for e in audit.evidence],
        audit.at,
    )


def hypothesis(audit: AuditFixture, mechanism: str) -> Hypothesis:
    return Hypothesis.model_validate(
        {
            "incident_id": audit.incident["id"],
            "statement": "Observed mechanism",
            "mechanism": mechanism,
            "confidence": 0.99,
            "supporting_evidence_ids": audit.selected["supporting_evidence_ids"],
        }
    )


def test_pool_gate_requires_citations_and_current_signals() -> None:
    audit = pool_fixture()
    selected = hypothesis(audit, "connection_pool_exhaustion")
    evidence = [Evidence.model_validate(e) for e in audit.evidence]
    assert not diagnosis_gaps("demo-api", selected, evidence, audit.at)
    calls = [{"id": event["id"], **event["data"]} for event in audit.timeline]
    assert not diagnosis_gaps("demo-api", selected, evidence, audit.at, calls)
    calls[0]["arguments"]["service"] = "demo-worker"
    assert diagnosis_gaps("demo-api", selected, evidence, audit.at, calls)
    uncited = selected.model_copy(
        update={"supporting_evidence_ids": selected.supporting_evidence_ids[:1]}
    )
    assert len(diagnosis_gaps("demo-api", uncited, evidence, audit.at)) == 2
    audit.metric("http_request_duration_seconds", 0.1)  # A newer uncited recovery reading.
    updated = [Evidence.model_validate(e) for e in audit.evidence]
    assert any("latency" in gap for gap in diagnosis_gaps("demo-api", selected, updated, audit.at))


def test_gate_rejects_unknown_foreign_stale_and_wrong_service() -> None:
    audit = pool_fixture()
    selected = hypothesis(audit, "connection_pool_exhaustion")
    evidence = [Evidence.model_validate(e) for e in audit.evidence]
    assert diagnosis_gaps(
        "demo-api", selected.model_copy(update={"mechanism": "unknown"}), evidence, audit.at
    )
    assert diagnosis_gaps("demo-worker", selected, evidence, audit.at)
    assert diagnosis_gaps("demo-api", selected, evidence, audit.at + timedelta(minutes=4))
    assert diagnosis_gaps(
        "demo-api",
        selected,
        [e.model_copy(update={"incident_id": uuid4()}) for e in evidence],
        audit.at,
    )


def test_worker_gate_requires_temporal_counter_comparison() -> None:
    audit = AuditFixture()
    audit.metric("worker_jobs_pending", 100)
    audit.metric("worker_jobs_processed_total", 20)
    audit.metric("worker_last_success_timestamp", (audit.at - timedelta(minutes=2)).timestamp())
    audit.add(
        "get_service_health",
        {"status": "healthy", "instances": [{"id": "worker-1", "healthy": True, "ready": True}]},
    )
    selected = hypothesis(audit, "worker_consumer_stall")
    assert (
        len(
            diagnosis_gaps(
                "demo-worker",
                selected,
                [Evidence.model_validate(e) for e in audit.evidence],
                audit.at,
            )
        )
        == 1
    )
    audit.metric("worker_jobs_processed_total", 20)
    selected = hypothesis(audit, "worker_consumer_stall")
    assert not diagnosis_gaps(
        "demo-worker", selected, [Evidence.model_validate(e) for e in audit.evidence], audit.at
    )
    audit.metric("worker_jobs_processed_total", 25)
    assert diagnosis_gaps(
        "demo-worker", selected, [Evidence.model_validate(e) for e in audit.evidence], audit.at
    )


def test_flag_change_history_can_precede_the_current_sampling_window() -> None:
    audit = AuditFixture()
    audit.metric("http_5xx_rate", 0.4)
    audit.add(
        "query_logs",
        {
            "entries": [
                {
                    "timestamp": audit.at.isoformat(),
                    "attributes": {"error_type": "CheckoutPathError"},
                }
            ]
        },
    )
    audit.add(
        "get_recent_deployments",
        {
            "deployments": [
                {
                    "service": "demo-api",
                    "timestamp": (audit.at - timedelta(minutes=10)).isoformat(),
                    "changes": {"new_checkout_path": True},
                }
            ]
        },
    )
    assert not diagnosis_gaps(
        "demo-api",
        hypothesis(audit, "checkout_flag_regression"),
        [Evidence.model_validate(e) for e in audit.evidence],
        audit.at,
    )
