from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    database_url: str = "postgresql+psycopg://commander:commander@localhost:5432/commander"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:12b"
    tool_transport: Literal["direct", "mcp"] = "direct"
    runtime_backend: Literal["compose", "kubernetes"] = "compose"
    kubernetes_api_url: str = "https://kubernetes.default.svc"
    kubernetes_ca_file: str = "/var/run/commander-kubernetes/ca.crt"
    kubernetes_read_token_file: str = "/var/run/commander-kubernetes/read-token"
    kubernetes_action_token_file: str = "/var/run/commander-kubernetes/action-token"
    model_timeout_seconds: float = Field(default=45, gt=0, le=300)
    auto_approve_low_risk: bool = True
    allow_medium_risk_actions: bool = True
    max_investigation_iterations: int = Field(default=8, ge=1, le=30)
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    demo_database_url: str = "postgresql+psycopg://demo:demo@localhost:5432/commander"
    investigation_database_url: str = (
        "postgresql+psycopg://investigation:investigation@localhost:5432/commander"
    )
    demo_api_url: str = "http://demo-api:8001"
    demo_worker_url: str = "http://demo-worker:8002"
    scenario_controller_url: str = "http://scenario-controller:8003"
    control_token: str = Field(default="", repr=False)
    operator_token: str = Field(default="", repr=False)
    verification_interval_seconds: float = Field(default=5, ge=0, le=60)
    worker_poll_seconds: float = Field(default=0.25, gt=0, le=5)
