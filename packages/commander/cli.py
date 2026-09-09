"""Cross-platform operator commands. Secrets stay in .env and request headers."""

import argparse
import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


def bootstrap() -> None:
    path = Path(".env")
    existing = dotenv_values(path) if path.exists() else {}
    defaults = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "DEMO_PASSWORD": secrets.token_hex(24),
        "INVESTIGATION_PASSWORD": secrets.token_hex(24),
        "CONTROL_TOKEN": secrets.token_hex(32),
        "OPERATOR_TOKEN": secrets.token_hex(32),
        "OLLAMA_MODEL": "gemma4:12b",
        "OLLAMA_BASE_URL": "http://host.docker.internal:11434",
        "OLLAMA_RUNTIME": "host",
    }
    missing = {key: value for key, value in defaults.items() if not existing.get(key)}
    if missing:
        with path.open("a", encoding="utf-8") as file:
            file.write("\n" + "\n".join(f"{key}={value}" for key, value in missing.items()) + "\n")
    print("Local configuration is ready in .env. Existing settings were preserved.")


def compose(*arguments: str) -> None:
    config = dotenv_values(".env")
    mode = config.get("OLLAMA_RUNTIME", "host")
    if mode not in {"host", "container"}:
        raise ValueError("OLLAMA_RUNTIME must be host or container")
    command = ["docker", "compose", "--env-file", ".env", "-f", "infra/compose/compose.yaml"]
    if mode == "container":
        command.extend(["-f", "infra/compose/compose.ollama.yaml", "--profile", "llm"])
    subprocess.run(
        [*command, *arguments],
        check=True,
    )


class OperatorClient:
    def __init__(
        self, api_url: str = "http://localhost:8000", scenario_url: str = "http://localhost:8003"
    ):
        config = dotenv_values(".env")
        self.api_url = api_url.rstrip("/")
        self.scenario_url = scenario_url.rstrip("/")
        self.client = httpx.Client(
            timeout=20,
            headers={
                "Authorization": f"Bearer {config.get('OPERATOR_TOKEN', '')}",
            },
        )

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, scenario: bool = False
    ) -> httpx.Response:
        base = self.scenario_url if scenario else self.api_url
        response = self.client.request(method, base + path, json=body)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self.client.close()


def follow_incident(operator: OperatorClient, incident_id: str) -> None:
    path = f"/api/v1/incidents/{incident_id}"
    previous = None
    while True:
        incident = operator.request("GET", path).json()
        if incident["current_state"] != previous:
            print(f"{incident['current_state']} — {incident['status']}", flush=True)
            previous = incident["current_state"]
        if incident["status"] == "WAITING_FOR_APPROVAL" and not incident.get("approval"):
            plan = incident["remediation_plan"]
            print(json.dumps(plan, indent=2), flush=True)
            approved = input("Approve this remediation plan? [y/N] ").strip().lower() == "y"
            operator.request(
                "POST",
                path + "/approval",
                {
                    "decision": "APPROVED" if approved else "REJECTED",
                    "actor": "local-operator",
                    "comment": "Reviewed in the interactive demo.",
                },
            )
        if incident["current_state"] == "POSTMORTEM_GENERATED":
            print(operator.request("GET", path + "/postmortem").text)
            return
        time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Operate the local incident lab")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "bootstrap",
        "up",
        "down",
        "health",
        "seed",
        "reset",
        "logs",
        "migrate",
        "model-pull",
    ):
        commands.add_parser(name)
    for name in ("scenario", "incident"):
        sub = commands.add_parser(name)
        sub.add_argument("scenario_id", default="checkout-db-pool-exhaustion", nargs="?")
    for name in ("status", "timeline", "report", "approve", "reject"):
        sub = commands.add_parser(name)
        sub.add_argument("incident_id")
        if name in {"approve", "reject"}:
            sub.add_argument("--actor", default="local-operator")
            sub.add_argument("--comment", default="")
    demo = commands.add_parser("demo")
    demo.add_argument("incident_id", nargs="?")
    arguments = parser.parse_args()
    if arguments.command == "bootstrap":
        bootstrap()
        return
    if arguments.command == "model-pull":
        config = dotenv_values(".env")
        if config.get("OLLAMA_RUNTIME", "host") != "container":
            parser.error(
                "For host Ollama, use ollama pull gemma4:12b; container mode uses model-pull"
            )
        compose("up", "-d", "--wait", "ollama")
        compose(
            "exec", "-T", "ollama", "ollama", "pull", config.get("OLLAMA_MODEL") or "gemma4:12b"
        )
        return
    if arguments.command in {"up", "down", "logs", "migrate"}:
        match arguments.command:
            case "up":
                compose("up", "-d", "--build", "--wait", "--wait-timeout", "300")
            case "down":
                compose("down")
            case "logs":
                compose("logs", "--tail", "100")
            case "migrate":
                compose("run", "--rm", "migrate")
        return
    operator = OperatorClient()
    try:
        if arguments.command == "demo":
            last_incident = Path("artifacts/last-incident.txt")
            incident_id = arguments.incident_id
            if not incident_id and last_incident.exists():
                incident_id = last_incident.read_text(encoding="utf-8").strip()
            if not incident_id:
                parser.error("Create an incident first, or pass its UUID to demo")
            follow_incident(operator, incident_id)
            return
        if arguments.command == "health":
            for name, url in (
                ("Commander", operator.api_url),
                ("Scenarios", operator.scenario_url),
                ("Demo API", "http://localhost:8001"),
                ("Prometheus", "http://localhost:9090"),
            ):
                path = "/-/healthy" if name == "Prometheus" else "/health"
                response = operator.client.get(url + path)
                response.raise_for_status()
                print(f"{name}: healthy")
            compose("exec", "-T", "agent-runtime", "python", "-m", "commander.model_health")
            return
        if arguments.command in {"reset", "seed"}:
            response = operator.request(
                "POST", f"/api/v1/scenarios/{arguments.command}", scenario=True
            )
        elif arguments.command == "scenario":
            response = operator.request(
                "POST", f"/api/v1/scenarios/{arguments.scenario_id}/activate", scenario=True
            )
        elif arguments.command == "incident":
            scenarios = operator.request("GET", "/api/v1/scenarios", scenario=True).json()
            selected = next((s for s in scenarios if s["id"] == arguments.scenario_id), None)
            if selected is None:
                parser.error("Unknown scenario identifier")
            # Only alert fields cross into the incident system, never expected cause or injection.
            response = operator.request(
                "POST",
                "/api/v1/incidents",
                {
                    "title": selected["title"],
                    "service": selected["service"],
                    "severity": selected["severity"],
                    "description": "Investigate observed service degradation.",
                },
            )
        else:
            path = f"/api/v1/incidents/{arguments.incident_id}"
            if arguments.command in {"approve", "reject"}:
                response = operator.request(
                    "POST",
                    path + "/approval",
                    {
                        "decision": "APPROVED" if arguments.command == "approve" else "REJECTED",
                        "actor": arguments.actor,
                        "comment": arguments.comment,
                    },
                )
            else:
                suffix = {"status": "", "timeline": "/timeline", "report": "/postmortem"}
                response = operator.request("GET", path + suffix[arguments.command])
        if arguments.command == "report":
            print(response.text)
        else:
            print(json.dumps(response.json(), indent=2))
        if arguments.command == "incident":
            last_incident = Path("artifacts/last-incident.txt")
            last_incident.parent.mkdir(exist_ok=True)
            last_incident.write_text(response.json()["id"], encoding="utf-8")
    finally:
        operator.close()


if __name__ == "__main__":
    main()
