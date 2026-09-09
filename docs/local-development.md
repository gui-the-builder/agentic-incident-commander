# Local development and Ollama modes

Run commands from the repository root with Docker Compose v2, Python 3.12+, and
`uv` installed. Make is optional. No cloud account is needed.

## Default: host Ollama

```sh
uv sync --frozen --link-mode copy
uv run python -m commander.cli bootstrap
ollama pull gemma4:12b
uv run python -m commander.cli up
uv run python -m commander.cli health
```

Start the Ollama application or `ollama serve` before bringing up the lab.
Bootstrap preserves existing `.env` values and generates missing credentials.
The default settings are:

```dotenv
OLLAMA_RUNTIME=host
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=gemma4:12b
```

The base URL is resolved from the agent container, not from the operator's shell.
Compose supplies the Linux host-gateway mapping. If Ollama only accepts loopback
connections, configure its `OLLAMA_HOST` binding and restart it so containers
can connect; see the [official Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.mdx).
Keep access limited to the local lab using host firewall rules.

On Windows, if Ollama is installed outside PATH, use:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull gemma4:12b
```

## Optional: Ollama in Compose

After bootstrap, set `OLLAMA_RUNTIME=container` in `.env`, then run:

```sh
uv run python -m commander.cli model-pull
uv run python -m commander.cli up
uv run python -m commander.cli health
```

The CLI selects `infra/compose/compose.ollama.yaml` and the `llm` profile for all
Compose lifecycle commands. `model-pull` starts only Ollama and downloads the
configured model into the persistent `ollama-data` volume. Pulling first avoids
making the full stack's startup deadline depend on a large model download.
The overlay points the agent at `http://ollama:11434`, overriding the host URL,
and waits for the server's health check. It exposes no host port.

The provided image is pinned to `ollama/ollama:0.33.3`. This configuration uses
CPU inference without GPU device access. GPU setup is hardware-specific; see
the [official Docker instructions](https://github.com/ollama/ollama/blob/main/docs/docker.mdx).
Host and container installations have separate model storage. Allow enough disk
and memory for the chosen model; CPU generation can require a larger
`MODEL_TIMEOUT_SECONDS` value (supported range: 1–300 seconds).

Finish active incidents/evaluations and run `down` before changing modes. Keep
using CLI lifecycle commands so the selected overlay is applied consistently.
The default stack includes its core services and observability; `core` and
`observability` profiles are not implemented or required. `llm` is the optional
container profile.

## Health and lifecycle

`health` checks the operator services and runs an Ollama check from inside
`agent-runtime`. It reads `/api/tags` and verifies the configured model is
installed; it does not generate text or prove model accuracy. Missing models or
unreachable servers produce a failing exit status.

Use `logs` to inspect service output, `reset` to restore fixtures, and `down` to
stop the stack while preserving volumes. `migrate` runs Alembic provisioning in
Compose; PostgreSQL is not exposed on a host port. Avoid fixture resets or service
rebuilds while an evaluation is running.

Host connectivity and model presence were checked from the running agent.
Both Compose configurations validate and the pinned container image exists.
Container-mode inference remains unverified pending an isolated run after the
current evaluation batch.

## Python service logs

Built application containers set `LOG_FORMAT=json`. Application and Uvicorn logs
include timestamp, level, service, message, request ID/error type when available,
and structured attributes. Exception tracebacks remain within a single JSON
record. Demo operational events retain their existing database payloads for
typed log queries. Other infrastructure images retain their native log formats.

The private MCP processes receive only the nonsecret format setting alongside
their scoped configuration. Their logs go to stderr; stdout remains reserved
for MCP protocol messages. For a Python service started outside Docker, set
`LOG_FORMAT=json` to use the same formatter.
