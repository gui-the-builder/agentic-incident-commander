# Evaluation rubric and reproducible rescoring

Every new evaluation result includes `scorer_version`. Version 2.0 replaces the
initial tool-presence evidence check with citation-based validation. The active
matrix was started with the original code and continues to use that version;
changing local source does not change its already-loaded scorer or containers.

Version 2.1 adds the Kubernetes zero-replica scenario. Its existing three Compose
evidence rules are unchanged from 2.0. New run metadata and rescores identify the
runtime backend; historical run files without that field default to Compose.

## Evidence quality

Only supporting citations on the selected, persisted diagnosis count. Each must
belong to the same incident and link to a successful allowlisted tool call with
an identical payload and observation timestamp. Input/output contracts must
validate. Observations and completed calls must precede diagnosis selection and
plan creation. Relevant service names and metric names must agree.

The minimum cited signal sets are:

| Scenario | Required observations |
| --- | --- |
| Pool exhaustion | Latency over 0.5 seconds; pool utilization over 3.5 connections or acquisition wait over 0.05 seconds; recent `PoolTimeout` log entries. |
| Bad checkout flag | Error ratio over 2%; recent `CheckoutPathError` logs; recent change history enabling `new_checkout_path`. |
| Stalled worker | Backlog over ten; two temporally distinct, unchanged processed-counter readings; last success older than 30 seconds; healthy worker instance. |
| Kubernetes zero replicas | Zero desired/ready/available worker replicas, observed controller generation, complete empty pod list, and named database pending count over ten. Workload/backlog observations must be within three minutes before diagnosis. |

Log/change timestamps must fall inside the requested tool window. Healthy values,
uncited observations, empty logs, and verification readings collected later do
not prove the initial diagnosis. Missing exports fail evidence assessment rather
than silently falling back to tool-name presence. Results list missing checks
and invalid citation IDs to make failures inspectable.

Diagnosis retains the original phrase rubric; it is an approximate classifier,
not a semantic correctness guarantee. Recovery and approval checks remain
separate. Repeated-identical-call counts include verification polling; use the
full audit and investigation-iteration count to distinguish necessary recovery
sampling from redundant investigation.

## Export and rescore

New runs save evidence alongside incident, hypothesis, and timeline records in
`audit.json`, using the authenticated incident `/evidence` endpoint. They also
export `/remediations`: every plan's diagnosis, risk, approval, action statuses,
expected checks, and observed verification. Verification timeline events retain
their plan IDs. These records distinguish successful execution from successful
recovery and preserve earlier attempts after a replacement plan. For older
audits, the following command reads persisted evidence through the existing
Compose API container and creates supplemental `evidence.json` files:

```sh
uv run python -m commander.export_evidence artifacts/evals/smoke-20260908
uv run python -m commander.rescore artifacts/evals/smoke-20260908 --output artifacts/evals/smoke-v2
```

Run from the repository root. Export only reads completed audits and database
records; it neither resets scenarios nor restarts services. Existing supplements
are preserved. Rescoring requires an empty, separate output directory and writes
JSON/CSV with source-audit paths. Preserve original scores, including failures.
When rescoring a live batch, label the result partial and record the run count;
it is a snapshot of completed audits, not the full matrix.
