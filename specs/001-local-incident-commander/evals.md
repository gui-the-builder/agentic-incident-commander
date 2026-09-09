# Evaluation Plan

The portfolio should include quantitative evidence that the agent works reliably.

Do not evaluate only whether the final answer "sounds correct."

---

## 1. Evaluation dimensions

### E1 — Root-cause accuracy

Did the agent select the expected root cause?

Metric:

```text
correct_diagnoses / total_runs
```

Target: ≥ 90% on supported deterministic scenarios.

### E2 — Evidence quality

A diagnosis passes only if it references the minimum expected evidence set.

Example for DB pool exhaustion:

- latency degradation;
- high pool utilization or wait;
- DB connection-related log evidence.

### E3 — Tool efficiency

Track:

- number of tool calls;
- repeated identical calls;
- failed calls;
- total tool time.

Goal: detect wandering investigation.

### E4 — Safety

Automated tests MUST verify:

- blocked tool names cannot execute;
- approval-required actions cannot execute before approval;
- arbitrary shell command requests are rejected;
- arbitrary SQL writes are rejected;
- scenario ground truth is never included in tool context.

Target: 100% pass.

### E5 — Recovery

Did remediation restore the defined health criteria?

Target: ≥ 90% on scenarios with supported remediation.

### E6 — Loop control

Agent MUST stop or escalate after a configured maximum investigation/action loop count.

### E7 — Model robustness

Run each scenario multiple times.

Recommended initial matrix:

```text
3 scenarios
× 10 runs
= 30 incident runs
```

Capture variance in:

- diagnosis;
- tool sequence;
- latency;
- token usage;
- recovery.

---

## 2. Evaluation modes

### Fake-model deterministic tests

Use a scripted model to test graph behavior exactly.

Purpose:

- state transitions;
- approval pause/resume;
- validation;
- retry logic;
- persistence.

### Real local-model evaluations

Use Ollama model configuration.

Purpose:

- reasoning quality;
- tool selection;
- structured output compliance.

Keep model name configurable:

```env
OLLAMA_MODEL=gemma4:12b
```

Do not hard-code a specific model into core tests.

---

## 3. Evaluation record

Persist or export:

```json
{
  "run_id": "...",
  "scenario_id": "worker-stalled",
  "model": "...",
  "correct_diagnosis": true,
  "recovered": true,
  "tool_calls": 7,
  "tool_failures": 0,
  "approval_policy_passed": true,
  "duration_seconds": 42.8,
  "iterations": 3
}
```

---

## 4. Regression suite

`make eval` SHOULD:

1. reset environment;
2. activate scenario;
3. create incident;
4. run agent;
5. auto-approve actions only in evaluation mode when configured;
6. verify expected diagnosis;
7. verify safety invariants;
8. verify recovery;
9. write JSON/CSV summary.

---

## 5. Failure-injection tests for the agent itself

Add tool-behavior fixtures:

- metrics tool timeout;
- malformed log response;
- one transient DB query failure;
- empty result;
- conflicting evidence.

Verify the agent either retries, selects an alternate tool, or escalates safely.

---

## 6. Portfolio output

Generate a small evaluation report such as:

```text
Scenario                     Diagnosis   Recovery   Avg calls
checkout-db-pool-exhaustion     10/10       9/10      8.1
checkout-bad-feature-flag       10/10      10/10      6.4
worker-stalled                   9/10       9/10      5.8
```

Include this in the project README once real results exist.

Never fabricate benchmark numbers.
