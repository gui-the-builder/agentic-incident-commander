import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from mcp import Client

from commander.backends import FileTokenAuth, backend_policy, read_definitions
from commander.config import Settings
from commander.domain import Contract, ServiceQuery
from commander.kube_manifest import render, write_manifest
from commander.mcp_transport import MCPReads, read_server, server_parameters
from tests.test_policy import proposal


def config() -> dict[str, str]:
    return {
        key: "test-secret"
        for key in (
            "POSTGRES_PASSWORD",
            "DEMO_PASSWORD",
            "INVESTIGATION_PASSWORD",
            "CONTROL_TOKEN",
            "OPERATOR_TOKEN",
        )
    }


def test_manifest_references_credentials_without_exposing_them_in_pod_specs() -> None:
    objects = render(
        {**config(), "ALLOW_MEDIUM_RISK_ACTIONS": "false", "KUBERNETES_BUILD_ID": "sha256:fixture"},
        "read-token-value",
        "action-token-value",
    )
    deployments = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Deployment"}
    assert {
        "postgres",
        "demo-api",
        "demo-worker",
        "agent-runtime",
        "commander-api",
        "scenario-controller",
        "traffic",
        "prometheus",
        "grafana",
        "otel-collector",
    } == set(deployments)
    assert all(o["metadata"]["namespace"] == "incident-lab" for o in objects)
    for name, deployment in deployments.items():
        pod = deployment["spec"]["template"]["spec"]
        assert not pod["automountServiceAccountToken"]
        assert "test-secret" not in json.dumps(pod)
        if name not in {"commander-api", "scenario-controller"}:
            assert '"name": "OPERATOR_TOKEN"' not in json.dumps(pod)
    runtime = deployments["agent-runtime"]["spec"]["template"]["spec"]
    assert {e["name"]: e.get("value") for e in runtime["containers"][0]["env"]}[
        "ALLOW_MEDIUM_RISK_ACTIONS"
    ] == "false"
    assert (
        deployments["agent-runtime"]["spec"]["template"]["metadata"]["annotations"][
            "incident-commander/build-id"
        ]
        == "sha256:fixture"
    )
    assert (
        runtime["volumes"][0]["projected"]["sources"][0]["secret"]["name"]
        == "kubernetes-client-tokens"
    )
    ports = {o["spec"]["ports"][0].get("nodePort") for o in objects if o["kind"] == "Service"}
    assert ports - {None} == {30080, 30081, 30083, 30090, 30300}


def test_secret_manifests_cannot_be_written_outside_ignored_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_manifest(tmp_path / "deployment.yaml", config(), "read", "action")


def test_rotated_kubernetes_tokens_are_read_for_each_request(tmp_path: Path) -> None:
    token = tmp_path / "token"
    headers = []
    with httpx.Client(
        auth=FileTokenAuth(str(token)),
        transport=httpx.MockTransport(
            lambda r: headers.append(r.headers["Authorization"]) or httpx.Response(200)
        ),
    ) as client:
        token.write_text("first")
        client.get("https://kubernetes/api")
        token.write_text("second")
        client.get("https://kubernetes/api")
    assert headers == ["Bearer first", "Bearer second"]


async def test_kubernetes_catalog_survives_mcp_and_backend_risk_is_consistent() -> None:
    settings = Settings(_env_file=None, runtime_backend="kubernetes")
    definitions = read_definitions(settings)
    assert "get_kubernetes_workload" in definitions
    assert "get_kubernetes_workload" not in read_definitions(Settings(_env_file=None))
    assert backend_policy(settings).classify(proposal("restart_demo_worker"))[1]

    async def workload(query: Contract) -> dict[str, Any]:
        return {
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
        }

    async with Client(
        read_server({"get_kubernetes_workload": workload}, definitions), mode="legacy"
    ) as client:
        result = await MCPReads(client, definitions).handlers()["get_kubernetes_workload"](
            ServiceQuery(service="demo-worker")
        )
        assert result["desired_replicas"] == 0
    read_env = server_parameters(settings, "reads").env or {}
    action_env = server_parameters(settings, "actions").env or {}
    assert "KUBERNETES_ACTION_TOKEN_FILE" not in read_env
    assert "KUBERNETES_READ_TOKEN_FILE" not in action_env


def test_kubectl_rejects_nonlocal_context_before_any_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from commander import kube_cli

    path = tmp_path / "kubeconfig"
    path.write_text(
        yaml.safe_dump(
            {
                "contexts": [{"name": "kind-incident-lab", "context": {"cluster": "lab"}}],
                "clusters": [{"name": "lab", "cluster": {"server": "https://production.example"}}],
            }
        )
    )
    monkeypatch.setattr(kube_cli, "KUBECONFIG", path)
    with pytest.raises(RuntimeError, match="loopback"):
        kube_cli.kubectl("apply", "-f", "ignored")
