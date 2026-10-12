"""payments - charges the card.

Failure behaviour is driven entirely by environment variables so that the Phase 6
chaos scripts can induce incidents without touching code or rebuilding images.
"""
import asyncio
import logging
import os
import random

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LATENCY_MS = int(os.getenv("PAYMENTS_LATENCY_MS", "40"))
LATENCY_JITTER_MS = int(os.getenv("PAYMENTS_LATENCY_JITTER_MS", "30"))
FAILURE_RATE = float(os.getenv("PAYMENTS_FAILURE_RATE", "0.0"))   # 503 - a real incident
DECLINE_RATE = float(os.getenv("PAYMENTS_DECLINE_RATE", "0.02"))  # 402 - normal business

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("payments")

app = FastAPI(title="payments")


class ChargeRequest(BaseModel):
    order_id: str
    customer_id: str
    amount_usd: float
    tier: str = "standard"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/charge")
async def charge(req: ChargeRequest) -> dict:
    await asyncio.sleep((LATENCY_MS + random.uniform(0, LATENCY_JITTER_MS)) / 1000)

    if random.random() < FAILURE_RATE:
        log.error(
            "payment processor unreachable order_id=%s customer_id=%s amount=%.2f",
            req.order_id, req.customer_id, req.amount_usd,
        )
        raise HTTPException(status_code=503, detail="payment processor unreachable")

    if random.random() < DECLINE_RATE:
        log.warning(
            "card declined order_id=%s customer_id=%s amount=%.2f",
            req.order_id, req.customer_id, req.amount_usd,
        )
        raise HTTPException(status_code=402, detail="card declined")

    log.info("charged order_id=%s amount=%.2f tier=%s", req.order_id, req.amount_usd, req.tier)
    return {"order_id": req.order_id, "status": "charged", "amount_usd": req.amount_usd}
