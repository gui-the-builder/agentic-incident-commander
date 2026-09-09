# Acceptance Criteria / Definition of Done

The portfolio MVP is complete when all items below are satisfied.

Each item is ticked with the evidence that satisfies it. Deterministic tests refer to
`tests/`; live checks refer to `tests/integration/test_live_stack.py` (opt-in with
`RUN_LIVE_TESTS=1`) and to saved audits under ignored `artifacts/`. Real-model reliability
numbers are reported in `docs/evaluation-results.md`; none are fabricated.

---

## Local execution

Evidence: `README.md` quickstart, `docs/local-development.md`, `Makefile`, and
`packages/commander/cli.py` (`bootstrap`, `up`, `health`). Compose is the only required
runtime; Ollama is the default and only required model provider (`config.py`).

- [x] A new developer can start the environment locally from documented commands.
- [x] No cloud account is required.
- [x] Ollama is supported as the default LLM provider.
- [x] Docker Compose is the default runtime.

## Incident workflow

Evidence: `POST /api/v1/incidents` persists before the runtime polls (`api.py`, `storage.py`);
every graph node records a `state_transition` (`workflow.py`, `test_workflow.py`); evidence is
created only by the audited `ReadGateway` (`tools.py`, `test_tools.py`); hypotheses are rows
(`HypothesisRow`); `validate_diagnosis` and the diagnosis gate reject uncited causes
(`policy.py`, `diagnosis.py`, `test_diagnosis.py`); `MAX_INVESTIGATION_ITERATIONS` bounds the
loop (`test_workflow.py::test_low_confidence_investigation_stops_at_iteration_limit`).

- [x] Incident creation produces a persisted incident.
- [x] LangGraph transitions are explicit and auditable.
- [x] Investigation gathers evidence through typed tools.
- [x] Hypotheses are persisted.
- [x] Final diagnosis references evidence.
- [x] Investigation has a bounded maximum loop count.

## Safety

Evidence: no shell/exec tool exists; unknown tool names are blocked and audited
(`test_tools.py`, `test_policy.py::test_unknown_actions_cannot_enter_a_plan`); the database
tool accepts only named queries and a read-only role (`test_policy.py::test_database_tool_does_not_accept_sql_or_parameters`,
live `test_postgres_investigation_credentials_are_constrained`); `ACTION_RISKS` is a
deterministic registry (`policy.py`); medium-risk actions pause for plan-bound approval and
rejected plans never execute (`test_policy.py`, `test_actions.py`, `test_mcp.py`,
`test_workflow.py::test_rejection_generates_report_without_execution`).

- [x] The model cannot execute arbitrary shell commands.
- [x] The model cannot execute arbitrary SQL writes.
- [x] Every action is allowlisted.
- [x] Every action has deterministic risk classification.
- [x] Medium-risk action cannot execute before approval.
- [x] Rejected remediation remains unexecuted.
- [x] Safety rules have automated tests.

## Scenarios

Evidence: `scenarios/*.yaml`, `packages/commander/scenarios.py`,
`tests/test_demo.py::test_scenarios_can_be_repeated_and_reset`, and the live parametrized
`test_live_scenario_causes_measured_failure_and_resets`. Hidden ground truth:
`Incident.agent_alert()`, the row-filtered investigation role, and
`test_read_adapter_never_reveals_worker_stall_flag`.

- [x] DB pool exhaustion scenario works.
- [x] Bad feature flag scenario works.
- [x] Stalled worker scenario works.
- [x] Each scenario resets to a healthy baseline.
- [x] Agent does not receive hidden root-cause metadata.

## Verification

Evidence: `VerificationCheck` is part of every `RemediationPlan` before execution
(`test_policy.py::test_plan_requires_verification_before_execution`); checks query Prometheus
signals through the same read tool; failed verification returns to investigation and never
resolves (`test_workflow.py::test_failed_recovery_never_resolves_and_iteration_budget_is_global`);
success records `RESOLVED` with a `VerificationRow`.

- [x] Remediation includes predeclared verification checks.
- [x] Verification uses observable signals.
- [x] Failed verification does not mark incident resolved.
- [x] Successful verification records a resolved incident.

## Observability

Evidence: `infra/observability/prometheus.yaml` scrapes demo-api, demo-worker, and the agent
runtime; the provisioned dashboard has six panels (live
`test_prometheus_scrapes_services_and_grafana_is_provisioned`); `query_logs` searches the
structured `demo_logs` table by message and `error_type`; `ToolCallRow` records arguments,
status, duration, result, and error (`test_tools.py`); `GET /api/v1/incidents/{id}/timeline`
and the CLI `timeline` command expose the run.

- [x] Prometheus collects demo-system metrics.
- [x] Grafana displays useful incident signals.
- [x] Structured logs are queryable.
- [x] Tool calls record duration, arguments, status, and result/error.
- [x] Agent run timeline is inspectable.

## Postmortem

Evidence: `Workflow.postmortem` writes deterministic sections (summary, impact, timeline,
root cause, evidence, remediation, verification, follow-ups, confidence) plus optional model
synthesis; served as Markdown by `/postmortem` and `cli.py report`
(`test_workflow.py`, `test_api.py`).

- [x] Completed incident produces Markdown postmortem.
- [x] Postmortem includes impact, timeline, cause, evidence, remediation, verification, and follow-ups.

## Testing

Evidence: `tests/test_workflow.py` (transitions, pause/resume, lock), `ScriptedModel` and
fake handlers throughout `tests/`, live scenario injector tests, approval tests in
`tests/test_policy.py`/`tests/test_actions.py`, retry/timeout tests in `tests/test_tools.py`
and `tests/test_investigation_failures.py`, and `make eval` → `packages/commander/evals.py`
writing `results.json`/`results.csv`.

- [x] Unit tests cover graph transitions.
- [x] Unit tests use fake model/tool implementations.
- [x] Integration tests cover all scenario injectors.
- [x] Approval gating is tested.
- [x] Retry/timeout paths are tested.
- [x] `make eval` produces structured evaluation results.

## Portfolio quality

Evidence: `README.md` contains the Mermaid architecture diagram, the quickstart, the
reproducible incident flow, the safety section, and measured evaluation tables that link to
`docs/evaluation-results.md`. Every number cited there comes from saved audits under
`artifacts/evals/`; failed runs are reported alongside passes.

- [x] README contains architecture diagram.
- [x] README contains one-command quickstart.
- [x] README includes a recorded or reproducible demo flow.
- [x] README explains safety decisions.
- [x] README shows real evaluation results.
- [x] No benchmark numbers are fabricated.
- [x] Repository can be understood by an interviewer in under 10 minutes.
