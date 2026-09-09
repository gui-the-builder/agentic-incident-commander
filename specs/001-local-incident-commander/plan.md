# Technical Plan

## 1. Architecture overview

```text
┌─────────────────────────────────────────────────────────────────┐
│                         Operator / Demo UI                       │
│                 CLI first, small web UI later                   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Commander API                              │
│ FastAPI: incidents, approvals, timeline, scenario controls      │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Agent Runtime                              │
│ LangGraph state machine                                         │
│                                                                 │
│ Gather → Hypothesize → Investigate → Diagnose → Plan            │
│       → Approval → Execute → Verify → Postmortem                 │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
                ▼                               ▼
┌──────────────────────────┐       ┌──────────────────────────────┐
│     Tool Gateway / MCP   │       │     Model Adapter            │
│ typed operational tools  │       │ Ollama default               │
└───────────────┬──────────┘       │ optional remote provider     │
                │                  └──────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Simulated Production Stack                    │
│ FastAPI services | PostgreSQL | Redis(optional) | workers       │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
                ▼                               ▼
┌──────────────────────────┐       ┌──────────────────────────────┐
│ Observability            │       │ Scenario Controller          │
│ Prometheus, Grafana,     │       │ inject/reset failures        │
│ logs, OpenTelemetry      │       │ deterministic fixtures       │
└──────────────────────────┘       └──────────────────────────────┘
```

---

## 2. Local infrastructure strategy

### Phase 1 — Docker Compose only

Use Docker Compose for the complete MVP.

Services:

```text
commander-api
agent-runtime
postgres
prometheus
grafana
loki
otel-collector
demo-api
demo-worker
scenario-controller
ollama (optional container; host Ollama is also acceptable)
```

Why Docker Compose first:

- fastest path to a finished portfolio project;
- easy for reviewers to run;
- no cloud dependency;
- simpler failure injection;
- enough infrastructure to demonstrate production concepts.

### Phase 2 — Local Kubernetes

Add kind or k3d only after the Compose version is stable.

Use Kubernetes to demonstrate:

- pod health inspection;
- rollout status;
- replica scaling;
- pod restart;
- crash loops;
- resource pressure.

Do not make Kubernetes a prerequisite for the first demo.

---

## 3. Application components

### 3.1 Commander API

**Technology:** FastAPI

Responsibilities:

- create incidents;
- get incident state;
- return incident timeline;
- approve/reject remediation;
- expose generated postmortem;
- trigger/reset scenarios for demo use.

### 3.2 Agent runtime

**Technology:** LangGraph

Responsibilities:

- maintain graph state;
- invoke model;
- select investigation tools;
- persist state transitions;
- perform deterministic validation between states;
- pause on approval;
- resume after approval;
- verify remediation.

Recommended graph nodes:

```text
normalize_alert
gather_baseline_context
generate_hypotheses
choose_investigation_step
execute_read_tool
update_hypotheses
diagnosis_gate
build_remediation_plan
risk_gate
await_approval
execute_action
verify
decide_next_step
generate_postmortem
```

### 3.3 Model adapter

Define an internal interface such as:

```python
class ReasoningModel(Protocol):
    async def complete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
    ) -> T:
        ...
```

Default implementation:

- Ollama

Optional:

- OpenAI-compatible endpoint
- other providers later

Important: orchestration code MUST depend on the adapter, not directly on a vendor SDK.

### 3.4 Tool gateway

Tools SHOULD be ordinary typed Python functions first.

MCP adapters can then expose the same capabilities.

Reason: MCP is valuable portfolio material, but the core agent should remain unit-testable without starting MCP servers.

Suggested layering:

```text
domain tool interface
        ↓
local implementation
        ↓
optional MCP server wrapper
        ↓
agent tool client
```

### 3.5 Scenario controller

Responsibilities:

- activate named scenario;
- reset environment;
- seed known data;
- expose scenario metadata to tests, but NOT to the agent.

The agent must diagnose from symptoms, not from hidden ground truth.

### 3.6 Demo services

Use a small service topology rather than a fake static dataset.

Example:

```text
demo-api
  ├── PostgreSQL
  └── sends jobs → demo-worker

demo-worker
  └── processes jobs and writes status to PostgreSQL
```

Expose:

- `/health`
- `/ready`
- `/checkout`
- `/jobs`
- `/metrics`

---

## 4. Observability design

### Metrics

Expose Prometheus metrics including:

```text
http_requests_total
http_request_duration_seconds
http_5xx_total
db_pool_in_use
db_pool_wait_seconds
worker_jobs_pending
worker_jobs_processed_total
worker_last_success_timestamp
```

### Logs

All services emit structured JSON logs:

```json
{
  "timestamp": "...",
  "level": "ERROR",
  "service": "demo-api",
  "request_id": "...",
  "message": "...",
  "error_type": "...",
  "attributes": {}
}
```

### Tracing

Use OpenTelemetry for:

- inbound HTTP;
- DB calls;
- agent tool calls where practical.

Do not block MVP on perfect distributed tracing.

---

## 5. Persistence

PostgreSQL stores:

- incidents;
- state transitions;
- hypotheses;
- evidence;
- tool calls;
- remediation plans;
- approvals;
- verification results;
- postmortems.

LangGraph checkpoint persistence SHOULD use PostgreSQL if straightforward. Otherwise use a project-specific persistence layer for MVP and add a checkpoint adapter later.

---

## 6. Safety architecture

The LLM never invokes infrastructure directly.

```text
LLM proposes ToolCall
        ↓
schema validation
        ↓
tool allowlist
        ↓
risk policy
        ↓
approval gate if required
        ↓
typed implementation
        ↓
result recorded
```

Forbidden in MVP:

- `exec(command: str)`
- unrestricted SQL write queries;
- host filesystem mutation;
- Docker socket access from the model;
- arbitrary HTTP requests to unknown hosts.

---

## 7. Risk policy

Implement deterministic configuration:

```yaml
actions:
  restart_demo_worker:
    risk: low
    approval_required: false

  restart_demo_api:
    risk: medium
    approval_required: true

  set_demo_feature_flag:
    risk: medium
    approval_required: true

  scale_demo_worker:
    risk: medium
    approval_required: true
```

Tests MUST prove the model cannot bypass this policy.

---

## 8. LLM prompts

Use small, task-specific prompts.

Avoid one giant "you are an SRE" prompt.

Recommended prompt boundaries:

- hypothesis generation;
- next investigation choice;
- hypothesis update;
- diagnosis summary;
- remediation proposal;
- postmortem synthesis.

Require structured outputs with Pydantic models.

---

## 9. Failure handling

### Tool failure

On timeout or retryable error:

1. persist failure;
2. retry according to tool policy;
3. inform graph state;
4. let agent choose alternate evidence if retries are exhausted.

### Model failure

- retry malformed structured output once or twice;
- persist validation error;
- fail safely if structure remains invalid.

### Verification failure

Return to investigation with:

- executed action;
- expected outcome;
- observed outcome.

Set a maximum loop count to prevent infinite investigation.

---

## 10. Security boundaries for local development

Even locally:

- run services as non-root where practical;
- do not mount the Docker socket into the agent;
- keep secrets in `.env`, never prompts;
- use read-only DB credentials for investigation tools;
- use separate constrained credentials for action tools;
- bind internal services to the Compose network where possible.

---

## 11. Milestones

### M0 — Skeleton

- repo structure;
- Compose stack;
- one demo service;
- PostgreSQL;
- Ollama adapter;
- health endpoints.

### M1 — Read-only incident investigation

- incident API;
- LangGraph skeleton;
- metrics/log tools;
- one deterministic scenario;
- evidence persistence;
- diagnosis output.

### M2 — Safe remediation

- remediation plans;
- policy engine;
- approval API;
- one action tool;
- verification loop.

### M3 — Portfolio MVP

- three scenarios;
- Grafana dashboard;
- postmortem generation;
- evaluation harness;
- polished README/demo script.

### M4 — Kubernetes extension

- kind/k3d;
- Kubernetes read/action tools;
- Kubernetes-specific scenario.
