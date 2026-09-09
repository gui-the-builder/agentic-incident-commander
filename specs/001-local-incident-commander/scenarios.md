# Incident Scenarios

All MVP scenarios must be deterministic, resettable, observable through tools, and solvable without hidden scenario metadata.

---

# Scenario 1 — Checkout latency from DB pool exhaustion

**ID:** `checkout-db-pool-exhaustion`

## Healthy baseline

- checkout p95 < 300 ms
- DB pool utilization < 70%
- DB acquisition wait near zero
- 5xx rate < 1%

## Injection

Scenario controller causes the demo API to hold DB connections longer than normal or reduce effective pool capacity.

Possible implementation:

- feature toggle enables artificial `sleep()` while holding a DB connection;
- pool size is reduced to a small value;
- load generator sends concurrent checkout traffic.

## Observable symptoms

- p95 latency rises above 2 s;
- `db_pool_in_use` approaches pool limit;
- `db_pool_wait_seconds` increases;
- logs contain connection acquisition timeout/wait messages;
- database itself remains reachable.

## Expected diagnosis

Database connection-pool exhaustion / excessive connection hold time.

## Expected remediation

For MVP, remediation can disable the injected slow-DB feature or restore pool configuration.

Risk: MEDIUM.

## Verification

- p95 <= 500 ms for 3 consecutive checks;
- DB wait returns near baseline;
- error rate remains acceptable.

---

# Scenario 2 — 5xx spike from bad feature flag

**ID:** `checkout-bad-feature-flag`

## Healthy baseline

- 5xx rate < 1%;
- checkout requests succeed;
- both old and new code paths available.

## Injection

Enable `new_checkout_path=true`.

The new path deterministically throws an application error for a subset or all qualifying requests.

## Observable symptoms

- 5xx rate > 20%;
- latency may remain normal;
- logs include a specific application exception;
- recent configuration history shows the flag changed shortly before the incident;
- DB health remains normal.

## Expected diagnosis

Bad checkout feature flag/configuration.

## Expected remediation

Set:

```text
new_checkout_path=false
```

Risk: MEDIUM.

## Verification

- 5xx rate <= 2%;
- successful checkout probe;
- no new matching exceptions.

---

# Scenario 3 — Worker backlog from stalled worker

**ID:** `worker-stalled`

## Healthy baseline

- pending jobs < 10;
- worker heartbeat recent;
- jobs processed continuously.

## Injection

Worker process remains alive but stops consuming jobs.

Do not make health endpoint fail immediately; the point is to require deeper investigation.

## Observable symptoms

- `worker_jobs_pending` rises steadily;
- `worker_jobs_processed_total` stops increasing;
- `worker_last_success_timestamp` becomes stale;
- API remains healthy;
- worker heartbeat may be stale or partially healthy.

## Expected diagnosis

Worker consumer is stalled.

## Expected remediation

Restart demo worker.

Risk: LOW.

## Verification

- processed counter resumes;
- backlog begins falling;
- worker last-success timestamp becomes current.

---

# Scenario design rules

Each scenario MUST include:

```yaml
id:
title:
service:
severity:
injection:
reset:
expected_signals:
distractor_signals:
expected_root_cause:
allowed_remediations:
verification:
```

Include at least one distractor signal in each scenario so the agent must compare evidence rather than match one obvious string.

---

# Optional future scenarios

## Memory leak / resource exhaustion

Good Kubernetes milestone scenario.

## Dependency slowdown

A downstream service becomes slow while remaining healthy.

## Cache stampede

Cache disabled or invalidated causing DB load.

## Bad deployment

Recent local build version introduces an error.

## Certificate/secret expiration simulation

Useful conceptually, but avoid real secret management complexity for MVP.
