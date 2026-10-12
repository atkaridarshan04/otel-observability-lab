"""gateway - the public edge. Every user request enters the system here.

This is plain FastAPI. There is not one line of OpenTelemetry in this file,
and there never will be - the edge has no business context worth annotating.
"""
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from otelkit import get_logger

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")
UPSTREAM_TIMEOUT_S = float(os.getenv("GATEWAY_UPSTREAM_TIMEOUT_S", "10"))

log = get_logger("gateway")

app = FastAPI(title="gateway")
client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S)


class CheckoutRequest(BaseModel):
    customer_id: str
    sku: str
    qty: int = 1
    tier: str = "standard"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/checkout")
async def checkout(req: CheckoutRequest) -> dict:
    try:
        r = await client.post(f"{ORDERS_URL}/orders", json=req.model_dump())
    except httpx.TimeoutException:
        # Classic cascading-failure shape: a slow dependency becomes OUR 504.
        log.error("orders timed out", extra={"customer_id": req.customer_id, "sku": req.sku, "tier": req.tier})
        raise HTTPException(status_code=504, detail="orders timed out")
    except httpx.HTTPError as exc:
        log.error("orders unreachable", extra={"customer_id": req.customer_id, "error": str(exc)})
        raise HTTPException(status_code=502, detail="orders unreachable")

    if r.status_code >= 500:
        log.error("orders returned 5xx", extra={"status": r.status_code, "customer_id": req.customer_id})
        raise HTTPException(status_code=502, detail="orders failed")
    if r.status_code >= 400:
        # 402 card-declined and 409 out-of-stock are EXPECTED outcomes, not incidents.
        # Phase 5 is largely about never paging anyone for these.
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", "rejected"))

    return r.json()
