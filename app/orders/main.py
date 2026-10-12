"""orders - the interesting one.

It fans out to payments and inventory CONCURRENTLY, then writes to Postgres.
That fan-out is what makes a trace worth looking at: you get a waterfall with two
parallel children, and the request is only as fast as the slower of the two.
"""
import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://otel:otel@postgres:5432/otel")
POOL_SIZE = int(os.getenv("ORDERS_POOL_SIZE", "10"))
UPSTREAM_TIMEOUT_S = float(os.getenv("ORDERS_UPSTREAM_TIMEOUT_S", "5"))

PRICE = {
    "SKU-TELESCOPE": 899.00,
    "SKU-TRIPOD": 149.50,
    "SKU-EYEPIECE": 79.99,
    "SKU-FILTER": 34.25,
    "SKU-STARMAP": 12.00,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orders")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    for attempt in range(30):
        try:
            state["db"] = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=POOL_SIZE)
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(1)
    state["http"] = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S)
    log.info("orders ready pool_size=%s", POOL_SIZE)
    yield
    await state["http"].aclose()
    await state["db"].close()


app = FastAPI(title="orders", lifespan=lifespan)


class OrderRequest(BaseModel):
    customer_id: str
    sku: str
    qty: int = 1
    tier: str = "standard"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/orders")
async def create_order(req: OrderRequest) -> dict:
    order_id = str(uuid.uuid4())
    amount = round(req.qty * PRICE.get(req.sku, 19.99), 2)
    http = state["http"]

    # Fan-out. Both calls carry the W3C `traceparent` header automatically once
    # auto-instrumentation is on - that header is what stitches four services
    # into one trace.
    charge, reserve = await asyncio.gather(
        http.post(f"{PAYMENTS_URL}/charge", json={
            "order_id": order_id, "customer_id": req.customer_id,
            "amount_usd": amount, "tier": req.tier,
        }),
        http.post(f"{INVENTORY_URL}/reserve", json={"sku": req.sku, "qty": req.qty}),
        return_exceptions=True,
    )

    for name, result in (("payments", charge), ("inventory", reserve)):
        if isinstance(result, httpx.TimeoutException):
            log.error("%s timed out order_id=%s customer_id=%s", name, order_id, req.customer_id)
            raise HTTPException(status_code=504, detail=f"{name} timed out")
        if isinstance(result, Exception):
            log.error("%s call failed order_id=%s err=%s", name, order_id, result)
            raise HTTPException(status_code=502, detail=f"{name} unreachable")

    if charge.status_code == 402:
        raise HTTPException(status_code=402, detail="card declined")
    if reserve.status_code == 409:
        raise HTTPException(status_code=409, detail="out of stock")
    if charge.status_code >= 500 or reserve.status_code >= 500:
        failed = "payments" if charge.status_code >= 500 else "inventory"
        log.error("%s returned 5xx order_id=%s", failed, order_id)
        raise HTTPException(status_code=502, detail=f"{failed} failed")
    if charge.status_code >= 400 or reserve.status_code >= 400:
        raise HTTPException(status_code=400, detail="order rejected")

    async with state["db"].acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (id, customer_id, sku, qty, amount_usd, tier) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            order_id, req.customer_id, req.sku, req.qty, amount, req.tier,
        )

    log.info("order placed order_id=%s customer_id=%s amount=%.2f", order_id, req.customer_id, amount)
    return {"order_id": order_id, "status": "placed", "amount_usd": amount, "sku": req.sku}
