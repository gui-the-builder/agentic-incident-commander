# ADR-001: Local-First Runtime

**Status:** Accepted

## Context

The project owner currently intends to develop and run the system primarily locally and does not require cloud infrastructure for the portfolio MVP.

The project must still demonstrate production engineering concepts such as:

- service boundaries;
- observability;
- failure injection;
- safe operational actions;
- persistence;
- agent reliability.

## Decision

The MVP will use Docker Compose as its primary runtime and Ollama as the default model runtime.

Cloud-provider-specific services will not be required.

A local Kubernetes deployment using kind or k3d will be implemented only after the Compose-based MVP is stable.

## Consequences

### Positive

- zero required cloud cost;
- low setup friction for reviewers;
- reproducible incidents;
- easy local debugging;
- architecture remains portable.

### Negative

- does not initially demonstrate managed cloud services;
- some production behaviors must be simulated;
- Docker Compose does not model every Kubernetes failure mode.

## Mitigation

Design infrastructure integrations behind interfaces so a later cloud or Kubernetes adapter can be added without rewriting the agent workflow.

Examples:

```text
MetricsClient
LogClient
ServiceControl
DeploymentHistory
ReadonlyDatabaseQueries
```

The portfolio narrative should emphasize that the local environment is a deliberate reproducible incident lab, not an attempt to pretend Compose is a full production platform.
