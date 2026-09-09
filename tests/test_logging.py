import io
import json
import logging
import os
import subprocess
import sys

from commander.logging_config import JsonFormatter


def test_structured_events_preserve_fields_without_nested_json() -> None:
    record = logging.LogRecord("incident-lab", logging.ERROR, __file__, 1, "fallback", (), None)
    record.event = {
        "service": "demo-api",
        "message": "Checkout failed\nwith a detail",
        "timestamp": "2026-09-08T12:00:00Z",
        "attributes": {"request_id": "request-123", "error_type": "CheckoutPathError"},
    }
    rendered = JsonFormatter("runtime").format(record)
    assert len(rendered.splitlines()) == 1
    event = json.loads(rendered)
    assert event["message"] == "Checkout failed\nwith a detail"
    assert event["service"] == "demo-api" and event["level"] == "ERROR"
    assert event["request_id"] == "request-123" and event["error_type"] == "CheckoutPathError"
    assert event["timestamp"] == "2026-09-08T12:00:00Z"


def test_exception_is_one_json_record_with_traceback() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("agent-runtime"))
    logger = logging.Logger("fixture")
    logger.addHandler(handler)
    try:
        raise ValueError("bad observation")
    except ValueError:
        logger.exception("Investigation failed")
    records = output.getvalue().splitlines()
    assert len(records) == 1
    event = json.loads(records[0])
    assert event["error_type"] == "ValueError"
    assert "bad observation" in event["exception"]
    assert event["service"] == "agent-runtime"


def test_subprocess_logging_keeps_stdout_available_for_mcp() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
from commander.logging_config import configure_logging
configure_logging('mcp-reads')
logging.getLogger('adapter').warning('Read unavailable')
print('protocol-output')
""",
        ],
        env={**os.environ, "LOG_FORMAT": "json"},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "protocol-output"
    event = json.loads(result.stderr)
    assert event["service"] == "mcp-reads" and event["message"] == "Read unavailable"
