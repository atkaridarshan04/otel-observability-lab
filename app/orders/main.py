"""orders - the interesting one.

It fans out to payments and inventory CONCURRENTLY, then writes to Postgres.
That fan-out is what makes a trace worth looking at: you get a waterfall with two
parallel children, and the request is only as fast as the slower of the two.
"""
import asyncio
import logging
import os
import random
import uuid
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from otelkit import add_event, counter, histogram, record_error, set_attrs, traced, up_down_counter

PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://otel:otel@postgres:5432/otel")
POOL_SIZE = int(os.getenv("ORDERS_POOL_SIZE", "10"))
UPSTREAM_TIMEOUT_S = float(os.getenv("ORDERS_UPSTREAM_TIMEOUT_S", "5"))

# When true, business metrics are labelled with customer.id. The loadgen uses
# 500 distinct customers, so this multiplies every labelled series by ~500.
# Phase 2 turns this on deliberately to watch the series count explode.
CARDINALITY_DEMO = os.getenv("ORDERS_CARDINALITY_DEMO", "false").lower() == "true"

PRICE = {
    "SKU-TELESCOPE": 899.00,
    "SKU-TRIPOD": 149.50,
    "SKU-EYEPIECE": 79.99,
    "SKU-FILTER": 34.25,
    "SKU-STARMAP": 12.00,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orders")

# --- business metrics -------------------------------------------------------
# These answer questions the HTTP-level metrics from auto-instrumentation never
# can. "Is checkout slow?" is an http metric. "Did revenue stop?" is this.
orders_placed = counter("orders.placed", "Orders successfully placed", "{order}")
orders_rejected = counter("orders.rejected", "Orders rejected, by reason", "{order}")
# Unit "{USD}": braces mark an ANNOTATION, not a real unit. OTel's Prometheus
# translation appends real units to the metric name but drops annotations, so
# this stays `orders_value` instead of becoming `orders_value_USD`. Using a bare
# "USD" here is how you end up with orders_value_USD_bucket forever.
order_value = histogram("orders.value", "Distribution of order value", "{USD}")
orders_in_flight = up_down_counter("orders.in_flight", "Orders currently in flight", "{order}")

state: dict = {}


def order_labels(req: "OrderRequest") -> dict:
    """Labels for business metrics.

    tier has 3 values and sku has 5, so this is 15 series per metric. Bounded,
    known in advance, and cheap - that is what a good label set looks like.

    Switching CARDINALITY_DEMO on adds customer.id, which the loadgen draws from
    a pool of 500. Every labelled metric is then 500x larger, for information
    that belongs on a span rather than a metric.
    """
    labels = {"tier": req.tier, "sku": req.sku}
    if CARDINALITY_DEMO:
        labels["customer_id"] = req.customer_id
    return labels


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


@traced("fraud.check", component="risk")
async def fraud_check(customer_id: str, amount: float, tier: str) -> float:
    """Pure business logic. No network call, no database.

    Auto-instrumentation is structurally blind to this function - there is no
    library to monkey-patch. If it ever becomes the slow part of checkout, the
    only reason you would ever find out is this decorator.
    """
    await asyncio.sleep(random.uniform(0.002, 0.012))
    # Exponent >1 skews the distribution toward zero, so a 0.98 threshold rejects
    # ~2% of standard-tier orders and ~1.4% of higher tiers. Keep this rare: a
    # 20% rejection rate would drown out the failure signals of Phase 6.
    score = random.random() ** (1.0 if tier == "standard" else 1.4)
    set_attrs(**{"fraud.score": round(score, 3)})
    return score


@app.post("/orders")
async def create_order(req: OrderRequest) -> dict:
    order_id = str(uuid.uuid4())
    amount = round(req.qty * PRICE.get(req.sku, 19.99), 2)
    http = state["http"]
    labels = order_labels(req)
    orders_in_flight.add(1, labels)
    try:
        return await _create_order(req, order_id, amount, http, labels)
    finally:
        # An UpDownCounter that is only ever incremented is a memory leak with
        # extra steps. Decrement in `finally` so error paths cannot strand it.
        orders_in_flight.add(-1, labels)


async def _create_order(req: OrderRequest, order_id: str, amount: float, http, labels: dict) -> dict:

    # --- the 20% that auto-instrumentation can never supply -----------------
    # The server span already exists and already knows this was POST /orders.
    # These attributes are what let you later ask "are enterprise customers
    # slow?" or "do high-value orders fail more?" - questions nobody builds a
    # dashboard for in advance.
    set_attrs(**{
        "order.id": order_id,
        "customer.id": req.customer_id,
        "customer.tier": req.tier,
        "order.sku": req.sku,
        "order.qty": req.qty,
        "order.value_usd": amount,
    })

    score = await fraud_check(req.customer_id, amount, req.tier)
    if score > 0.98:
        add_event("order.rejected", reason="fraud_score", score=round(score, 3))
        orders_rejected.add(1, {**labels, "reason": "fraud"})
        raise HTTPException(status_code=403, detail="flagged for review")

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
            # Without record_error the span stays GREEN even though the request
            # failed. Tempo, the service graph and every span-derived error
            # metric would all under-report. This one line is load-bearing.
            record_error(result, **{"failed.dependency": name})
            raise HTTPException(status_code=504, detail=f"{name} timed out")
        if isinstance(result, Exception):
            log.error("%s call failed order_id=%s err=%s", name, order_id, result)
            record_error(result, **{"failed.dependency": name})
            raise HTTPException(status_code=502, detail=f"{name} unreachable")

    # Expected business outcomes. Deliberately NOT marked as span errors - if
    # they were, your error rate would include customers mistyping card numbers
    # and you would page someone for it. Phase 5 depends on this distinction.
    if charge.status_code == 402:
        add_event("payment.declined", order_id=order_id)
        orders_rejected.add(1, {**labels, "reason": "card_declined"})
        raise HTTPException(status_code=402, detail="card declined")
    if reserve.status_code == 409:
        add_event("inventory.out_of_stock", sku=req.sku)
        orders_rejected.add(1, {**labels, "reason": "out_of_stock"})
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

    orders_placed.add(1, labels)
    # Recorded while the span is still active, so the SDK attaches an exemplar
    # carrying this request's trace_id. That exemplar is what turns a spike on
    # the chart into one click to the trace that caused it.
    order_value.record(amount, labels)

    log.info("order placed order_id=%s customer_id=%s amount=%.2f", order_id, req.customer_id, amount)
    return {"order_id": order_id, "status": "placed", "amount_usd": amount, "sku": req.sku}
