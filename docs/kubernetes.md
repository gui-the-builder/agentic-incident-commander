# Local Kubernetes extension

Compose remains the default quickstart. Deployment rendering, runtime adapter
selection, and the zero-worker-replica fault are implemented, and the stack has
been deployed and exercised on a local kind cluster. See "Live deployment
evidence" and "Live scenario evidence" below for what was actually observed.

## Cluster and permission boundary

`infra/kubernetes/kind.yaml` defines a single-node `incident-lab` cluster with a
loopback API endpoint. Use a dedicated kubeconfig under ignored `artifacts/`
when creating it, and explicitly select `kind-incident-lab` for operator commands.
This avoids implicitly using a contributor's current cluster context. See
[kind configuration](https://kind.sigs.k8s.io/docs/user/configuration/) and
[isolated kubeconfig usage](https://kind.sigs.k8s.io/docs/user/quick-start/#interacting-with-your-cluster).

Local Windows tooling is available at `artifacts/tools/kind-v0.33.0.exe`; its
published SHA-256 checksum and version were verified. No cluster was started
while the Compose evaluation was active.

The node image is pinned to Kubernetes v1.37.0 by digest. A checksum-verified
`artifacts/tools/kubectl-v1.37.0.exe` is used locally; the older system kubectl
installation is unchanged.

## Deployment and operation

The local deployment commands are:

```sh
uv run python -m commander.kube_cli up
uv run python -m commander.kube_cli status
uv run python -m commander.kube_cli model-check
```

The CLI builds and loads the existing image, creates namespace RBAC, renders
`artifacts/kubernetes/deployment.yaml`, applies services, and waits for migrations
and application readiness. The manifest contains credentials and stays ignored
by Git. PostgreSQL uses a persistent claim; startup containers wait for schema
and role grants. Re-running `up` replaces the disposable migration Job but
preserves the database volume. Run it only when no Kubernetes incident is active.

Only `artifacts/kubernetes/kubeconfig` and context `kind-incident-lab` are used.
The CLI rejects non-loopback API endpoints. Local ports are Commander 18000,
demo API 18001, scenarios 18003, Prometheus 19090, and Grafana 13000. Host Ollama
defaults to `host.docker.internal:11434`; set `KUBERNETES_OLLAMA_BASE_URL` in `.env`
if that host route differs. On Docker Desktop for Windows this default route was
verified from inside the `agent-runtime` pod with `kube_cli model-check`.

Separate read/action service-account tokens are requested for 24 hours. Use
`uv run python -m commander.kube_cli refresh-tokens` before expiry. It updates
only the token Secret; clients reread mounted tokens per request. The runtime
has both scoped credentials but no automatically mounted account token.
Direct and MCP transports use the same backend selection. MCP children receive
only their respective token-path configuration; they share the runtime's OS
identity and are not a sandbox against malicious adapter code.

`rbac.yaml` declares separate read/action service accounts and namespace Roles.
Reads permit named demo Deployment inspection and namespace pod listing.
Actions permit get/patch of only `demo-api` and `demo-worker`, plus the worker
scale subresource. Neither account receives secrets, exec, or cluster-wide
permissions. RBAC cannot constrain individual patch fields; the trusted adapter
limits patch bodies. See [Kubernetes RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/).

## Implemented adapters

`KubernetesReads.workload` returns replica counts, generation, rollout conditions,
and pod phase/readiness/restarts/waiting reasons. Fixed namespace/workload names
and bounded listing constrain queries. Raw specs, environment values, and status
messages are excluded. `KUBERNETES_READ_TOOLS` supplies the typed catalog entry
for the existing audited `ReadGateway`.

`KubernetesActions` implements existing remediation contracts. Restarts patch
the Deployment pod-template annotation with the journal action UUID. Worker
scaling maps `concurrency` to replicas, constrained to 1–4 as permitted by the
specification. Resource-version preconditions reject concurrent changes. Flag
changes retain the controlled service API.

The backend must run behind `ActionGateway` with
`Policy(action_risks=KUBERNETES_ACTION_RISKS)`. Kubernetes worker restarts are
medium risk because a rollout can affect multiple replicas, so explicit
plan-bound approval is required. Successful results are reused; failed or
uncertain mutations are never retried automatically. Action audits record risk
and approval requirements for the offline safety scorer.

## Offline verification

All 34 rendered workload/RBAC resources pass offline field/type validation against
the official Kubernetes v1.37 OpenAPI schema, using placeholder credentials.
The evidence is `artifacts/kubernetes/manifest-schema-check.json`.

Mock-transport tests cover exact request paths/bodies, resource-version
preconditions, sanitized output, evidence auditing, scale validation, approval
before HTTP requests, deduplication, and non-replay after conflicts. RBAC tests
reject dangerous grants.

## Live deployment evidence

`kube_cli up` was run on 2026-09-08 against Docker Desktop on Windows with the
checksum-verified `kind` v0.33.0 and `kubectl` v1.37.0 binaries. Observed:

- The `incident-lab` cluster was created from `infra/kubernetes/kind.yaml` with the
  digest-pinned v1.37.0 node image; the image built from `infra/compose/Dockerfile`
  loaded into the node.
- RBAC applied, the manifest rendered with two 24-hour service-account tokens, all
  objects were admitted, the `commander-migrate` Job completed (it restarted three
  times while PostgreSQL initialised, then succeeded), and every Deployment rollout
  finished: postgres, commander-api, demo-api, demo-worker, scenario-controller,
  agent-runtime, traffic, prometheus, otel-collector, grafana (11/11 pods Running).
- Operator ports respond on the host: Commander 18000 (`/health`, `/ready`), demo API
  18001, scenario controller 18003, Prometheus 19090, Grafana 13000.
- `kube_cli model-check` reported host Ollama reachable with `gemma4:12b` installed
  from inside the `agent-runtime` pod.
- `kubectl auth can-i` impersonating both service accounts matches the declared
  boundary exactly; the capture is `artifacts/kubernetes/rbac-live-check.txt`.
  `commander-reads` can get only the two demo Deployments and list namespace pods.
  `commander-actions` can get/patch only the two demo Deployments and the worker
  scale subresource. Neither can read secrets, exec, delete, list Deployments,
  touch `postgres`, or see `kube-system`.
- Running `KubernetesReads.workload` inside the pod with the mounted read token
  returned the sanitized `demo-worker` status through the real API server, and the
  same token received HTTP 403 when it attempted a scale patch.

## Kubernetes failure scenario

After deployment, with no other Kubernetes incident active:

```sh
uv run python -m commander.kube_cli fault
uv run python -m commander.kube_cli incident
uv run python -m commander.kube_cli demo <incident-uuid>
uv run python -m commander.kube_cli reset
```

The trusted operator fault command resets Kubernetes fixtures, scales the worker
to zero, waits for its pods to disappear, and submits forty real checkouts.
The nested fixture `scenarios/kubernetes/worker-zero-replicas.yaml` stays outside
the Compose scenario catalog. Only alert fields reach the incident API. All
operator HTTP commands use ports 18000/18001/18003; Compose keeps its own ports.
`demo` displays the plan and asks the operator to approve or reject it.

Diagnosis requires a complete, current zero-replica workload observation and a
database backlog count. Remediation uses the existing medium-risk scale action;
its `concurrency` argument maps to 1–4 replicas. Restarting a zero-replica
Deployment preserves zero replicas and cannot restore processing. The model
receives the selected backend's action semantics. Verification requires real
counter growth, a drained backlog, and a current success timestamp; missing
pre-remediation scrapes are never interpreted as zero.

## Live scenario evidence

The fault ran end to end on the kind cluster with `TOOL_TRANSPORT=mcp` and
`RUNTIME_BACKEND=kubernetes`. Incident `abd3ad74-f630-4ce6-9ed3-ccadaec9cefe`
(`artifacts/evals/kubernetes-zero-replicas-20260908-v2/`) passed diagnosis, cited evidence,
recovery, and approval policy, resolving in 113.6 seconds over two investigation iterations.

What was observed:

- With the worker scaled to zero, its health read and all three worker metrics genuinely
  failed. Fourteen of thirty-seven tool calls failed, and those failures were recorded and
  acknowledged rather than hidden.
- The agent read the workload through `get_kubernetes_workload` and the backlog through the
  named `pending_jobs_count` query. Its first diagnosis attempt was returned to investigation
  by the gate with "Cite the latest database pending_jobs_count above ten"; it then gathered
  that reading and passed.
- The plan was classified MEDIUM, paused in `AWAITING_APPROVAL`, and executed only after
  approval. The action was a real scale patch against the API server using the action token,
  taking the Deployment from 0 to 4 replicas.
- The backlog drained from 1280 pending jobs to 1, the processed counter grew, and
  verification confirmed recovery. The runner then restored one replica.

An earlier attempt, `f2a569f6-ac24-49b6-9e84-5a9a23cbe052`, is preserved in
`artifacts/evals/kubernetes-zero-replicas-20260908/` and failed. With every worker signal
unavailable, the model selected a hypothesis with no valid citations, `validate_diagnosis`
raised, and the workflow ended the incident in `FAILED` after two iterations. A rejected
selection is now treated as a recoverable reasoning error that returns to bounded
investigation; see `docs/diagnosis-gate.md`.

This is one successful trial of one scenario. It establishes that the deployment, scoped
credentials, fault injection, approval gate, real mutation, and recovery all work against a
live cluster. It does not establish a reliability rate.

For an isolated real-model evaluation with JSON/CSV scores, evidence, and reports:

```sh
uv run python -m commander.evals --backend kubernetes --trials 1 --approve-fixtures --output artifacts/evals/kubernetes-01
```

This explicitly targets the Kubernetes fixture and ports, then restores one
worker replica after the batch completes. The default evaluation backend remains
Compose. Mismatched backend/scenario selections fail before any mutation.
Without `--approve-fixtures`, the runner stops for operator review at approval
and preserves the incident ID; it does not reset an active incident. Rubric 2.1
checks the cited workload and database observations independently of the runtime
diagnosis gate. No Kubernetes benchmark has been recorded yet.
