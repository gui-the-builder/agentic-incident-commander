"""JSON service logs on stderr; never write logging into an MCP stdout stream."""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", {})
        if not isinstance(event, dict):
            event = {}
        attributes = event.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        payload: dict[str, Any] = {
            "timestamp": event.get("timestamp")
            or datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": event.get("service", self.service),
            "logger": record.name,
            "request_id": event.get("request_id", attributes.get("request_id")),
            "message": event.get("message", record.getMessage()),
            "error_type": event.get("error_type", attributes.get("error_type")),
            "attributes": attributes,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
            if record.exc_info[0] is not None:
                payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def configure_logging(service: str) -> None:
    # Containers opt in by default. Library/test callers retain their logging setup.
    if os.getenv("LOG_FORMAT") != "json":
        return
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    formatter = JsonFormatter(service)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
    root.setLevel(logging.INFO)
