"""Operate only the dedicated local kind cluster and repository-owned kubeconfig."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

from commander.kube_manifest import IMAGE, write_manifest

KUBECONFIG = Path("artifacts/kubernetes/kubeconfig")
MANIFEST = Path("artifacts/kubernetes/deployment.yaml")


def run(*command: str, capture: bool = False, timeout: int = 900) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=capture, timeout=timeout)
    return result.stdout.strip() if capture else ""


def kind_executable() -> str:
    installed = shutil.which("kind")
    local = Path("artifacts/tools/kind-v0.33.0.exe")
    if local.exists():
        return str(local.resolve())
    if installed:
        return installed
    raise RuntimeError("Install kind first; see docs/kubernetes.md")


def kubectl(*arguments: str, capture: bool = False) -> str:
    if not KUBECONFIG.exists():
        raise RuntimeError("The dedicated lab kubeconfig is missing; run kube_cli up first")
    config = yaml.safe_load(KUBECONFIG.read_text(encoding="utf-8"))
    context = next(c["context"] for c in config["contexts"] if c["name"] == "kind-incident-lab")
    cluster = next(c["cluster"] for c in config["clusters"] if c["name"] == context["cluster"])
    endpoint = urlsplit(cluster["server"])
    if endpoint.scheme != "https" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("The lab kubeconfig must target a loopback HTTPS kind API")
    local = Path("artifacts/tools/kubectl-v1.37.0.exe")
    executable = str(local.resolve()) if local.exists() else "kubectl"
    return run(
        executable,
        "--kubeconfig",
        str(KUBECONFIG.resolve()),
        "--context",
        "kind-incident-lab",
        "--namespace",
        "incident-lab",
        *arguments,
        capture=capture,
    )


def render_deployment(build_id: str | None = None) -> None:
    config = {key: value for key, value in dotenv_values(".env").items() if value is not None}
    if build_id:
        config["KUBERNETES_BUILD_ID"] = build_id
    read = kubectl("create", "token", "commander-reads", "--duration=24h", capture=True)
    action = kubectl("create", "token", "commander-actions", "--duration=24h", capture=True)
    write_manifest(MANIFEST, config, read, action)
    print(f"Rendered {MANIFEST}; it contains local credentials and must remain untracked.")


def up() -> None:
    kind = kind_executable()
    KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
    clusters = run(kind, "get", "clusters", capture=True).splitlines()
    if "incident-lab" not in clusters:
        run(
            kind,
            "create",
            "cluster",
            "--name",
            "incident-lab",
            "--config",
            "infra/kubernetes/kind.yaml",
            "--kubeconfig",
            str(KUBECONFIG.resolve()),
            "--wait",
            "5m",
        )
    else:
        run(
            kind,
            "export",
            "kubeconfig",
            "--name",
            "incident-lab",
            "--kubeconfig",
            str(KUBECONFIG.resolve()),
        )
    run("docker", "build", "-t", IMAGE, "-f", "infra/compose/Dockerfile", ".")
    run(kind, "load", "docker-image", IMAGE, "--name", "incident-lab")
    kubectl("apply", "-f", "infra/kubernetes/rbac.yaml")
    render_deployment(run("docker", "image", "inspect", "--format", "{{.Id}}", IMAGE, capture=True))
    # A completed migration Job is disposable; database/PVC resources are preserved.
    kubectl("delete", "job", "commander-migrate", "--ignore-not-found")
    kubectl("apply", "-f", str(MANIFEST))
    kubectl("wait", "--for=condition=complete", "job/commander-migrate", "--timeout=300s")
    for service in (
        "commander-api",
        "demo-api",
        "demo-worker",
        "scenario-controller",
        "agent-runtime",
    ):
        kubectl("rollout", "status", f"deployment/{service}", "--timeout=300s")
    print("Local Kubernetes services are ready. Commander: http://localhost:18000")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "up",
            "status",
            "refresh-tokens",
            "model-check",
            "fault",
            "reset",
            "incident",
            "demo",
        ],
    )
    parser.add_argument("incident_id", nargs="?")
    args = parser.parse_args()
    if args.command in {"fault", "reset", "incident", "demo"}:
        from commander import kube_scenario
        from commander.cli import OperatorClient, follow_incident

        if args.command == "demo" and not args.incident_id:
            parser.error("demo requires the Kubernetes incident UUID")
        operator = OperatorClient("http://localhost:18000", "http://localhost:18003")
        try:
            if args.command == "fault":
                kube_scenario.activate(operator)
                print("Worker zero-replica fault activated.")
            elif args.command == "reset":
                kube_scenario.reset(operator)
                print("Kubernetes worker baseline restored.")
            elif args.command == "incident":
                print(json.dumps(kube_scenario.create_incident(operator), indent=2))
            else:
                follow_incident(operator, args.incident_id)
        finally:
            operator.close()
        return
    if args.command == "up":
        up()
    elif args.command == "status":
        kubectl("get", "deployments,pods,jobs")
    elif args.command == "model-check":
        kubectl("exec", "deployment/agent-runtime", "--", "python", "-m", "commander.model_health")
    else:
        render_deployment()
        # Apply only the token secret: refreshing credentials must not rerun migrations or rollouts.
        objects = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
        secret = next(
            obj
            for obj in objects
            if obj["kind"] == "Secret" and obj["metadata"]["name"] == "kubernetes-client-tokens"
        )
        token_manifest = MANIFEST.with_name("tokens.yaml")
        token_manifest.write_text(yaml.safe_dump(secret), encoding="utf-8")
        kubectl("apply", "-f", str(token_manifest))


if __name__ == "__main__":
    main()
