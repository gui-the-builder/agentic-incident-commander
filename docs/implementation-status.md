# Implementation status

Every task in `specs/001-local-incident-commander/tasks.md` is implemented except the
optional web UI (T117) and the live Kubernetes scenario run (T144), and every acceptance
criterion in `acceptance.md` is satisfied with the evidence recorded there. This file records
evidence and open verification, not a replacement scope. Real-model reliability targets are
reported honestly in `docs/evaluation-results.md`; the 30-run benchmark has not been rerun
since the latest fixes.

## Implemented

- Python package and lockfile; Ruff, strict mypy, pytest, Alembic migrations, and CI configuration.
- Persistent domain records, model/token audits, typed read tools, deterministic action policy, plan-bound approval, and resumable LangGraph workflow.
- Real PostgreSQL checkout requests, bounded connection pool, job consumer, continuous traffic, three YAML fault fixtures, reset/seed API, and separate operator/control tokens.
- Concrete Prometheus and read-only SQL adapters, constrained flag/restart/scale operations, and investigation database permissions with row-level filtering.
- Commander API, durable polling runtime, cross-platform CLI, interactive approval demo, lifecycle Make targets, and real-model JSON/CSV evaluation runner.
- Compose stack, provisioned Grafana dashboard, Prometheus scrapes, structured PostgreSQL log search, and OpenTelemetry HTTP/DB export.
- Shared JSON logging for Python containers, including Uvicorn and MCP stderr; structured operational fields and one-record exception traces are covered by tests.
- Optional MCP read/action stdio servers, runtime client adapters, separate credential environments, unchanged approval/journal enforcement, and transport documentation in `docs/mcp.md`.
- Authenticated evidence export, versioned citation-based evaluation rubric, repeated-call/iteration metrics, historical read-only evidence export, and non-destructive offline rescoring.
- Authenticated full remediation-history endpoint and evaluation export, plan-linked verification events, and postmortems that distinguish successful, pending, and rejected action attempts.
- Claimed-mechanism diagnosis gate with service-specific citation checks, persisted missing-evidence feedback, contradiction handling, and bounded reinvestigation. Unconfirmed diagnoses produce factual-only reports. See `docs/diagnosis-gate.md`.
- Optional Ollama container overlay and model-pull command; model readiness checks from the agent network and host/container lifecycle documentation in `docs/local-development.md`.
- Kubernetes: kind configuration, scoped RBAC, workload/restart/scale adapters, direct/MCP runtime selection, token rotation, generated deployment manifests, dedicated-kubeconfig lifecycle CLI, and contract tests. The zero-worker-replica operator fixture, diagnostic gate, and missing-baseline recovery checks are implemented. Deployment, scoped credentials, fault injection, approval, real mutation, and recovery were all validated live on a kind cluster; see `docs/kubernetes.md`.

## Verified evidence

- The full deterministic suite passes 156 tests; seven opt-in live checks are skipped in the default run and all seven pass against the running Compose lab. Lint/format checks pass, and strict type checks pass across 35 source modules. The wheel and source distribution build, and inspection confirms no generated credential from `.env` appears in either.
- All 34 generated Kubernetes workload/RBAC objects pass offline validation against the official v1.37 OpenAPI schema. This proves field/type compatibility only; cluster admission, authorization, startup, and recovery are unverified.
- The evaluation CLI now has an explicit Kubernetes backend, shared incident audit/report export, and rubric 2.1 zero-replica evidence checks. Targeted tests cover port isolation, mismatched scenarios, explicit approval, failure evidence, and audit preservation. The Kubernetes live evaluation has since run and passed; see `docs/kubernetes.md`.
- The latest evaluation/rubric checks pass 28 tests, and targeted policy/action/Kubernetes checks pass 26 tests. Backend risk overrides now explicitly reject high-risk actions and mutations labeled read-only, preventing silent classification as low risk.
- Fourteen MCP tests pass, covering all six read schemas/results, real framed protocol requests, independent stdio processes, a subprocess database read, invalid arguments/outputs, approval enforcement, retry audits, and successful/uncertain action replay prevention.
- Seven live Compose checks passed: three scenarios and resets, PostgreSQL credential boundaries, Prometheus/Grafana provisioning, actual supervised API restart, and PostgreSQL advisory-lock exclusion.
- Docker startup/migrations succeeded. The wheel/source distribution built; inspection found no local credentials, virtualenv, or runtime artifacts in the source archive.
- Updated wheel/source archives in `artifacts/builds/20260908-feedback/` build successfully. Inspection confirms the new Kubernetes, diagnosis, and MCP modules are packaged, the nested Kubernetes fixture is in the source archive, and credentials/runtime artifacts remain excluded.
- A real `gemma4:12b` bad-flag incident (`b0184426-9122-4cd7-81e0-db9b7f67c594`) diagnosed the failure, paused for approval, executed the approved rollback, verified recovery, and generated a report. Its audit is under `artifacts/first-live-run/`.
- The first three-scenario model smoke batch completed: all three recovered and passed evidence/approval checks; diagnosis scored 2/3. The pool diagnosis statement was insufficiently specific even though remediation correctly described pool exhaustion. Original scores remain unchanged in `artifacts/evals/smoke-20260908/`.

The observed Ollama sampler rejected bounded Pydantic schemas. The adapter now supplies a compatible structural grammar and still enforces the complete contract through Pydantic. The refined pool trial (`06b0ab51-6606-4a6a-b3f9-5f3f51798c0b`) passed diagnosis/evidence/recovery/approval checks in 198.0 seconds. Its recorded prompt usage was 51,450 tokens, compared with 141,534 in the initial pool trial. Both audits remain available; neither single trial establishes a reliability rate.

The original ten-trial-per-scenario batch completed all 30 runs and its process
exited successfully. Read-only inspection found no remaining runnable incidents.
Original artifacts remain under `artifacts/evals/matrix-20260908/`; complete
citation-based rescoring is in `artifacts/evals/matrix-20260908-v21/`. It reports
23/30 diagnosis, 4/30 evidence, 28/30 recovery, and 30/30 approval-policy passes.
See `docs/evaluation-results.md`. The reliability target remains unproven.

The first four matrix trials include one vague diagnosis and one failed hypothesis generation caused by repeatedly malformed evidence UUIDs (`f1e82693-d08b-4ebf-acd2-47aa15bf6a1f`). Original results remain unchanged. The rebuilt stack now includes field-specific validation feedback, exact-reference validation, the diagnosis gate, and MCP transport. These fixes were not present during the baseline; fresh model validation is in progress.

Inspection of flag trial `d70a4fd2-92b3-4867-a9e5-2c1e7b902432` found well-formed
but nonexistent evidence UUIDs. Source now validates hypothesis citations against
the prompt's exact evidence set within the same bounded model retry loop. Tests
cover correction of supporting/contradicting references and repeated failures
with retained token/error audits. Real Gemma improvement remains unverified.

The recovery-loop audit found that reasoning did not explicitly receive prior
remediation outcomes. Model context now reconstructs plan-bound action status,
expected checks, and observed verification from durable records. Tests verify
restart persistence, incident isolation, uncertain actions, and fresh approval
for a replacement plan after failed recovery.

## This session's fixes and live validation

Three defects were found by running the lab, fixed, and revalidated live.

1. **Log search missed structured error names.** `query_logs` matched only the message column
   while the demo API stores the failure name in `attributes.error_type`, so every
   `PoolTimeout` search returned nothing. With both fields searched, trial `1dac59c9` became
   the first pool run to pass the citation rubric with no missing checks.
2. **Correct diagnosis, ineffective action.** That same trial diagnosed pool exhaustion and
   then proposed `scale_demo_worker`, which cannot relieve the demo-api pool; it escalated
   after exhausting its budget. Action contracts now publish factual descriptions of what each
   control does, and the remediation prompt requires an action that counteracts the diagnosed
   mechanism. Trial `ccf4fe6a` then chose `set_demo_feature_flag(slow_db=false)` and passed
   every check, resolving in 194.0 seconds using 43,791 prompt tokens against 1,049,366.
3. **A rejected diagnosis killed the incident.** On the Kubernetes zero-replica fault every
   worker signal is unavailable, the model selected a hypothesis with no valid citations,
   `validate_diagnosis` raised, and the run ended `FAILED` after two iterations. That
   rejection is now a recoverable reasoning error that returns to bounded investigation.
   Trial `abd3ad74` then resolved the incident on the real cluster.

A flaky test was also fixed: `tests/test_investigation_failures.py` applied a 10 ms
`query_metrics` timeout to every fault, so unrelated metric reads timed out on a loaded
machine. The tight timeout now applies only to the fault that exercises it.

The deterministic suite is 156 passing tests with seven opt-in live checks skipped; lint,
format, and strict type checks pass across 35 source modules.

## Remaining work and verification

After the baseline ended, the updated Compose build and health/model checks
passed, as did all seven live integration checks. Local `.env` now selects MCP.
The isolated Gemma flag trial completed through `artifacts/verify_mcp_restart.py`
with output under `artifacts/evals/mcp-gate-restart-20260908/`. It passed diagnosis,
cited evidence, recovery, and approval checks, including approval while the runtime
was stopped and execution after restart. This is a single success, not a reliability rate.

The subsequent pool trial under `artifacts/evals/mcp-gate-pool-20260908/` escalated
after eight investigation iterations without executing remediation. Its repeated
`PoolTimeout` searches returned no entries because `contains` searched messages
only, while the error name was stored in `attributes.error_type`. The read adapter
now searches both fields with literal substring matching. That fix was validated live:
trial `1dac59c9` became the first pool run to pass the citation rubric with no missing
checks. Revalidate any live tool handle in `artifacts/active-evaluation.json` before
further fixture mutation.

The local Kubernetes milestone is complete and validated live; see `docs/kubernetes.md`.
The remaining gaps are measurement and optional scope, not unimplemented features.

1. Rerun the 30-run matrix. The recorded baseline predates every fix above, and the four
   post-fix trials are single runs. No reliability rate has been established for the current
   build, and the documented benchmark remains the old, failing one.
2. Run the two remaining Compose scenarios live through MCP. Only the pool scenario has been
   revalidated since the fixes; the flag scenario was validated earlier through the MCP
   restart path and the worker scenario has not been rerun.
3. CI configuration exists but has not been exercised by a remote GitHub run.
4. Container-mode Ollama inference is unverified. Host connectivity, model presence, and both
   Compose configurations validate, and the pinned image manifest exists.
5. The optional minimal web UI (T117) was not built. The CLI and API docs cover the operator
   flow, which satisfies the Phase 9 exit criterion.

## Local environment

Docker Desktop is running. Host Ollama has `gemma4:12b` installed (Ollama executable is outside PATH). Make is unavailable in this Windows shell; equivalent `uv run python -m commander.cli ...` commands work. OneDrive requires copy mode for dependency installation.
