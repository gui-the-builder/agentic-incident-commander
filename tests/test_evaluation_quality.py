import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from commander.domain import Evidence
from commander.evaluation_quality import evidence_assessment
from commander.rescore import rescore


class AuditFixture:
    def __init__(self) -> None:
        self.at = datetime(2026, 9, 8, 12, tzinfo=UTC)
        self.incident = {
            "id": str(uuid4()),
            "service": "demo-api",
            "remediation_plan": {
                "created_at": (self.at + timedelta(minutes=2)).isoformat(),
            },
        }
        self.selected = {
            "incident_id": self.incident["id"],
            "updated_at": (self.at + timedelta(minutes=1)).isoformat(),
            "supporting_evidence_ids": [],
        }
        self.timeline: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []

    def add(self, name: str, payload: dict[str, Any], **arguments: Any) -> None:
        self.at += timedelta(seconds=1)
        call_id = uuid4()
        record = Evidence(
            incident_id=self.incident["id"],
            tool_call_id=call_id,
            source=name,
            evidence_type=name,
            summary="fixture",
            payload=payload,
            observed_at=self.at,
        )
        self.evidence.append(record.model_dump(mode="json"))
        self.selected["supporting_evidence_ids"].append(str(record.id))
        self.timeline.append(
            {
                "id": str(call_id),
                "type": "tool_call",
                "data": {
                    "tool_name": name,
                    "status": "SUCCEEDED",
                    "completed_at": self.at.isoformat(),
                    "arguments": {"service": self.incident["service"], **arguments},
                    "result": {
                        "ok": True,
                        "data": deepcopy(payload),
                        "observed_at": self.at.isoformat(),
                    },
                },
            }
        )

    def metric(self, name: str, value: float) -> None:
        self.add(
            "query_metrics",
            {"metric": name, "value": value, "unit": "value", "window_minutes": 3},
            metric=name,
        )

    def assess(self, scenario: str) -> dict[str, Any]:
        return evidence_assessment(
            scenario, self.incident, self.selected, self.timeline, self.evidence
        )


def pool_fixture() -> AuditFixture:
    audit = AuditFixture()
    audit.metric("http_request_duration_seconds", 2.5)
    audit.metric("db_pool_in_use", 4)
    audit.add(
        "query_logs",
        {
            "entries": [
                {
                    "timestamp": audit.at.isoformat(),
                    "level": "ERROR",
                    "message": "database connection acquisition timed out",
                    "attributes": {"error_type": "PoolTimeout"},
                }
            ]
        },
    )
    return audit


def test_pool_rubric_requires_cited_observed_signals() -> None:
    audit = pool_fixture()
    assert audit.assess("checkout-db-pool-exhaustion")["passed"]
    audit.selected["supporting_evidence_ids"].pop(0)
    result = audit.assess("checkout-db-pool-exhaustion")
    assert not result["passed"] and result["missing_checks"] == ["latency_degraded"]


@pytest.mark.parametrize(
    "mutation",
    ["healthy", "failed", "foreign", "future", "fabricated", "missing", "old_log", "wrong_service"],
)
def test_evidence_rubric_rejects_false_positives(mutation: str) -> None:
    audit = pool_fixture()
    record = audit.evidence[0]
    call = audit.timeline[0]["data"]
    if mutation == "healthy":
        record["payload"]["value"] = call["result"]["data"]["value"] = 0.1
    elif mutation == "failed":
        call["status"] = "FAILED"
    elif mutation == "foreign":
        record["incident_id"] = str(uuid4())
    elif mutation == "future":
        record["observed_at"] = call["result"]["observed_at"] = "2026-09-08T13:00:00Z"
    elif mutation == "fabricated":
        record["payload"]["value"] = 7
    elif mutation == "missing":
        audit.evidence.clear()
    elif mutation == "old_log":
        audit.evidence[-1]["payload"]["entries"][0]["timestamp"] = "2026-09-07T12:00:00Z"
        audit.timeline[-1]["data"]["result"]["data"] = deepcopy(audit.evidence[-1]["payload"])
    else:
        call["arguments"]["service"] = "demo-worker"
    assert not audit.assess("checkout-db-pool-exhaustion")["passed"]


def test_flag_rubric_requires_real_enabled_change_and_exception() -> None:
    audit = AuditFixture()
    audit.metric("http_5xx_rate", 0.4)
    audit.add(
        "query_logs",
        {
            "entries": [
                {
                    "timestamp": audit.at.isoformat(),
                    "level": "ERROR",
                    "message": "Checkout failed",
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
                    "timestamp": audit.at.isoformat(),
                    "service": "demo-api",
                    "version": "local-v1",
                    "changes": {"new_checkout_path": True},
                }
            ]
        },
    )
    assert audit.assess("checkout-bad-feature-flag")["passed"]
    audit.evidence[-1]["payload"]["deployments"][0]["changes"]["new_checkout_path"] = False
    audit.timeline[-1]["data"]["result"]["data"] = deepcopy(audit.evidence[-1]["payload"])
    assert not audit.assess("checkout-bad-feature-flag")["passed"]


def test_worker_rubric_requires_counter_history_and_liveness() -> None:
    audit = AuditFixture()
    audit.incident["service"] = "demo-worker"
    audit.metric("worker_jobs_pending", 100)
    audit.metric("worker_jobs_processed_total", 20)
    audit.metric("worker_last_success_timestamp", (audit.at - timedelta(minutes=2)).timestamp())
    audit.add(
        "get_service_health",
        {"status": "healthy", "instances": [{"id": "worker-1", "healthy": True, "ready": True}]},
    )
    assert not audit.assess("worker-stalled")["passed"]
    audit.metric("worker_jobs_processed_total", 20)
    assert audit.assess("worker-stalled")["passed"]
    audit.metric("worker_jobs_processed_total", 21)
    assert not audit.assess("worker-stalled")["passed"]


def test_missing_selected_diagnosis_never_passes() -> None:
    audit = pool_fixture()
    assert not evidence_assessment(
        "checkout-db-pool-exhaustion", audit.incident, None, audit.timeline, audit.evidence
    )["passed"]
    assert not audit.assess("unknown-scenario")["passed"]


def test_rescore_preserves_originals_and_reports_missing_exports(tmp_path: Path) -> None:
    source = tmp_path / "original"
    run = source / "run-a"
    run.mkdir(parents=True)
    audit = {
        "incident": {
            "id": "run-a",
            "status": "FAILED",
            "current_state": "FAILED",
            "created_at": "2026-09-08T12:00:00Z",
            "updated_at": "2026-09-08T12:00:01Z",
        },
        "hypotheses": [],
        "timeline": [],
    }
    text = json.dumps(audit)
    (run / "audit.json").write_text(text)
    (run / "run.json").write_text(json.dumps({"scenario_id": "worker-stalled"}))
    (run / "result.json").write_text('{"evidence_passed":true}')
    output = tmp_path / "v2"
    result = rescore(source, output)[0]
    assert result["scorer_version"] == "2.1"
    assert not result["evidence_export_available"] and not result["evidence_passed"]
    assert (run / "audit.json").read_text() == text
    assert (run / "result.json").read_text() == '{"evidence_passed":true}'
    assert (output / "results.csv").exists()
    with pytest.raises(ValueError, match="empty"):
        rescore(source, output)
    with pytest.raises(ValueError, match="separate"):
        rescore(source, source)
