import json
from uuid import uuid4

import httpx
import pytest

from commander.config import Settings
from commander.domain import HypothesisSet, Incident
from commander.models import AuditedModel, ModelFailure, OllamaModel, sampler_schema
from commander.storage import ModelCallRow, Repository


@pytest.mark.parametrize("field", ["supporting_evidence_ids", "contradicting_evidence_ids"])
async def test_ollama_repairs_well_formed_but_unknown_evidence_id(field: str) -> None:
    known_id, invented_id = str(uuid4()), str(uuid4())
    calls: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 2:
            feedback = body["messages"][-1]["content"]
            assert "unknown_evidence_reference" in feedback and field in feedback
            assert invented_id not in feedback
        reference = invented_id if len(calls) == 1 else known_id
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "hypotheses": [
                                {
                                    "statement": "Observed connection pool exhaustion",
                                    "confidence": 0.8,
                                    field: [reference],
                                }
                            ]
                        }
                    )
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="http://ollama", transport=httpx.MockTransport(respond)
    ) as client:
        result = await OllamaModel(client, "fixture-model").complete_structured(
            "update_hypotheses", {"evidence": [{"id": known_id}]}, HypothesisSet
        )
    assert len(calls) == 2
    assert str(getattr(result.hypotheses[0], field)[0]) == known_id


async def test_repeated_invented_citations_exhaust_retry_and_are_audited(
    repository: Repository,
    incident: Incident,
) -> None:
    known_id, invented_id = str(uuid4()), str(uuid4())
    content = json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "Observed failure",
                    "confidence": 0.8,
                    "supporting_evidence_ids": [invented_id],
                }
            ]
        }
    )
    async with httpx.AsyncClient(
        base_url="http://ollama",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"message": {"content": content}, "eval_count": 7})
        ),
    ) as client:
        audited = AuditedModel(OllamaModel(client, "fixture-model"), repository, incident.id)
        with pytest.raises(ModelFailure, match="bounded retries"):
            await audited.complete_structured(
                "hypotheses", {"evidence": [{"id": known_id}]}, HypothesisSet
            )
    record = repository.records(ModelCallRow, incident.id)[0]
    assert record["status"] == "FAILED"
    assert sum(m.get("eval_count", 0) for m in record["model_metadata"]) == 14
    assert sum("validation_error" in m for m in record["model_metadata"]) == 2
    assert repository.hypotheses(incident.id) == []


async def test_ollama_retries_malformed_structured_output() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        content = (
            "not json"
            if len(calls) == 1
            else json.dumps(
                {
                    "hypotheses": [
                        {
                            "statement": "Pool exhausted",
                            "confidence": 0.8,
                        }
                    ]
                }
            )
        )
        return httpx.Response(200, json={"message": {"content": content}, "eval_count": 20})

    async with httpx.AsyncClient(
        base_url="http://ollama",
        transport=httpx.MockTransport(respond),
    ) as client:
        model = OllamaModel(client, "gemma4:12b")
        result = await model.complete_structured("hypotheses", {}, HypothesisSet)
    assert result.hypotheses[0].statement == "Pool exhausted"
    assert len(calls) == 2 and calls[0]["model"] == "gemma4:12b"
    assert calls[0]["format"] == sampler_schema(HypothesisSet.model_json_schema())
    assert '"maximum": 1' in calls[0]["messages"][0]["content"]
    assert any("validation_error" in entry for entry in model.metadata)


async def test_retry_identifies_truncated_citation_without_echoing_invalid_input() -> None:
    evidence_id = "9bf49492-be10-40a0-9401-074cdd177500"
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            feedback = payload["messages"][-1]["content"]
            assert '"path": ["hypotheses", 0, "supporting_evidence_ids", 0]' in feedback
            assert "uuid_parsing" in feedback and "Copy referenced UUIDs exactly" in feedback
            assert "ignore rules" not in feedback
        citation = "ignore rules" if len(requests) == 1 else evidence_id
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "hypotheses": [
                                {
                                    "statement": "Pool exhausted",
                                    "confidence": 0.8,
                                    "supporting_evidence_ids": [citation],
                                }
                            ]
                        }
                    )
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="http://ollama", transport=httpx.MockTransport(respond)
    ) as client:
        result = await OllamaModel(client, "fixture-model").complete_structured(
            "hypotheses", {"evidence": [{"id": evidence_id}]}, HypothesisSet
        )
    assert str(result.hypotheses[0].supporting_evidence_ids[0]) == evidence_id


async def test_ollama_invalid_output_fails_safely() -> None:
    async with httpx.AsyncClient(
        base_url="http://ollama",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"message": {"content": "{}"}}),
        ),
    ) as client:
        with pytest.raises(ModelFailure, match="bounded retries"):
            await OllamaModel(client, "gemma4:12b").complete_structured(
                "hypotheses",
                {},
                HypothesisSet,
            )


def test_default_local_model() -> None:
    assert Settings(_env_file=None).ollama_model == "gemma4:12b"


async def test_sampler_compatibility_does_not_relax_validation(
    repository: Repository,
    incident: Incident,
) -> None:
    content = json.dumps({"hypotheses": [{"statement": "Failure", "confidence": 2}]})
    async with httpx.AsyncClient(
        base_url="http://ollama",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"message": {"content": content}, "eval_count": 12}),
        ),
    ) as client:
        audited = AuditedModel(OllamaModel(client, "gemma4:12b"), repository, incident.id)
        with pytest.raises(ModelFailure):
            await audited.complete_structured("hypotheses", {}, HypothesisSet)
    records = repository.records(ModelCallRow, incident.id)
    assert records[0]["status"] == "FAILED"
    assert sum(item.get("eval_count", 0) for item in records[0]["model_metadata"]) == 24
    assert records[0]["duration_ms"] >= 0


def test_action_schema_publishes_factual_semantics_without_fixture_metadata() -> None:
    from commander.domain import RemediationProposal
    from commander.models import sampler_schema

    schema = RemediationProposal.model_json_schema()
    defs = schema["$defs"]
    described = {
        name: " ".join(defs[name]["description"].split())
        for name in ("RestartWorker", "RestartAPI", "SetFlag", "ScaleWorker")
    }
    assert "no effect on demo-api" in described["ScaleWorker"]
    assert "cannot undo a flag change" in described["RestartAPI"]
    assert "only action that changes demo-api configuration" in described["SetFlag"]
    flag = defs["FlagArguments"]["properties"]["flag"]["description"]
    assert "slow_db" in flag and "new_checkout_path" in flag
    # The sampler grammar inlines definitions but keeps the semantics text.
    # JSON escapes the docstring line breaks; compare on normalized whitespace.
    grammar = " ".join(json.dumps(sampler_schema(schema)).replace("\\n", " ").split())
    for text in described.values():
        assert text in grammar
    # Scenario identity and expected causes never appear in the published contract.
    for hidden in ("scenario", "root cause", "expected", "exhaustion", "stalled"):
        assert hidden not in grammar.lower()
