import httpx
import pytest

from commander.model_health import check_model


def test_model_readiness_does_not_request_inference() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}]})

    with httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(respond)) as client:
        check_model(client, "gemma4:12b")
        with pytest.raises(ValueError, match="not installed"):
            check_model(client, "missing:latest")


@pytest.mark.parametrize("payload", [{"models": []}, {"models": "invalid"}, {"models": [1]}])
def test_readiness_does_not_confuse_running_server_with_installed_model(payload: dict) -> None:
    with (
        httpx.Client(
            base_url="http://ollama",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client,
        pytest.raises(ValueError),
    ):
        check_model(client, "gemma4:12b")


def test_unreachable_ollama_fails_readiness() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with httpx.Client(
        base_url="http://ollama", transport=httpx.MockTransport(unavailable)
    ) as client:
        with pytest.raises(httpx.ConnectError):
            check_model(client, "gemma4:12b")
