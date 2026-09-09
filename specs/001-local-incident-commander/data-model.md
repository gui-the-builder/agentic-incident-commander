# Data Model

Use PostgreSQL for application persistence.

The exact ORM is implementation-defined; SQLAlchemy 2.x is recommended.

---

## 1. Incident

```text
Incident
- id: UUID
- title: string
- description: text?
- service: string
- severity: enum
- status: enum
- current_state: enum
- created_at: timestamp
- updated_at: timestamp
- started_at: timestamp?
- resolved_at: timestamp?
- scenario_id: string?        # demo metadata, never included in agent context
```

Status values:

```text
OPEN
WAITING_FOR_APPROVAL
RESOLVED
ESCALATED
FAILED
```

---

## 2. StateTransition

```text
StateTransition
- id: UUID
- incident_id: UUID
- from_state: string?
- to_state: string
- reason: text?
- metadata: jsonb
- created_at: timestamp
```

Every graph transition MUST create a record.

---

## 3. Evidence

```text
Evidence
- id: UUID
- incident_id: UUID
- tool_call_id: UUID?
- evidence_type: string
- source: string
- summary: text
- payload: jsonb
- observed_at: timestamp
- relevance: float?
- created_at: timestamp
```

Evidence payload SHOULD remain structured.

---

## 4. Hypothesis

```text
Hypothesis
- id: UUID
- incident_id: UUID
- statement: text
- confidence: float
- status: enum
- supporting_evidence_ids: UUID[]
- contradicting_evidence_ids: UUID[]
- created_at: timestamp
- updated_at: timestamp
```

Status:

```text
ACTIVE
REJECTED
SELECTED
```

---

## 5. ToolCall

```text
ToolCall
- id: UUID
- incident_id: UUID
- tool_name: string
- arguments: jsonb
- result: jsonb?
- status: enum
- error: text?
- started_at: timestamp
- completed_at: timestamp?
- duration_ms: integer?
- attempt: integer
```

Status:

```text
STARTED
SUCCEEDED
FAILED
TIMED_OUT
BLOCKED
```

---

## 6. RemediationPlan

```text
RemediationPlan
- id: UUID
- incident_id: UUID
- diagnosis_hypothesis_id: UUID
- summary: text
- risk: enum
- approval_required: boolean
- verification_plan: jsonb
- created_at: timestamp
```

---

## 7. RemediationAction

```text
RemediationAction
- id: UUID
- remediation_plan_id: UUID
- action_type: string
- arguments: jsonb
- order_index: integer
- risk: enum
- status: enum
- result: jsonb?
- created_at: timestamp
- executed_at: timestamp?
```

---

## 8. Approval

```text
Approval
- id: UUID
- incident_id: UUID
- remediation_plan_id: UUID
- decision: enum
- actor: string
- comment: text?
- created_at: timestamp
```

Decision:

```text
APPROVED
REJECTED
```

---

## 9. VerificationResult

```text
VerificationResult
- id: UUID
- incident_id: UUID
- remediation_plan_id: UUID
- success: boolean
- checks: jsonb
- summary: text
- created_at: timestamp
```

Example checks:

```json
[
  {
    "name": "5xx_rate",
    "before": 0.31,
    "after": 0.01,
    "operator": "<=",
    "target": 0.02,
    "passed": true
  }
]
```

---

## 10. Postmortem

```text
Postmortem
- id: UUID
- incident_id: UUID
- markdown: text
- generated_at: timestamp
- model_metadata: jsonb?
```

---

## 11. Agent graph state

Keep graph state compact.

Suggested structure:

```python
class IncidentGraphState(TypedDict):
    incident_id: str
    alert: Alert
    evidence_ids: list[str]
    hypothesis_ids: list[str]
    selected_hypothesis_id: str | None
    planned_remediation_id: str | None
    pending_approval: bool
    investigation_iterations: int
    last_tool_result: dict | None
    terminal_reason: str | None
```

Persist large payloads in tables; keep IDs in graph state.
