"""Render an inspectable local Kubernetes manifest; secrets stay under ignored artifacts."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

NAMESPACE = "incident-lab"
IMAGE = "incident-commander:kubernetes"


def resource(kind: str, name: str, **fields: Any) -> dict[str, Any]:
    version = {"Deployment": "apps/v1", "Job": "batch/v1"}.get(kind, "v1")
    return {
        "apiVersion": version,
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE},
        **fields,
    }


def secret_env(name: str, key: str) -> dict[str, Any]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": "lab-config", "key": key}}}


def deployment(
    name: str,
    image: str,
    port: int | None,
    env: list[dict[str, Any]],
    command: list[str] | None = None,
    ready: str = "/health",
    app: bool = True,
    database_key: str | None = None,
) -> dict[str, Any]:
    labels = {"app.kubernetes.io/name": name}
    container: dict[str, Any] = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": env,
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    if command:
        container["command"] = command
    if port:
        container["ports"] = [{"containerPort": port}]
        container["readinessProbe"] = {
            "httpGet": {"path": ready, "port": port},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
        }
        container["livenessProbe"] = {
            "httpGet": {"path": "/health" if app else ready, "port": port},
            "periodSeconds": 10,
            "timeoutSeconds": 3,
            "initialDelaySeconds": 30,
        }
    pod: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "containers": [container],
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
    }
    if app:
        pod["securityContext"].update(runAsNonRoot=True, runAsUser=10001, fsGroup=10001)
    if database_key:
        pod["initContainers"] = [
            {
                "name": "wait-schema",
                "image": IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "python",
                    "-m",
                    "commander.startup",
                    "demo_config" if database_key == "demo-url" else "incidents",
                ],
                "env": [secret_env("DATABASE_URL", database_key)],
                "securityContext": container["securityContext"],
            }
        ]
    return resource(
        "Deployment",
        name,
        spec={
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    )


def service(name: str, port: int, node_port: int | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "selector": {"app.kubernetes.io/name": name},
        "ports": [{"port": port, "targetPort": port}],
    }
    if node_port:
        spec["type"] = "NodePort"
        spec["ports"][0]["nodePort"] = node_port
    return resource("Service", name, spec=spec)


def mount(
    obj: dict[str, Any], name: str, volume: dict[str, Any], path: str, sub_path: str | None = None
) -> None:
    pod = obj["spec"]["template"]["spec"]
    pod.setdefault("volumes", []).append({"name": name, **volume})
    item = {"name": name, "mountPath": path, "readOnly": True}
    if sub_path:
        item["subPath"] = sub_path
    pod["containers"][0].setdefault("volumeMounts", []).append(item)


def render(config: dict[str, str], read_token: str, action_token: str) -> list[dict[str, Any]]:
    required = (
        "POSTGRES_PASSWORD",
        "DEMO_PASSWORD",
        "INVESTIGATION_PASSWORD",
        "CONTROL_TOKEN",
        "OPERATOR_TOKEN",
    )
    if any(not config.get(key) for key in required) or not read_token or not action_token:
        raise ValueError("Bootstrap credentials and both Kubernetes tokens are required")
    secrets = {key: config[key] for key in required}
    for role, key in (
        ("commander", "POSTGRES_PASSWORD"),
        ("demo", "DEMO_PASSWORD"),
        ("investigation", "INVESTIGATION_PASSWORD"),
    ):
        secrets[role + "-url"] = (
            f"postgresql+psycopg://{role}:{quote(config[key], safe='')}@postgres:5432/commander"
        )
    objects = [
        resource("Secret", "lab-config", type="Opaque", stringData=secrets),
        resource(
            "Secret",
            "kubernetes-client-tokens",
            type="Opaque",
            stringData={"read-token": read_token, "action-token": action_token},
        ),
        resource(
            "ConfigMap",
            "postgres-init",
            data={"10-roles.sh": Path("infra/compose/init-roles.sh").read_text(encoding="utf-8")},
        ),
        resource(
            "PersistentVolumeClaim",
            "postgres-data",
            spec={"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "2Gi"}}},
        ),
    ]
    postgres = deployment(
        "postgres",
        "postgres:17-alpine",
        None,
        [
            {"name": "POSTGRES_USER", "value": "commander"},
            {"name": "POSTGRES_DB", "value": "commander"},
            secret_env("POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
            secret_env("DEMO_PASSWORD", "DEMO_PASSWORD"),
            secret_env("INVESTIGATION_PASSWORD", "INVESTIGATION_PASSWORD"),
        ],
        app=False,
    )
    pod = postgres["spec"]["template"]["spec"]
    pod["containers"][0].pop(
        "securityContext"
    )  # Official entrypoint initializes ownership before dropping uid.
    mount(
        postgres, "roles", {"configMap": {"name": "postgres-init"}}, "/docker-entrypoint-initdb.d"
    )
    pod["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}})
    pod["containers"][0]["volumeMounts"].append(
        {"name": "data", "mountPath": "/var/lib/postgresql/data"}
    )
    pod["containers"][0]["readinessProbe"] = {
        "exec": {"command": ["pg_isready", "-U", "commander", "-d", "commander"]}
    }
    objects.extend([postgres, service("postgres", 5432)])
    objects.append(
        resource(
            "Job",
            "commander-migrate",
            spec={
                "backoffLimit": 10,
                "template": {
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "migrate",
                                "image": IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["python", "-m", "commander.provision"],
                                "env": [secret_env("DATABASE_URL", "commander-url")],
                            }
                        ],
                    }
                },
            },
        )
    )
    demo_env = [
        secret_env("DEMO_DATABASE_URL", "demo-url"),
        secret_env("CONTROL_TOKEN", "CONTROL_TOKEN"),
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector:4318"},
    ]
    app_specs = [
        (
            "commander-api",
            "api",
            8000,
            30080,
            [
                secret_env("DATABASE_URL", "commander-url"),
                secret_env("OPERATOR_TOKEN", "OPERATOR_TOKEN"),
            ],
            "commander-url",
            "/ready",
        ),
        ("demo-api", "demo", 8001, 30081, demo_env, "demo-url", "/ready"),
        ("demo-worker", "worker", 8002, None, demo_env, "demo-url", "/ready"),
        (
            "scenario-controller",
            "scenarios",
            8003,
            30083,
            [*demo_env, secret_env("OPERATOR_TOKEN", "OPERATOR_TOKEN")],
            "demo-url",
            "/health",
        ),
        (
            "agent-runtime",
            "runtime",
            8004,
            None,
            [
                secret_env("DATABASE_URL", "commander-url"),
                secret_env("INVESTIGATION_DATABASE_URL", "investigation-url"),
                secret_env("CONTROL_TOKEN", "CONTROL_TOKEN"),
                {"name": "RUNTIME_BACKEND", "value": "kubernetes"},
                {"name": "TOOL_TRANSPORT", "value": config.get("TOOL_TRANSPORT", "direct")},
                {
                    "name": "OLLAMA_BASE_URL",
                    "value": config.get(
                        "KUBERNETES_OLLAMA_BASE_URL", "http://host.docker.internal:11434"
                    ),
                },
                {"name": "OLLAMA_MODEL", "value": config.get("OLLAMA_MODEL", "gemma4:12b")},
                {
                    "name": "MODEL_TIMEOUT_SECONDS",
                    "value": config.get("MODEL_TIMEOUT_SECONDS", "120"),
                },
                {
                    "name": "AUTO_APPROVE_LOW_RISK",
                    "value": config.get("AUTO_APPROVE_LOW_RISK", "true"),
                },
                {
                    "name": "ALLOW_MEDIUM_RISK_ACTIONS",
                    "value": config.get("ALLOW_MEDIUM_RISK_ACTIONS", "true"),
                },
                {
                    "name": "MAX_INVESTIGATION_ITERATIONS",
                    "value": config.get("MAX_INVESTIGATION_ITERATIONS", "8"),
                },
            ],
            "commander-url",
            "/health",
        ),
    ]
    for name, module, port, node_port, env, db_key, ready in app_specs:
        obj = deployment(
            name,
            IMAGE,
            port,
            env,
            [
                "uvicorn",
                f"commander.{module}:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ],
            ready=ready,
            database_key=db_key,
        )
        if name == "agent-runtime":
            mount(
                obj,
                "kubernetes",
                {
                    "projected": {
                        "defaultMode": 0o440,
                        "sources": [
                            {"secret": {"name": "kubernetes-client-tokens"}},
                            {
                                "configMap": {
                                    "name": "kube-root-ca.crt",
                                    "items": [{"key": "ca.crt", "path": "ca.crt"}],
                                }
                            },
                        ],
                    }
                },
                "/var/run/commander-kubernetes",
            )
        objects.extend([obj, service(name, port, node_port)])
    objects.append(
        deployment(
            "traffic",
            IMAGE,
            None,
            [
                {"name": "DEMO_API_URL", "value": "http://demo-api:8001"},
                {"name": "TRAFFIC_CONCURRENCY", "value": "12"},
            ],
            ["python", "-m", "commander.traffic"],
        )
    )
    for name, image, obs_port, obs_node_port, source, target, command, ready in [
        (
            "prometheus",
            "prom/prometheus:v3.5.0",
            9090,
            30090,
            "infra/observability/prometheus.yaml",
            "/etc/prometheus/prometheus.yml",
            None,
            "/-/healthy",
        ),
        (
            "otel-collector",
            "otel/opentelemetry-collector-contrib:0.132.0",
            None,
            None,
            "infra/observability/otel.yaml",
            "/etc/otelcol/config.yaml",
            ["/otelcol-contrib", "--config=/etc/otelcol/config.yaml"],
            "/health",
        ),
    ]:
        objects.append(
            resource("ConfigMap", name, data={"config": Path(source).read_text(encoding="utf-8")})
        )
        obj = deployment(name, image, obs_port, [], command, app=False, ready=ready)
        mount(obj, "config", {"configMap": {"name": name}}, target, "config")
        objects.extend([obj, service(name, obs_port or 4318, obs_node_port)])
    grafana_data = {
        p.name: p.read_text(encoding="utf-8")
        for p in Path("infra/observability/grafana").rglob("*")
        if p.is_file()
    }
    objects.append(resource("ConfigMap", "grafana", data=grafana_data))
    grafana = deployment(
        "grafana",
        "grafana/grafana:12.1.1",
        3000,
        [
            {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "true"},
            {"name": "GF_AUTH_ANONYMOUS_ORG_ROLE", "value": "Viewer"},
            {"name": "GF_AUTH_DISABLE_LOGIN_FORM", "value": "true"},
        ],
        app=False,
        ready="/api/health",
    )
    for filename, directory in (
        ("prometheus.yaml", "datasources"),
        ("provider.yaml", "dashboards"),
        ("incident-lab.json", "dashboards"),
    ):
        mount(
            grafana,
            filename.replace(".", "-"),
            {"configMap": {"name": "grafana"}},
            f"/etc/grafana/provisioning/{directory}/{filename}",
            filename,
        )
    objects.extend([grafana, service("grafana", 3000, 30300)])
    if config.get("KUBERNETES_BUILD_ID"):
        for obj in objects:
            if (
                obj["kind"] == "Deployment"
                and obj["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
            ):
                obj["spec"]["template"]["metadata"]["annotations"] = {
                    "incident-commander/build-id": config["KUBERNETES_BUILD_ID"]
                }
    return objects


def write_manifest(path: Path, config: dict[str, str], read_token: str, action_token: str) -> None:
    if "artifacts" not in path.resolve().relative_to(Path.cwd().resolve()).parts:
        raise ValueError("Secret-bearing manifests must be written under repository artifacts")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump_all(render(config, read_token, action_token), sort_keys=False),
        encoding="utf-8",
    )
