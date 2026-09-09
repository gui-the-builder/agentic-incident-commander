"""Operator endpoints; incident creation is durable before the worker sees it."""

from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from commander.config import Settings
from commander.domain import Alert, Approval, ApprovalRequest, Evidence, Hypothesis, Incident
from commander.logging_config import configure_logging
from commander.storage import Conflict, NotFound, PostmortemRow, Repository, make_engine


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    from commander.security import token_dependency

    configure_logging("commander-api")
    settings = settings or Settings()
    repository = repository or Repository(make_engine(settings.database_url))
    app = FastAPI(title="Agentic Incident Commander", version="0.1.0")
    authorized = Depends(token_dependency(settings.operator_token))

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    def ready() -> dict[str, bool]:
        with repository.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ready": True}

    @app.post(
        "/api/v1/incidents", response_model=Incident, status_code=201, dependencies=[authorized]
    )
    def create_incident(alert: Alert) -> Incident:
        return repository.create_incident(alert)

    @app.get("/api/v1/incidents/{incident_id}", dependencies=[authorized])
    def incident(incident_id: UUID) -> dict[str, Any]:
        incident, revision, checkpoint = repository.load(incident_id)
        response = incident.model_dump(mode="json")
        response["revision"] = revision
        if checkpoint.get("plan_id"):
            plan = repository.plan(UUID(checkpoint["plan_id"]))
            response["remediation_plan"] = plan.model_dump(mode="json")
            approval = repository.approval(plan.id)
            response["approval"] = approval.model_dump(mode="json") if approval else None
        return response

    @app.get("/api/v1/incidents/{incident_id}/timeline", dependencies=[authorized])
    def timeline(incident_id: UUID) -> list[dict[str, Any]]:
        return repository.timeline(incident_id)

    @app.get("/api/v1/incidents/{incident_id}/hypotheses", dependencies=[authorized])
    def hypotheses(incident_id: UUID) -> list[Hypothesis]:
        repository.load(incident_id)
        return repository.hypotheses(incident_id)

    @app.get("/api/v1/incidents/{incident_id}/evidence", dependencies=[authorized])
    def evidence(incident_id: UUID) -> list[Evidence]:
        repository.load(incident_id)
        return repository.evidence(incident_id)

    @app.post("/api/v1/incidents/{incident_id}/approval", dependencies=[authorized])
    def approve(incident_id: UUID, request: ApprovalRequest) -> Approval:
        return repository.approve(incident_id, request)

    @app.get("/api/v1/incidents/{incident_id}/remediations", dependencies=[authorized])
    def remediations(incident_id: UUID) -> list[dict[str, Any]]:
        repository.load(incident_id)
        return repository.remediation_history(incident_id)

    @app.get("/api/v1/incidents/{incident_id}/postmortem", dependencies=[authorized])
    def postmortem(incident_id: UUID) -> PlainTextResponse:
        repository.load(incident_id)
        reports = repository.records(PostmortemRow, incident_id)
        if not reports:
            raise HTTPException(409, "Postmortem is not available yet")
        return PlainTextResponse(reports[-1]["markdown"], media_type="text/markdown")

    return app
