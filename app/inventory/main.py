"""inventory - reserves stock, with a Redis cache in front of Postgres.

The cache is here on purpose: a cache hit and a cache miss produce visibly
different traces, which is the cleanest possible demonstration of why a latency
AVERAGE lies to you and a histogram does not.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from otelkit import add_event, get_logger, set_attrs

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://otel:otel@postgres:5432/otel")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
POOL_SIZE = int(os.getenv("INVENTORY_POOL_SIZE", "10"))
SLOW_QUERY_MS = int(os.getenv("INVENTORY_SLOW_QUERY_MS", "0"))  # chaos knob
CACHE_TTL_S = int(os.getenv("INVENTORY_CACHE_TTL_S", "10"))

log = get_logger("inventory")

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
    state["cache"] = aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("inventory ready pool_size=%s slow_query_ms=%s", POOL_SIZE, SLOW_QUERY_MS)
    yield
    await state["cache"].aclose()
    await state["db"].close()


app = FastAPI(title="inventory", lifespan=lifespan)


class ReserveRequest(BaseModel):
    sku: str
    qty: int = 1


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/reserve")
async def reserve(req: ReserveRequest) -> dict:
    cache_key = f"stock:{req.sku}"
    cached = await state["cache"].get(cache_key)

    # `cache.hit` is the single highest-value attribute in this whole service.
    # Hits and misses are two different populations with wildly different
    # latencies, and any average over both describes neither. In Phase 2 this
    # becomes the reason to use histograms; in Grafana it is the attribute you
    # group by to split a bimodal latency distribution into two honest ones.
    set_attrs(**{
        "cache.hit": cached is not None,
        "inventory.sku": req.sku,
        "inventory.qty_requested": req.qty,
    })

    if cached is not None:
        stock = int(cached)
    else:
        add_event("cache.miss", key=cache_key)
        async with state["db"].acquire() as conn:
            # The sleep is INSIDE the pool checkout on purpose. A slow query
            # does not politely release its connection while it runs - it holds
            # one for its whole duration, which is exactly why a slow query and
            # a small pool combine into an outage that neither causes alone.
            if SLOW_QUERY_MS:
                await asyncio.sleep(SLOW_QUERY_MS / 1000)
            row = await conn.fetchrow("SELECT stock FROM inventory WHERE sku = $1", req.sku)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown sku {req.sku}")
        stock = row["stock"]
        await state["cache"].setex(cache_key, CACHE_TTL_S, stock)

    set_attrs(**{"inventory.stock_available": stock})

    if stock < req.qty:
        log.warning("out of stock", extra={"sku": req.sku, "requested": req.qty, "available": stock})
        raise HTTPException(status_code=409, detail="out of stock")

    async with state["db"].acquire() as conn:
        # Same again on the write path, which EVERY request takes regardless of
        # whether the cache hit. This is what makes pool contention universal
        # rather than limited to cache misses.
        if SLOW_QUERY_MS:
            await asyncio.sleep(SLOW_QUERY_MS / 1000)
        await conn.execute("UPDATE inventory SET stock = stock - $1 WHERE sku = $2", req.qty, req.sku)

    # Write-THROUGH, not invalidate. An earlier version of this deleted the key
    # here, which meant every single request was a cache miss and the cache was
    # decorative. With a short TTL this gives a realistic mix of hits and misses,
    # which is exactly the bimodal latency distribution Phase 2 needs.
    await state["cache"].setex(cache_key, CACHE_TTL_S, stock - req.qty)

    return {"sku": req.sku, "reserved": req.qty, "remaining": stock - req.qty}
