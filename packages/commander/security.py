import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException


def token_dependency(expected: str) -> Callable[..., None]:
    def verify(authorization: Annotated[str | None, Header()] = None) -> None:
        if not expected:
            raise HTTPException(503, "Access token is not configured")
        if authorization is None or not secrets.compare_digest(authorization, f"Bearer {expected}"):
            raise HTTPException(401, "Invalid access token")

    return verify
