# Contracts

This file defines the initial API, agent-tool, and scenario contracts.

---

## 1. Commander HTTP API

### POST `/api/v1/incidents`

Create an incident.

Request:

```json
{
  "title": "Checkout latency high",
  "description": "p95 > 2s for 5 minutes",
  "service": "demo-api",
  "severity": "SEV2"
}
```

Response:

```json
{
  "id": "uuid",
  "status": "OPEN",
  "current_state": "RECEIVED"
}
```

---

### GET `/api/v1/incidents/{incident_id}`

Returns incident summary.

---

### GET `/api/v1/incidents/{incident_id}/timeline`

Returns ordered events:

```json
[
  {
    "timestamp": "...",
    "type": "tool_call",
    "summary": "Queried request latency",
    "data": {}
  }
]
```

---

### GET `/api/v1/incidents/{incident_id}/hypotheses`

Returns current hypothesis set.

---

### POST `/api/v1/incidents/{incident_id}/approval`

Request:

```json
{
  "decision": "APPROVED",
  "actor": "local-operator",
  "comment": "Proceed with service restart"
}
```

---

### GET `/api/v1/incidents/{incident_id}/postmortem`

Returns Markdown or JSON containing Markdown.

---

## 2. Scenario API

### POST `/api/v1/scenarios/{scenario_id}/activate`

Activates deterministic incident.

### POST `/api/v1/scenarios/reset`

Restores healthy baseline.

### GET `/api/v1/scenarios`

Lists available scenarios.

Important: scenario root-cause metadata MUST NOT be exposed to the agent tool set.

---

## 3. Tool contract conventions

Each tool defines:

```text
name
description
input schema
output schema
risk
timeout_seconds
max_retries
read_only
```

Tool output envelope:

```json
{
  "ok": true,
  "observed_at": "2026-01-01T12:00:00Z",
  "data": {},
  "error": null
}
```

---

## 4. Read tools

### `get_service_health`

Input:

```json
{
  "service": "demo-api"
}
```

Output:

```json
{
  "status": "degraded",
  "instances": [
    {
      "id": "demo-api-1",
      "healthy": true,
      "ready": true
    }
  ]
}
```

---

### `query_metrics`

Input:

```json
{
  "metric": "http_request_duration_seconds",
  "service": "demo-api",
  "window_minutes": 10,
  "aggregation": "p95"
}
```

Output:

```json
{
  "metric": "http_request_duration_seconds",
  "value": 2.31,
  "unit": "seconds",
  "window_minutes": 10
}
```

The implementation SHOULD use an allowlisted metric/query abstraction instead of letting the LLM submit arbitrary PromQL in MVP.

---

### `query_logs`

Input:

```json
{
  "service": "demo-api",
  "level": "ERROR",
  "contains": "connection",
  "window_minutes": 10,
  "limit": 100
}
```

Output:

```json
{
  "entries": [
    {
      "timestamp": "...",
      "level": "ERROR",
      "message": "database connection acquisition timed out",
      "attributes": {}
    }
  ]
}
```

---

### `get_recent_deployments`

For Compose MVP this may return application build/config change history from a local table rather than querying a real deployment platform.

Input:

```json
{
  "service": "demo-api",
  "window_minutes": 120
}
```

---

### `get_service_config`

Returns allowlisted operational config, excluding secrets.

---

### `query_database_readonly`

Do NOT accept raw SQL from the model.

Use named queries.

Input:

```json
{
  "query_name": "pending_jobs_count",
  "parameters": {}
}
```

---

## 5. Action tools

### `restart_demo_worker`

Risk: `LOW`

Input:

```json
{
  "reason": "worker heartbeat stale and backlog increasing"
}
```

Implementation options:

- controlled process supervisor;
- scenario-controller action endpoint;
- container restart through a narrowly scoped controller.

Do not expose Docker APIs to the agent directly.

---

### `restart_demo_api`

Risk: `MEDIUM`

Approval required by default.

---

### `set_demo_feature_flag`

Risk: `MEDIUM`

Input:

```json
{
  "flag": "new_checkout_path",
  "value": false,
  "reason": "5xx spike correlates with enabled flag"
}
```

Allowed flags MUST be explicit.

---

### `scale_demo_worker`

Risk: `MEDIUM`

For Compose MVP, "scale" may change worker concurrency in a controlled service.

For Kubernetes extension, it can map to deployment replica count.

---

## 6. Structured model contracts

### Hypothesis generation

```json
{
  "hypotheses": [
    {
      "statement": "Database pool exhaustion is causing request queuing",
      "confidence": 0.55,
      "supporting_evidence_ids": ["..."],
      "contradicting_evidence_ids": []
    }
  ]
}
```

### Investigation choice

```json
{
  "tool": "query_logs",
  "arguments": {
    "service": "demo-api",
    "level": "ERROR",
    "contains": "connection",
    "window_minutes": 10,
    "limit": 50
  },
  "rationale": "Need direct evidence of DB acquisition failures"
}
```

The runtime MUST ignore tool names not on the allowlist.

### Remediation proposal

```json
{
  "summary": "Disable the bad checkout feature flag",
  "actions": [
    {
      "action_type": "set_demo_feature_flag",
      "arguments": {
        "flag": "new_checkout_path",
        "value": false,
        "reason": "..."
      }
    }
  ],
  "verification_checks": [
    {
      "signal": "http_5xx_rate",
      "target": "<= 0.02",
      "window_minutes": 3
    }
  ]
}
```
