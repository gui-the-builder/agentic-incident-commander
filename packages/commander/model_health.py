"""Read-only Ollama readiness check from the agent's own network environment."""

import sys

import httpx

from commander.config import Settings


def check_model(client: httpx.Client, model: str) -> None:
    response = client.get("/api/tags")
    response.raise_for_status()
    models = response.json().get("models", [])
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        raise ValueError("Ollama returned an invalid model catalog")
    requested = model if ":" in model else model + ":latest"
    available = {item.get("model", item.get("name")) for item in models}
    if requested not in available:
        raise ValueError(
            f"Configured model {model!r} is not installed; pull it before creating incidents"
        )


def main() -> None:
    settings = Settings()
    try:
        with httpx.Client(base_url=settings.ollama_base_url, timeout=10) as client:
            check_model(client, settings.ollama_model)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        print(
            "Ollama readiness failed: check server reachability and pull the configured model. "
            "See docs/local-development.md.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"Ollama: reachable; {settings.ollama_model} is installed")


if __name__ == "__main__":
    main()
