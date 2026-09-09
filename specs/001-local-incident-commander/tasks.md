# Implementation Tasks

Tasks are ordered to produce a usable system incrementally.

Checkboxes are intended to be copied directly into GitHub issues or a project board.

---

## Phase 0 — Repository and local stack

- [x] T001 Create repository structure from root `README.md`.
- [x] T002 Add Python project configuration and dependency management.
- [x] T003 Add `Makefile` with `bootstrap`, `up`, `down`, `reset`, `test`, `eval`.
- [x] T004 Add `.env.example`.
- [x] T005 Create Docker Compose network and PostgreSQL service.
- [x] T006 Add Prometheus.
- [x] T007 Add Grafana with provisioned datasource.
- [x] T008 Add Loki or selected local log backend.
- [x] T009 Add OpenTelemetry collector.
- [x] T010 Document Ollama host/container configuration.
- [x] T011 Add CI for lint, typecheck, unit tests.

**Exit criteria:** `make up` starts healthy base infrastructure.

Evidence: `pyproject.toml`, `uv.lock`, `Makefile`, `.env.example`, `infra/compose/compose.yaml`
(PostgreSQL, Prometheus, Grafana, OpenTelemetry collector), `infra/observability/`,
`docs/local-development.md` (host/container Ollama), and `.github/workflows/ci.yml`.
Structured PostgreSQL log search is the selected local log backend (T008) instead of Loki.
The live check `test_prometheus_scrapes_services_and_grafana_is_provisioned` passes against
the running stack. CI has not yet been exercised by a remote GitHub run.

---

## Phase 1 — Demo production system

- [x] T020 Build `demo-api` FastAPI service.
- [x] T021 Add `/health`, `/ready`, `/metrics`.
- [x] T022 Implement checkout endpoint backed by PostgreSQL.
- [x] T023 Add structured JSON logging.
- [x] T024 Instrument HTTP and DB calls.
- [x] T025 Build `demo-worker`.
- [x] T026 Add jobs table and worker processing loop.
- [x] T027 Add worker metrics.
- [x] T028 Build lightweight traffic/load generator.
- [x] T029 Add baseline Grafana dashboard.

**Exit criteria:** healthy system emits realistic metrics and logs.

Evidence: `packages/commander/demo.py` (checkout API with a real bounded pool),
`packages/commander/worker.py` (supervised consumer), `packages/commander/lab_storage.py`
(jobs table), `packages/commander/traffic.py`, `packages/commander/telemetry.py` (HTTP/DB
instrumentation), `packages/commander/logging_config.py`, and the six-panel dashboard in
`infra/observability/grafana/dashboards/incident-lab.json`. Tests: `tests/test_demo.py`,
`tests/test_logging.py`.

---

## Phase 2 — Scenario controller

- [x] T030 Create scenario definition schema.
- [x] T031 Implement global reset to healthy state.
- [x] T032 Implement DB pool exhaustion scenario.
- [x] T033 Implement bad feature flag scenario.
- [x] T034 Implement stalled worker scenario.
- [x] T035 Add scenario API/CLI.
- [x] T036 Add deterministic integration tests for each scenario.
- [x] T037 Ensure scenario ground truth is inaccessible to agent tools.

**Exit criteria:** each scenario can be activated/reset repeatedly.

Evidence: `packages/commander/scenarios.py` (schema, controller, API), the three fixtures in
`scenarios/`, `tests/test_demo.py::test_scenarios_can_be_repeated_and_reset`, and the live
`tests/integration/test_live_stack.py::test_live_scenario_causes_measured_failure_and_resets`.
Ground truth is hidden by `Incident.agent_alert()`, the row-filtered investigation role
(`infra/compose/init-roles.sh`), and `test_read_adapter_never_reveals_worker_stall_flag`.

---

## Phase 3 — Domain and persistence

- [x] T040 Create incident domain models.
- [x] T041 Create database migrations.
- [x] T042 Implement incident repository.
- [x] T043 Implement evidence repository.
- [x] T044 Implement hypothesis repository.
- [x] T045 Implement tool-call audit repository.
- [x] T046 Implement remediation/approval repositories.
- [x] T047 Implement state-transition audit.

**Exit criteria:** domain state persists through process restart.

Evidence: `packages/commander/domain.py`, `migrations/versions/`, and the `Repository` in
`packages/commander/storage.py` (incidents, evidence, hypotheses, tool/model calls, plans,
approvals, verification, transitions). Tests: `tests/test_storage.py`,
`tests/test_migrations.py`, `tests/test_workflow.py::test_approval_pause_and_new_workflow_resume`.

---

## Phase 4 — Read-only tools

- [x] T050 Define common tool protocol and result envelope.
- [x] T051 Implement `get_service_health`.
- [x] T052 Implement allowlisted `query_metrics`.
- [x] T053 Implement `query_logs`.
- [x] T054 Implement `get_recent_deployments`.
- [x] T055 Implement `get_service_config`.
- [x] T056 Implement named `query_database_readonly`.
- [x] T057 Add timeouts/retries.
- [x] T058 Add audit wrapper around every tool.
- [x] T059 Unit-test schemas and failure handling.

**Exit criteria:** an ordinary Python test can investigate all scenarios without an LLM.

Evidence: `packages/commander/tools.py` (`ToolDefinition`, `ToolResult` envelope, audited
`ReadGateway` with timeouts/retries) and `packages/commander/adapters.py` (six read handlers,
named database queries only). Tests: `tests/test_tools.py`, `tests/test_demo.py`,
`tests/test_investigation_failures.py`.

---

## Phase 5 — Model layer

- [x] T060 Define provider-neutral `ReasoningModel`.
- [x] T061 Implement Ollama adapter.
- [x] T062 Add structured Pydantic outputs.
- [x] T063 Add malformed-output retry.
- [x] T064 Add fake/scripted model for tests.
- [x] T065 Add prompt templates for hypothesis generation.
- [x] T066 Add prompt template for investigation choice.
- [x] T067 Add prompt template for diagnosis.
- [x] T068 Add prompt template for remediation.
- [x] T069 Add prompt template for postmortem.

**Exit criteria:** fake model tests are deterministic; Ollama can produce valid structured output.

Evidence: `packages/commander/models.py` (`ReasoningModel`, `OllamaModel`, bounded retry with
field-specific feedback, `ScriptedModel`, task prompts). Tests: `tests/test_models.py`.
Real structured output is recorded in every audit under `artifacts/evals/`.

---

## Phase 6 — LangGraph investigation workflow

- [x] T070 Define graph state.
- [x] T071 Implement alert normalization.
- [x] T072 Implement baseline context node.
- [x] T073 Implement hypothesis generation.
- [x] T074 Implement investigation-tool selection.
- [x] T075 Implement tool execution node.
- [x] T076 Implement hypothesis update.
- [x] T077 Implement diagnosis gate.
- [x] T078 Add maximum iteration guard.
- [x] T079 Add escalation path.
- [x] T080 Persist every state transition.

**Exit criteria:** agent can correctly diagnose at least one scenario with read-only tools.

Evidence: `packages/commander/workflow.py` (explicit LangGraph nodes with repository-backed
checkpoints and a global iteration budget) and `packages/commander/diagnosis.py` (cited-evidence
gate, see `docs/diagnosis-gate.md`). Tests: `tests/test_workflow.py`, `tests/test_diagnosis.py`.
Real-model diagnoses are documented in `docs/evaluation-results.md`.

---

## Phase 7 — Remediation and approval

- [x] T090 Implement action risk registry.
- [x] T091 Implement deterministic policy engine.
- [x] T092 Implement remediation-plan validation.
- [x] T093 Implement `restart_demo_worker`.
- [x] T094 Implement `restart_demo_api`.
- [x] T095 Implement `set_demo_feature_flag`.
- [x] T096 Implement `scale_demo_worker`.
- [x] T097 Implement approval API.
- [x] T098 Implement LangGraph pause/resume around approval.
- [x] T099 Add security tests proving policy cannot be bypassed.

**Exit criteria:** medium-risk action cannot execute without explicit approval.

Evidence: `packages/commander/policy.py` (deterministic risk registry and plan-bound approval),
`packages/commander/actions.py` (executes only the persisted current plan), constrained
`/internal/*` control routes in `demo.py`/`worker.py`, and the approval endpoint in `api.py`.
Tests: `tests/test_policy.py`, `tests/test_actions.py`,
`tests/test_workflow.py::test_action_gateway_cannot_bypass_pause`, `tests/test_mcp.py`.

---

## Phase 8 — Verification and postmortem

- [x] T100 Define verification-check schema.
- [x] T101 Capture before-remediation signals.
- [x] T102 Execute verification queries after remediation.
- [x] T103 Implement successful resolution path.
- [x] T104 Implement failed-verification return-to-investigation path.
- [x] T105 Implement postmortem generator.
- [x] T106 Export postmortem as Markdown.

**Exit criteria:** one scenario completes end to end from alert to postmortem.

Evidence: `VerificationCheck` in `domain.py`, pre-remediation signal capture and consecutive
verification in `workflow.py`, the failed-verification return to investigation, and the
Markdown postmortem served by `/api/v1/incidents/{id}/postmortem`. Tests:
`tests/test_workflow.py`, `tests/test_worker_verification.py`. Live end-to-end audits:
`artifacts/first-live-run/` and `artifacts/evals/`.

---

## Phase 9 — Commander API and operator UX

- [x] T110 Implement incident creation endpoint.
- [x] T111 Implement incident details endpoint.
- [x] T112 Implement timeline endpoint.
- [x] T113 Implement hypotheses endpoint.
- [x] T114 Implement approval/rejection endpoint.
- [x] T115 Implement postmortem endpoint.
- [x] T116 Build CLI commands for demo.
- [ ] T117 Optional: minimal web UI.

**Exit criteria:** reviewer can operate the system without touching internals.

Evidence: `packages/commander/api.py` (create, details, timeline, hypotheses, evidence,
approval, remediations, postmortem) and `packages/commander/cli.py` (lifecycle, scenario,
incident, status, timeline, approve/reject, report, interactive `demo`). Tests: `tests/test_api.py`,
`tests/test_cli.py`. The optional web UI (T117) was not built; the CLI and API docs cover the
operator flow.

---

## Phase 10 — Evaluation

- [x] T120 Build scenario evaluation runner.
- [x] T121 Add diagnosis scorer.
- [x] T122 Add recovery scorer.
- [x] T123 Add safety-invariant checks.
- [x] T124 Add tool-efficiency metrics.
- [x] T125 Add JSON/CSV report output.
- [x] T126 Run 10 trials per scenario.
- [x] T127 Document real results.

**Exit criteria:** `make eval` produces a repeatable report.

Evidence: `packages/commander/evals.py` (runner, diagnosis/recovery/safety scorers, efficiency
metrics, JSON/CSV output), `packages/commander/evaluation_quality.py` (citation rubric), and
`packages/commander/rescore.py`. Tests: `tests/test_evals.py`, `tests/test_evaluation_quality.py`.
Completed 30-run batches and their scores are documented in `docs/evaluation-results.md`.

---

## Phase 11 — MCP packaging

Do this after the non-MCP implementation works.

- [x] T130 Wrap read tools in MCP server(s).
- [x] T131 Wrap action tools with separate permission boundary.
- [x] T132 Add MCP client adapter.
- [x] T133 Add contract tests comparing direct and MCP tool behavior.
- [x] T134 Document why MCP was added and what boundary it provides.

Evidence: `packages/commander/mcp_transport.py`, runtime transport selection,
14 passing tests in `tests/test_mcp.py`, and `docs/mcp.md`. A real Gemma flag
incident passed through MCP with approval while the runtime was stopped, then
recovered after restart. Evidence: `artifacts/evals/mcp-gate-restart-20260908/`.
A live Kubernetes incident later ran end to end through the same MCP transport. Multi-scenario reliability remains unmeasured; see `docs/evaluation-results.md`.

---

## Phase 12 — Local Kubernetes extension

- [x] T140 Add kind or k3d cluster config (`infra/kubernetes/kind.yaml`).
- [x] T141 Deploy demo system locally.
- [x] T142 Add Kubernetes read tools.
- [x] T143 Add safe restart/scale tools.
- [x] T144 Add one Kubernetes failure scenario.
- [x] T145 Keep Docker Compose as default quickstart.

Evidence: `packages/commander/kube_cli.py` deployed the full stack to a local kind
cluster; all eleven pods reached Running and the migration Job completed. Live RBAC
impersonation matches the declared boundary (`artifacts/kubernetes/rbac-live-check.txt`),
and the read token performed a real workload read but received HTTP 403 on a scale patch.
`packages/commander/kubernetes.py` provides the read tool (T142) and the constrained
restart/scale actions (T143); `tests/test_kubernetes.py` and `tests/test_kube_deployment.py`
cover them. Compose remains the default in `README.md` and in `Settings.runtime_backend`
(T145). T144's zero-replica fault ran end to end on the cluster: incident
`abd3ad74-f630-4ce6-9ed3-ccadaec9cefe` diagnosed the zero-replica worker from cited workload
and database evidence, paused for approval, executed a real scale patch, and verified
recovery. It passed diagnosis, cited evidence, recovery, and approval policy. See
`docs/kubernetes.md` and `artifacts/evals/kubernetes-zero-replicas-20260908-v2/`.

---

# MVP cut line

If time is limited, complete through Phase 10 before MCP and Kubernetes.

That version is already a strong portfolio project.
