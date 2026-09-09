# Real-model evaluation results

## Completed 30-run baseline

The original direct-transport Gemma batch completed ten trials per scenario.
Original audits/scores remain in `artifacts/evals/matrix-20260908/`; all thirty
evidence exports were rescored separately into `artifacts/evals/matrix-20260908-v21/`.
Rubric 2.1 retains the same Compose evidence checks as 2.0.

| Scenario | Diagnosis | Cited evidence | Recovery | Approval policy | Avg duration | Avg tool calls |
| --- | --- | --- | --- | --- | --- | --- |
| Pool exhaustion | 6/10 | 0/10 | 9/10 | 10/10 | 183.4 s | 115.9 |
| Bad checkout flag | 8/10 | 4/10 | 9/10 | 10/10 | 239.8 s | 113.6 |
| Stalled worker | 9/10 | 0/10 | 10/10 | 10/10 | 176.8 s | 88.9 |

Totals are diagnosis **23/30 (76.7%)**, cited evidence **4/30 (13.3%)**, recovery
**28/30 (93.3%)**, and approval policy **30/30**. The diagnosis and evidence
requirements are not met. Durations include verification and reports; tool-call
counts include repeated recovery polling. Diagnosis is the documented phrase
rubric, not an independent semantic guarantee.

The batch ran the earlier code throughout. Citation gates, reference-repair
feedback, remediation history, and MCP have since been prepared for fresh live
validation; their tests do not establish improved real-model reliability. The
historical failures remain part of the record.

## Log-search fix: first cited-evidence pass on the pool scenario

The completed 30-run baseline scored 0/10 cited evidence on pool exhaustion. The cause was
identified in `artifacts/evals/mcp-gate-pool-20260908`: `query_logs` matched only the message
column, while the demo API stores the failure name in `attributes.error_type`, so every
`PoolTimeout` search returned nothing and that trial escalated after eight iterations without
remediation. The read adapter now searches both fields with literal substring matching.

Trial `1dac59c9-6d13-4ffc-b0ba-0f6a0f27ce5d` (`artifacts/evals/mcp-fix-pool-20260908`, MCP
transport) is the first pool run to pass the citation rubric with no missing checks, and its
diagnosis check passed as well.

| Check | Result |
| --- | --- |
| Correct diagnosis | pass |
| Cited evidence (rubric 2.1) | pass, no missing checks |
| Recovery | fail |
| Approval policy | pass |

It still ended `ESCALATED`. The model diagnosed pool exhaustion correctly, then proposed
`scale_demo_worker` with concurrency 4. Worker concurrency cannot relieve the demo-api
connection pool, verification never passed, and the incident exhausted its eight-iteration
budget in 705.3 seconds. The approval gate behaved correctly throughout: the medium-risk plan
paused, was approved, executed once, and was never replayed.

This is one trial. It shows the evidence-selection gap was a tool defect rather than a
reasoning limit, and it exposes a separate remediation-selection gap.

## Action-semantics fix: first full pool pass

The action contracts now publish factual descriptions of what each control does, and the
remediation prompt requires an action that counteracts the diagnosed mechanism; see
`docs/diagnosis-gate.md`. After rebuilding the runtime with that change, trial
`ccf4fe6a-93ee-4a9b-a74d-a1c05eaa107e` (`artifacts/evals/action-semantics-pool-20260908`, MCP
transport) passed every check and resolved.

| Check | Baseline `1dac59c9` | With action semantics `ccf4fe6a` |
| --- | --- | --- |
| Correct diagnosis | pass | pass |
| Cited evidence (rubric 2.1) | pass | pass |
| Recovery | fail | pass |
| Approval policy | pass | pass |
| Final status | `ESCALATED` | `RESOLVED` |
| Chosen action | `scale_demo_worker` | `set_demo_feature_flag(slow_db=false)` |
| Investigation iterations | 8 of 8 | 1 of 8 |
| Duration | 705.3 s | 194.0 s |
| Prompt tokens | 1,049,366 | 43,791 |

The two trials differ only in the runtime build; the scenario, transport, rubric, and
approval path are identical. The medium-risk plan still paused for explicit approval and
executed exactly once.

These are single trials on one scenario. They demonstrate that the two defects were real and
that the fixes address them; they do not establish a reliability rate. The 30-run matrix in
the table above predates all of these fixes and has not been rerun.

## Live Kubernetes zero-replica scenario

The first live run of the Kubernetes backend. Both trials used the MCP transport on the kind
cluster; they differ only in whether a rejected diagnosis ends the incident.

| Trial | Diagnosis | Cited evidence | Recovery | Approval | Status | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| `f2a569f6` (before fix) | fail | fail | fail | pass | `FAILED` | 43.3 s |
| `abd3ad74` (after fix) | pass | pass | pass | pass | `RESOLVED` | 113.6 s |

The successful run scaled the demo-worker Deployment from zero to four replicas through a real
approved API-server patch and drained a 1280-job backlog. Fourteen of its thirty-seven tool
calls failed because a worker with no pods genuinely has no health or metrics; those failures
were audited and acknowledged. Artifacts are under
`artifacts/evals/kubernetes-zero-replicas-20260908/` and `-v2/`. This is one trial per
condition, not a reliability rate. See `docs/kubernetes.md`.

## Evidence rubric correction

The original evidence scores below checked tool presence and were too weak to
prove the specification's citation requirement. [Rubric 2.0](evaluation-rubric.md)
checks the actual cited observations and their provenance. Rescoring preserves
the original audits and scores in separate directories.

| Completed sample | Original evidence passes | Citation-based evidence passes |
| --- | --- | --- |
| Three-scenario smoke batch | 3/3 | 0/3 |
| First six completed matrix trials (pool scenario only) | 5/6 | 0/6 |

The smoke flag diagnosis omitted exception-log citations; the worker diagnosis
cited one counter reading and omitted liveness; the pool diagnosis cited only
configuration/change history. The matrix snapshot lacks required cited latency
and, in several trials, connection-log observations. One matrix run failed
hypothesis generation after repeated malformed evidence UUIDs. These are real
failures, not passing benchmark evidence.

Sources: `artifacts/evals/smoke-20260908-v2/` and
`artifacts/evals/matrix-20260908-v2-partial/`. The latter contains exactly six
completed runs and does not establish full-matrix performance. Diagnosis,
recovery, and approval results remain independent of evidence quality.

## Original smoke scores (tool-presence evidence rubric)

Model: `gemma4:12b`, local Ollama. This is one trial per scenario, not the required
ten-trial matrix. Source audits and postmortems are saved under
`artifacts/evals/smoke-20260908/`; JSON and CSV contain the raw scores.

| Scenario | Diagnosis | Evidence | Recovery | Approval policy | Duration |
| --- | --- | --- | --- | --- | --- |
| DB pool exhaustion | Fail | Pass | Pass | Pass | 217.6 s |
| Bad checkout flag | Pass | Pass | Pass | Pass | 270.2 s |
| Stalled worker | Pass | Pass | Pass | Pass | 192.7 s |

Durations include model calls, remediation, verification windows, and report
generation. They are not investigation-only latency.

The pool trial selected: “The high checkout latency is caused by a database
performance degradation introduced in the 'local-v1' deployment, specifically
linked to the 'slow_db' configuration flag.” This does not explicitly name pool
exhaustion, so the diagnosis rubric failed it. The plan subsequently identified
pool exhaustion correctly and disabled `slow_db`, restoring measured health.
The score has not been changed to count a correct action as a correct diagnosis.

The hypothesis prompts have since been refined to demand a causal mechanism.
Repeated verification observations also made the initial postmortem prompts large;
the report now retains cited evidence and first/last observations per signal while
the full timeline stays in the audit.

## Refined pool trial

Run `06b0ab51-6606-4a6a-b3f9-5f3f51798c0b` passed all four original checks in 198.0 seconds.
It selected a diagnosis explaining that slow queries exhaust the limited pool of
four connections. Recorded prompt usage was 51,450 tokens versus 141,534 in the
initial pool run. This single follow-up is saved under
`artifacts/evals/refined-pool-20260908/`; it does not replace the failed initial score.
The completed 30-run matrix is reported above; this smoke result is preserved separately.

Run IDs:

- Pool: `f5105703-b279-458f-83f6-91916dc45c03`
- Flag: `6f5a2229-05ce-4987-911f-e5a720482e07`
- Worker: `ed2f2052-8717-4a03-81fa-d390e1750df5`
