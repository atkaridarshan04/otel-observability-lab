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

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://otel:otel@postgres:5432/otel")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
POOL_SIZE = int(os.getenv("INVENTORY_POOL_SIZE", "10"))
SLOW_QUERY_MS = int(os.getenv("INVENTORY_SLOW_QUERY_MS", "0"))  # chaos knob
CACHE_TTL_S = int(os.getenv("INVENTORY_CACHE_TTL_S", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("inventory")

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

    if cached is not None:
        stock = int(cached)
    else:
        if SLOW_QUERY_MS:
            await asyncio.sleep(SLOW_QUERY_MS / 1000)  # chaos: simulate a bad query plan
        async with state["db"].acquire() as conn:
            row = await conn.fetchrow("SELECT stock FROM inventory WHERE sku = $1", req.sku)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown sku {req.sku}")
        stock = row["stock"]
        await state["cache"].setex(cache_key, CACHE_TTL_S, stock)

    if stock < req.qty:
        log.warning("out of stock sku=%s requested=%s available=%s", req.sku, req.qty, stock)
        raise HTTPException(status_code=409, detail="out of stock")

    async with state["db"].acquire() as conn:
        await conn.execute("UPDATE inventory SET stock = stock - $1 WHERE sku = $2", req.qty, req.sku)
    await state["cache"].delete(cache_key)

    return {"sku": req.sku, "reserved": req.qty, "remaining": stock - req.qty}
