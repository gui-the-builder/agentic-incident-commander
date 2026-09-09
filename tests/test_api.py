import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from commander.api import create_app
from commander.config import Settings
from commander.domain import State, Status
from commander.storage import Base, Repository, VerificationRow, make_engine
from commander.tools import ReadGateway
from tests.test_storage import persisted_plan
from tests.test_tools import healthy


def test_operator_lifecycle_is_durable_and_authenticated(tmp_path: Path) -> None:
    database = make_engine("sqlite:///" + str(tmp_path / "api.db"))
    Base.metadata.create_all(database)
    repository = Repository(database)
    settings = Settings(_env_file=None, operator_token="test-operator")
    with TestClient(create_app(settings, repository)) as client:
        alert = {"title": "Checkout latency", "service": "demo-api", "severity": "SEV2"}
        assert client.post("/api/v1/incidents", json=alert).status_code == 401
        client.headers["Authorization"] = "Bearer test-operator"
        response = client.post("/api/v1/incidents", json=alert)
        assert response.status_code == 201 and response.json()["current_state"] == "RECEIVED"
        incident_id = UUID(response.json()["id"])
        path = f"/api/v1/incidents/{incident_id}"
        incident = repository.load(incident_id)[0]
        assert client.get(path).json()["status"] == "OPEN"
        assert len(client.get(path + "/timeline").json()) == 1
        assert client.get(path + "/postmortem").status_code == 409
        assert client.get(path + "/evidence").json() == []
        assert client.get(path + "/remediations").json() == []
        assert (
            client.get(
                path + "/remediations", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        assert client.get(f"/api/v1/incidents/{uuid4()}/remediations").status_code == 404
        assert (
            client.get(path + "/evidence", headers={"Authorization": "Bearer invalid"}).status_code
            == 401
        )
        assert client.get(f"/api/v1/incidents/{uuid4()}/evidence").status_code == 404
        asyncio.run(
            ReadGateway(repository, {"get_service_health": healthy}).call(
                incident_id, "get_service_health", {"service": "demo-api"}
            )
        )
        exported = client.get(path + "/evidence").json()
        assert len(exported) == 1 and exported[0]["incident_id"] == str(incident_id)
        assert exported[0]["payload"]["status"] == "healthy"
        request = {"decision": "APPROVED", "actor": "test-operator"}
        assert client.post(path + "/approval", json=request).status_code == 409
        plan = persisted_plan(repository, incident)
        repository.advance(
            incident_id,
            0,
            State.AWAITING_APPROVAL,
            {"plan_id": str(plan.id)},
            "Review",
            Status.WAITING_FOR_APPROVAL,
        )
        assert client.get(path).json()["remediation_plan"]["id"] == str(plan.id)
        assert client.post(path + "/approval", json=request).status_code == 200
        assert client.post(path + "/approval", json=request).status_code == 409
        second_plan = persisted_plan(repository, incident)
        repository.claim_action(plan.id, 0)
        repository.add_record(
            VerificationRow,
            incident_id,
            {"success": False, "checks": [], "summary": "Fixture verification failed"},
            plan_id=str(plan.id),
        )
        history = client.get(path + "/remediations").json()
        assert {attempt["plan_id"] for attempt in history} == {str(plan.id), str(second_plan.id)}
        first = next(attempt for attempt in history if attempt["plan_id"] == str(plan.id))
        assert first["actions"][0]["status"] == "STARTED"
        assert first["verification"][0]["success"] is False
        assert first["approval"]["decision"] == "APPROVED"
        assert first["diagnosis_hypothesis_id"] == str(plan.diagnosis_hypothesis_id)
        assert first["risk"] == "MEDIUM" and first["approval_required"]
        verification = next(
            e for e in client.get(path + "/timeline").json() if e["type"] == "verification"
        )
        assert verification["data"]["plan_id"] == str(plan.id)
        assert client.get(f"/api/v1/incidents/{uuid4()}").status_code == 404
        assert client.get(path + "/hypotheses").json()[0]["statement"] == "Failure"
    database.dispose()
