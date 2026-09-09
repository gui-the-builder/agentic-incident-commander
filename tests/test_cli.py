from typing import Any

import httpx
import pytest

from commander.cli import OperatorClient, follow_incident


@pytest.mark.parametrize(
    "answer, decision", [("y", "APPROVED"), ("", "REJECTED"), ("no", "REJECTED")]
)
def test_interactive_demo_requires_explicit_yes(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    decision: str,
) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []
    statuses = iter(
        [
            {
                "current_state": "AWAITING_APPROVAL",
                "status": "WAITING_FOR_APPROVAL",
                "approval": None,
                "remediation_plan": {"summary": "Reviewed change"},
            },
            {"current_state": "POSTMORTEM_GENERATED", "status": "RESOLVED"},
        ]
    )

    def request(
        self: OperatorClient,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        scenario: bool = False,
    ) -> httpx.Response:
        requests.append((method, path, body))
        if method == "POST":
            return httpx.Response(200, json={"decision": body["decision"] if body else None})
        if path.endswith("/postmortem"):
            return httpx.Response(200, text="# Incident report")
        return httpx.Response(200, json=next(statuses))

    monkeypatch.setattr(OperatorClient, "request", request)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    monkeypatch.setattr("commander.cli.time.sleep", lambda _: None)
    operator = OperatorClient()
    try:
        follow_incident(operator, "fixture-id")
    finally:
        operator.close()
    sent = [body for method, _, body in requests if method == "POST"]
    assert len(sent) == 1 and sent[0] is not None and sent[0]["decision"] == decision
