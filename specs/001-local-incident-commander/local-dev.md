# Local Development Guide

This project is designed to run primarily on one developer machine.

No cloud account is required.

Implementation note: use [the current local development guide](../../docs/local-development.md)
for executable commands. The implemented driver is `postgresql+psycopg`, the
default stack includes observability, and the optional Ollama container uses
the `llm` overlay selected by `OLLAMA_RUNTIME=container`. The profile examples
below describe the original design recommendations.

---

## 1. Prerequisites

Recommended:

- Linux, macOS, or WSL2
- Docker Engine / Docker Desktop
- Docker Compose v2
- Python 3.12+
- `uv` or Poetry
- Make
- Git
- Ollama

Optional later:

- kind or k3d
- kubectl

---

## 2. Local resource expectations

A comfortable target:

- 16 GB RAM
- 4+ CPU cores
- ~15 GB free disk

If RAM is limited:

- run Ollama on the host rather than inside Compose;
- use a smaller quantized model;
- disable Grafana/Loki when running unit tests;
- run only one demo-api replica;
- use lightweight log storage.

---

## 3. Environment variables

Example:

```dotenv
APP_ENV=local

DATABASE_URL=postgresql+asyncpg://commander:commander@postgres:5432/commander

OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=gemma4:12b

PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100

AUTO_APPROVE_LOW_RISK=true
ALLOW_MEDIUM_RISK_ACTIONS=true
MAX_INVESTIGATION_ITERATIONS=8
```

On Linux, host access may require:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

---

## 4. Make targets

Target developer workflow:

```bash
make bootstrap
make up
make health
make seed
make scenario SCENARIO=checkout-db-pool-exhaustion
make incident SCENARIO=checkout-db-pool-exhaustion
make logs
make reset
make test
make eval
make down
```

---

## 5. Compose profiles

Recommended profiles:

```text
core
observability
llm
```

Examples:

```bash
docker compose --profile core up -d
docker compose --profile core --profile observability up -d
```

Allow Ollama to run separately on the host.

---

## 6. Local networking

Keep service-to-service traffic inside the Compose network.

Expose only what developers need, for example:

```text
8000 Commander API
8001 Demo API
3000 Grafana
9090 Prometheus
3100 Loki
```

PostgreSQL does not need to be public unless developer tooling requires it.

---

## 7. Database strategy

Use two logical credential sets:

### Investigation credential

Read-only access to named operational views/queries.

### Application/action credential

Used only by internal services that require mutations.

The LLM-facing database tool receives named query identifiers, never arbitrary SQL.

---

## 8. Ollama workflow

Install/pull the selected model manually.

Keep the model configurable so contributors can use different hardware.

Recommended behavior:

```bash
ollama serve
ollama pull <chosen-model>
```

Then:

```bash
make up
```

The project should detect and clearly report when Ollama is unavailable.

---

## 9. Testing layers

### Fast unit tests

No Docker/LLM required.

Use:

- fake model;
- fake tools;
- in-memory or test DB where appropriate.

### Integration tests

Run:

- PostgreSQL;
- demo services;
- scenario controller.

### Agent evals

Run with actual Ollama and full observability stack.

Do not make real-model evals part of every CI push.

---

## 10. Kubernetes later

When the Compose MVP is complete:

```bash
kind create cluster --name incident-lab
```

or use k3d.

The Kubernetes profile should reuse the same:

- incident domain;
- LangGraph workflow;
- model adapter;
- risk policy;
- tool contracts.

Only infrastructure-specific tool implementations should change.

---

## 11. Cost model

The default project should have effectively zero infrastructure cost.

Potential costs are optional:

- electricity/local compute;
- optional remote LLM API usage;
- optional future cloud deployment.

Do not design MVP features that require paid services.
