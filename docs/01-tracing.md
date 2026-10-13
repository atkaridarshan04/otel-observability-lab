# Phase 1 — Tracing

Stack added: **Tempo** (trace storage) and **Grafana** (the UI). The collector now fans traces out to both Tempo and stdout.

Note what did *not* change to add a tracing backend: no application code, no image rebuild, no SDK setting. The services still send OTLP to the collector and have no idea Tempo exists. That indirection is the collector earning its keep for the first time.

Grafana: <http://localhost:3000> (anonymous admin, lab only). Tempo's API is on <http://localhost:3200> if you prefer curl.

## What a span actually is

A span is one unit of work with a start and end. Its parts:

- **Name** — low cardinality, describing the *class* of operation. `POST /orders`, never `POST /orders/8f3a-...`. Putting an ID in a span name is the fastest way to make a trace UI useless.
- **SpanKind** — `SERVER`, `CLIENT`, `INTERNAL`, `PRODUCER`, `CONSUMER`. Not decoration: a backend pairs a CLIENT span in one service with the SERVER span it caused in another, and that pairing is what generates a service dependency graph. Get kinds wrong and your service map is wrong.
- **Status** — `UNSET`, `OK`, or `ERROR`. Only `ERROR` counts as a failure anywhere downstream.
- **Attributes** — key/values on this one span.
- **Events** — timestamped points *inside* the span, with no duration. Much cheaper than a child span.
- **Links** — references to other traces. How you connect a batch job to the many requests that enqueued its work.

## Who produced each span

Every span records the **InstrumentationScope** that emitted it. That turns an abstract ownership question into something you can read directly off a real trace:

```text
  +   0.0ms  loadgen    POST                          63.48ms  [httpx]
  +   0.5ms  gateway    POST /checkout                62.78ms  [fastapi]
  +   0.9ms  gateway    POST                          61.81ms  [httpx]
  +   1.3ms  orders     POST /orders                  61.17ms  [fastapi]
  +   1.5ms  orders     fraud.check                    6.36ms  [otelkit]   <-- hand-written
  +   8.0ms  orders     POST                          53.13ms  [httpx]
  +   8.3ms  orders     POST                           2.46ms  [httpx]
  +   8.7ms  payments   POST /charge                  52.30ms  [fastapi]
  +   8.8ms  inventory  POST /reserve                  1.87ms  [fastapi]
  +   9.0ms  inventory  GET                            0.16ms  [redis]
  +   9.2ms  inventory  SELECT                         0.16ms  [asyncpg]
  +   9.5ms  inventory  SETEX                          0.11ms  [redis]
  +   9.7ms  inventory  UPDATE                         0.58ms  [asyncpg]
  +  61.6ms  orders     INSERT                         0.46ms  [asyncpg]
```

Twenty-nine of those thirty spans came from `fastapi`, `httpx`, `redis` and `asyncpg` instrumentation that nobody wrote. One came from `otelkit` — a developer's deliberate choice. That ratio is the 80/20 split, made concrete.

## The 20%: what developers actually add

Three things, and nothing else.

**Business attributes on the existing span.** Auto-instrumentation made the span; you say what it was *about*:

```python
set_attrs(**{
    "order.id": order_id,
    "customer.tier": req.tier,
    "order.value_usd": amount,
})
```

This is what makes "are enterprise customers slow?" answerable without a deploy.

**Spans around work that crosses no boundary.** `fraud_check` is a pure function — no socket, no library to patch, structurally invisible to auto-instrumentation. If it became the slow part of checkout, one decorator is the only reason you'd ever know:

```python
@traced("fraud.check", component="risk")
async def fraud_check(...): ...
```

**Error status on failures.** This one is load-bearing and routinely missed:

```python
span.record_exception(exc)                              # stores the stack trace
span.set_status(Status(StatusCode.ERROR, str(exc)))     # makes it COUNT as an error
```

`record_exception` alone leaves the span **green**. Tempo, the service graph, and every span-derived error metric would all under-report. `otelkit.record_error()` does both so it can't be half-done.

### The thing that is not an error

`payments` returns `402 card declined` and `503 processor unreachable`. Only one is an incident. We deliberately record the 402 as a *span event*, not an error:

```python
add_event("payment.declined", order_id=order_id)   # business outcome
record_error(exc, **{"failed.dependency": name})   # genuine failure
```

Encode this at instrumentation time or you cannot recover it later. An error rate that counts declined cards pages you for customers mistyping card numbers. Phase 5 depends entirely on this distinction being right here.

## Semantic conventions

OTel standardises attribute names: `http.request.method`, `http.response.status_code`, `db.system.name`, `server.address`. Use them for anything standard — dashboards, processors and backends all key off these names, and inventing `my_http_status` means nothing understands it.

Use your **own namespace** for business attributes: `order.*`, `customer.*`, `fraud.*`. Never put business data in an `http.*` key.

One caution: semconv has churned. `http.status_code` became `http.response.status_code`; `db.system` became `db.system.name`. Instrumentation libraries migrate at different rates — this lab's spans currently carry the older `http.status_code`. Always check what your spans *actually* contain rather than what the current spec says they should.

## Context propagation, and how it breaks

Four services shared one trace ID via a single HTTP header:

```text
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             ^  trace-id                         parent span-id   ^ sampled
```

The client instrumentation injects it, the server instrumentation extracts it. That's the entire mechanism — which tells you every way it breaks:

- **A proxy strips unknown headers.** Most common cause in the wild.
- **A message queue.** HTTP instrumentation can't help; context must be written into message metadata and read back out explicitly.
- **A thread pool or `asyncio.create_task`.** Context lives in a context-local. Hand work to another worker without carrying it and the child becomes a new root.
- **A background task started after the response.** Same cause.
- **A service with no instrumentation in the middle.** Breaks the chain even if both ends are instrumented.

Symptom in all cases: traces that look "chopped into pieces", many roots where you expected one. When you see that, suspect propagation before suspecting the backend.

## Sampling

Two families, and the distinction matters enormously for cost.

**Head sampling** decides at the root span, before anything is known about how the request turns out. Cheap — unsampled requests cost nothing. Configured by env var:

```bash
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1          # keep 10%
```

`parentbased_*` means "respect the upstream decision, and only decide for yourself if you're the root". This is what keeps a trace whole: without it, each service samples independently and you get fragments of traces everywhere.

The fatal flaw: at 10%, you keep 10% of your errors too. The one trace you actually need during an incident is 90% likely to have been thrown away.

**Tail sampling** buffers complete traces and decides afterwards — keep *all* errors, keep everything slower than 2s, keep 1% of the boring successes. Strictly better decisions, at the cost of memory and the requirement that one collector sees the whole trace. It lives in the collector, not the app, which is why it's a Phase 4 topic.

The usual production answer is head-sample lightly or not at all, then tail-sample in the gateway collector.

## Querying: TraceQL

```traceql
{ .customer.tier = "enterprise" }                    all enterprise traffic
{ .order.value_usd > 500.0 }                         high-value orders
{ .cache.hit = false }                               cache misses only
{ name = "fraud.check" && .fraud.score > 0.9 }       risky orders
{ resource.service.name = "payments" } | avg(duration)
```

`resource.*` matches resource attributes (per-process), `span.*` or a bare `.` matches span attributes.

**A gotcha that will waste your afternoon:** TraceQL is type-strict and fails *silently*. `order.value_usd` is stored as a double, so:

```traceql
{ .order.value_usd > 0 }        -> 0 results    (integer literal, no match)
{ .order.value_usd > 500.0 }    -> 20 results   (float literal, matches)
```

No error, no warning — just an empty result that looks like "no such data". When a query returns nothing you believe should match, check the attribute's actual type in the raw span output before concluding anything.

## The result: why averages lie

`inventory` records `cache.hit` on every request. Splitting the same endpoint by that one attribute:

```text
cache HIT      n=40   p50=  2.72ms  p95=  4.76ms
cache MISS     n=40   p50=  3.87ms  p95=  6.05ms
BLENDED        n=80   p50=  3.37ms  p95=  5.78ms

mean of blended population: 3.36ms
...which describes neither: hits mean 2.67ms, misses mean 4.05ms
```

One endpoint, two populations, and the blended average describes neither of them. The gap is modest here only because Postgres is local and unloaded; set `INVENTORY_SLOW_QUERY_MS=400` and it becomes a chasm. This is the argument for histograms over averages that Phase 2 builds on, and the reason `cache.hit` is the highest-value attribute in the whole service.

## Two traps we hit building this

**The collector reads its config once, at startup.** The config is a bind mount, so editing it on disk changes nothing in the running process, and `docker compose up -d` won't recreate a container whose *compose definition* didn't change. Symptom: a new exporter silently absent. Diagnose it by asking the collector what it actually loaded:

```bash
docker compose logs otel-agent   | grep -oE '"otelcol.component.id": "[^"]+"' | sort -u
docker compose logs otel-gateway | grep -oE '"otelcol.component.id": "[^"]+"' | sort -u
```

If your exporter isn't in that list, it isn't running, no matter what the file says. `docker compose restart otel-agent otel-gateway` after every config edit.

> At this phase there was a single `otel-collector` service. Phase 4 splits it into `otel-agent` and `otel-gateway`, and the compose file in this repo is the final, split version — so the commands above name both tiers.

**Verify your telemetry describes reality.** An earlier version of `inventory` did `SETEX` then `DEL` on every request, so the cache never served anything and `cache.hit` was *always* false. The traces were perfectly accurate — they faithfully reported a cache that did nothing. Telemetry tells you what the code does, not what you meant it to do. Checking `redis-cli DBSIZE` returned 0 is what actually found it.

## Exercises

1. **Find a rejected order.** Query `{ name = "fraud.check" && .fraud.score > 0.98 }`, open the trace, find the `order.rejected` span event and read its attributes.
2. **Break propagation deliberately.** In `orders`, wrap one of the fan-out calls in `asyncio.create_task` without awaiting it in context. Rebuild and watch the trace split into two roots.
3. **Feel head sampling's flaw.** Set `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.05` in `lab/.env`, recreate, let traffic run, then try to find a 403 rejection. Most are gone. Now imagine that during an incident.
4. **Prove the service graph comes from span kinds.** Open Grafana's Service Graph for Tempo and map each edge back to a CLIENT/SERVER span pair.
5. **Read raw OTLP again.** Set the collector's debug `verbosity` back to `detailed`, restart it, and run `tools/waterfall.py`. Confirm the `fraud.check` span appears with `otelkit` as its scope.
