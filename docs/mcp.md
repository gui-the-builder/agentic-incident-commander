# MCP tool boundaries

The default Compose quickstart calls typed Python adapters directly. Set
`TOOL_TRANSPORT=mcp` in `.env` and rebuild/recreate `agent-runtime` to use MCP:

```sh
uv run python -m commander.cli up
uv run pytest tests/test_mcp.py -q
```

Wait for active incidents/evaluations to finish before changing the runtime.
Both transports use the same operational handlers, input/output contracts,
evidence records, approval policy, and action journal. MCP adds a standard
protocol for replacing or independently hosting those adapters; it does not
grant the model new capabilities.

## Separate servers

The runtime launches two private stdio subprocesses using the installed Python
interpreter and the MCP SDK. No network listener or Docker socket is exposed.
For manual MCP client configuration, the entrypoints are:

```sh
python -m commander.mcp_transport reads
python -m commander.mcp_transport actions
```

The read process receives only the investigation database URL, Prometheus URL,
and demo service URLs. It exposes the six read tools with their original JSON
schemas. The investigation database role enforces read-only access and hides
scenario ground truth. The client retains bounded retries and per-attempt audits.

The action process receives the incident database URL, control token, service
URLs, and policy settings. Its sole tool is `execute_plan(incident_id, plan_id)`.
It reloads the current persisted plan, checks incident ownership and EXECUTING
state, requires the stored approval where applicable, and claims journal entries
before side effects. A caller cannot supply an approval boolean or arbitrary
action parameters. Successful actions reuse their stored result; uncertain
actions cannot be automatically replayed.

Neither process receives the operator token, and both disable automatic `.env`
loading. SDK subprocess inheritance is limited to essential OS variables plus
the explicit configuration above. This separates credentials and callable
capabilities; stdio subprocesses share the runtime's OS identity and are not an
OS sandbox against malicious server code. Use separate identities/containers if
deploying untrusted adapters. The model only sees the read catalog and proposes
plans; the workflow alone invokes the action client after its approval gate.

## Verification

Contract tests exercise framed MCP requests for all six reads, identical schemas
and outputs, evidence/audit creation, transient retries, denied actions without
approval, forged approval fields, stale plan IDs, and successful-action replay.
Additional tests start both actual stdio subprocesses with separate credential
environments. These deterministic tests do not reset the live evaluation lab.

A real `gemma4:12b` flag incident passed diagnosis, cited-evidence, recovery, and
approval checks through the MCP runtime. The validation stopped the runtime at
approval, approved the persisted plan while it was stopped, checked that actions
remained pending, then restarted it and verified recovery. The incident is
`a0f476d4-de5f-4a19-b156-509bf098fc5b`; its audit and `restart-proof.json` are under
`artifacts/evals/mcp-gate-restart-20260908/`. This single trial proves the exercised
restart path, not a multi-scenario reliability rate.
