"""loadgen - drives steady traffic at the gateway so there is always something to observe."""
import asyncio
import logging
import os
import random

import httpx

from otelkit import get_logger

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
RPS = float(os.getenv("LOADGEN_RPS", "5"))
CONCURRENCY = int(os.getenv("LOADGEN_CONCURRENCY", "10"))

SKUS = ["SKU-TELESCOPE", "SKU-TRIPOD", "SKU-EYEPIECE", "SKU-FILTER", "SKU-STARMAP"]
TIERS = ["standard"] * 70 + ["premium"] * 25 + ["enterprise"] * 5

log = get_logger("loadgen")


async def one_request(client: httpx.AsyncClient) -> None:
    payload = {
        "customer_id": f"cust-{random.randint(1, 500):04d}",
        "sku": random.choice(SKUS),
        "qty": random.randint(1, 3),
        "tier": random.choice(TIERS),
    }
    try:
        r = await client.post(f"{GATEWAY_URL}/checkout", json=payload, timeout=15.0)
        if r.status_code >= 500:
            log.warning("checkout failed status=%s sku=%s", r.status_code, payload["sku"])
    except Exception as exc:  # noqa: BLE001 - loadgen must never die
        log.warning("checkout error %s: %s", type(exc).__name__, exc)


async def main() -> None:
    log.info("loadgen starting target=%s rps=%s", GATEWAY_URL, RPS)
    await asyncio.sleep(8)  # let the services finish booting

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:

        async def guarded() -> None:
            async with sem:
                await one_request(client)

        while True:
            asyncio.create_task(guarded())
            await asyncio.sleep(1.0 / RPS)


if __name__ == "__main__":
    asyncio.run(main())
