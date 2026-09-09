# Evidence requirements before remediation

Each hypothesis now includes a claimed `mechanism`: `connection_pool_exhaustion`,
`checkout_flag_regression`, `worker_consumer_stall`, `worker_replicas_zero`, or `unknown`. The model chooses
this classification from observations. It does not receive a scenario ID,
injection configuration, or expected root cause. Historical hypotheses without
the field load as `unknown`; they remain readable but cannot become a newly
authorized diagnosis without further investigation.

After validating incident-scoped references and acknowledging failed tool calls,
the workflow checks the selected hypothesis's supporting citations:

- Pool exhaustion requires degraded latency, pool saturation or acquisition wait,
  and connection-timeout logs.
- Checkout flag regression requires elevated errors, the matching exception logs,
  and change history enabling the failing path.
- Worker stall requires backlog, unchanged counter observations at distinct times,
  a stale success timestamp, and a live worker.
- Zero worker replicas requires the latest cited Kubernetes observation: zero
  desired/ready/available replicas, an observed controller generation, and a
  complete empty pod list. The named database read must show more than ten pending
  jobs; absent Prometheus scrapes alone cannot establish this cause.

The requirements are included in model context for every investigation. Checks
use successful tool records for the incident's service. Measurements must be
recent (three minutes); change history may describe a deployment from the prior
two hours. Newer recovery measurements override older failure readings, even when
the model omits the newer measurements from its citations. Initial metric reads
use a one-minute window to observe fresh incidents without excessive dilution by
earlier healthy traffic. Verification retains its predeclared recovery windows.

If a worker's processed counter is unavailable before remediation, verification
retains that missing value in its audit. The first successful recovery scrape
establishes a separate `progress_baseline`; it does not pass the progress check.
Subsequent observations must exceed that baseline for the required consecutive
checks. Flat, decreasing, and unavailable counters cannot confirm recovery.

When evidence is incomplete, the workflow persists `diagnosis_feedback` and a
transition explaining the missing observations. It returns to investigation;
it creates no remediation plan and cannot request or execute an action. The model
can gather the missing signal or correct its citations. Every further round
consumes the existing durable investigation budget; exhaustion escalates the
incident. Unknown mechanisms also follow that bounded path.

## Rejected selections are recoverable

`validate_diagnosis` rejects a selection that names an unpersisted hypothesis, cites evidence
outside the incident, supplies no supporting citations, or fails to acknowledge a failed tool
call. That rejection is a reasoning error, not an infrastructure failure, so the workflow
records it as `diagnosis_feedback`, persists a `Diagnosis rejected` transition, and returns to
bounded investigation. It never creates a plan, requests approval, or executes an action.

This matters when a service is entirely unreachable. On the Kubernetes zero-replica fault the
worker has no pods, so its health read and all three worker metrics fail, and a model that
selects a hypothesis with no valid citations previously ended the incident in `FAILED` after
two iterations. It now keeps its remaining budget to reach the workload and database reads.
A model that never repairs its citations still exhausts the budget and escalates;
`tests/test_workflow.py` covers both the recovery and the escalation path.

## Action semantics in the remediation contract

A correct diagnosis can still produce an ineffective plan. Each action contract now
carries a factual description of what the control actually does, and those descriptions
are published in the remediation JSON schema the model must satisfy:

- `restart_demo_worker` replaces a consumer that stopped draining jobs. It changes no
  demo-api behaviour and no configuration flag.
- `restart_demo_api` restarts the API process. Configuration flags persist across
  restarts, so a restart cannot undo a flag change.
- `set_demo_feature_flag` is the only action that changes demo-api configuration, and
  the flag field names both allowlisted flags and what each one does.
- `scale_demo_worker` changes worker job-consumer capacity only. It has no effect on
  demo-api or its database connection pool.

These are properties of the controls, not hints about the active fixture. No scenario
identifier, injection setting, or expected root cause appears in the schema;
`tests/test_models.py::test_action_schema_publishes_factual_semantics_without_fixture_metadata`
asserts both that the semantics reach the sampler grammar and that fixture vocabulary
does not. The deterministic policy engine remains the only authority over risk and
approval; these descriptions cannot widen the allowlist.

Every subsequent reasoning step receives `remediation_history` reconstructed
from persisted plans, action journals, and verification records. It distinguishes
pending, successful, failed, and uncertain actions, includes the predeclared
checks, and summarizes observed outcomes. A successful action with failed recovery
is not presented as resolution. New workflow instances retain this context;
each replacement medium-risk plan still requires its own approval.

Ollama output validation also checks hypothesis citation UUIDs against the exact
evidence IDs in the current context. Both malformed UUIDs and well-formed invented
IDs receive field-specific feedback within the existing bounded retry count.
Repeated invalid references fail safely and remain in model-call audits. The
workflow independently checks references before persisting hypotheses.

An incident without a selected diagnosis gets a factual postmortem identifying
the cause as unconfirmed. It does not request a model-generated synthesis that
could assert a cause after failed investigation.

Regression tests cover missing-citation escalation, citation repair before
approval, stale/foreign evidence, contradictory recovery signals, metrics
timeouts, malformed/empty logs, and transient database failure followed by an
alternate investigation tool. The independent offline evaluation rubric still
checks saved citations and provenance. Passing unit tests does not establish
real-model accuracy: deploy and run new trials after the original matrix ends,
preserving its failures and scores.
