"""Bounded continuous checkout load; every outcome comes from real HTTP requests."""

import asyncio
import logging
import os

import httpx

from commander.logging_config import configure_logging


async def run() -> None:
    configure_logging("traffic")
    url = os.getenv("DEMO_API_URL", "http://demo-api:8001").rstrip("/")
    concurrency = int(os.getenv("TRAFFIC_CONCURRENCY", "12"))
    if not 1 <= concurrency <= 32:
        raise ValueError("TRAFFIC_CONCURRENCY must be between 1 and 32")
    async with httpx.AsyncClient(timeout=10) as client:

        async def send() -> None:
            while True:
                try:
                    await client.post(url + "/checkout")
                except httpx.HTTPError:
                    logging.getLogger(__name__).warning("Checkout endpoint unavailable")
                await asyncio.sleep(0.5)

        await asyncio.gather(*(send() for _ in range(concurrency)))


if __name__ == "__main__":
    asyncio.run(run())
