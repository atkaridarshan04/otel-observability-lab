# Phase 0 — The mental model

Everything in this course hangs off a small number of ideas. Get these right and the YAML is just typing; get them wrong and no amount of tooling helps.

## 1. Monitoring answers known questions. Observability answers new ones.

A dashboard you built last month answers a question you already knew to ask: "is CPU high?", "is the queue deep?". That is monitoring, and it is fine — until the outage is caused by something nobody predicted.

Observability is the property that you can ask a *brand new* question of your system, right now, without shipping code to answer it. "Are enterprise-tier customers in eu-west seeing slow checkouts, but only for orders containing a backordered SKU?" Nobody builds that dashboard in advance. You need the raw telemetry to be rich enough that the question is answerable after the fact.

The practical test: when something breaks in a way you have never seen before, can you find out why without adding a `print` and redeploying? If no, you have monitoring.

## 2. Three signals, and what each is actually for

| Signal | Answers | Cost shape | Where it lies to you |
|---|---|---|---|
| **Metrics** | *Is something wrong, and how bad?* | Cheap and flat — cost scales with **number of unique label combinations**, not traffic | Aggregates hide individuals. A fine p50 can hide 2% of users getting 30s timeouts |
| **Traces** | *Where in the request did it go wrong?* | Expensive — cost scales with traffic, hence sampling | Sampled data can miss the rare event you care about (fixed by tail sampling) |
| **Logs** | *What exactly happened, in detail?* | Most expensive at scale, by volume | No structure by default; grep across 40 services is not an investigation strategy |

The reason you need all three is that they answer *different questions in sequence*. Metrics say "checkout error rate jumped to 4%". Traces say "it's the payments call, and only on the retry path". Logs say "connection pool exhausted, waited 4012ms".

Trying to do all three jobs with one signal is the most common and most expensive mistake. Logs-only shops pay enormous bills to compute metrics the hard way. Metrics-only shops can tell you something is broken but never where.

## 3. The actual value is the *join* between them

This is the idea most tutorials skip, and it is the whole game.

Each signal carries a **trace ID**. A metric can carry an **exemplar** — a pointer to one representative trace that landed in that histogram bucket. A log line can carry `trace_id` and `span_id`. Once all three share that key, you get the drill-down:

```text
alert: checkout error budget burning fast
  -> dashboard: errors concentrated in `orders`
    -> exemplar on the p99 latency bucket: one specific slow trace
      -> trace waterfall: orders -> inventory span took 4.1s
        -> logs filtered by that trace_id:
             ERROR inventory connection pool exhausted, waited 4012ms
```

Five clicks from "something is wrong" to the exact line of evidence. That path is what we are building. **If a piece of telemetry does not help you move along that path, it is cost without value.** Use that as your filter for every decision in this course.

## 4. What OpenTelemetry actually is

OTel is deliberately four separable things. People conflate them and get confused.

**The specification and data model.** A vendor-neutral definition of what a span, metric and log record *are*, plus the OTLP wire protocol. This is the part that matters most, and it is why OTel won: your instrumentation is no longer owned by your vendor.

**The API.** What application code calls — `tracer.start_as_current_span(...)`. Critically, the API is a no-op if no SDK is installed. A library can instrument itself with the OTel API and impose zero cost on users who don't opt in. That's why instrumentation is increasingly built into frameworks rather than bolted on.

**The SDK.** The implementation that actually records, samples, batches and exports. Configured almost entirely through `OTEL_*` environment variables — which is what makes it a DevOps-controllable surface rather than a dev one.

**The Collector.** A standalone binary that receives, processes and re-exports telemetry. This is the piece you will spend the most time on, and Phase 4 is dedicated to it.

### Why the Collector exists at all

You could export straight from the app to your backend. You shouldn't, for reasons that are all operational:

- **Change the backend without redeploying anything.** Switching vendors becomes a collector config change, not a fleet-wide redeploy.
- **Enrich centrally.** Add `k8s.pod.name`, `cloud.region`, `deployment.environment` in one place instead of in every service.
- **Scrub centrally.** Strip PII, drop high-cardinality attributes, enforce policy once rather than trusting 40 teams.
- **Buffer and retry.** The backend going down becomes a queue to drain, not dropped data and app-side errors.
- **Sample intelligently.** Tail sampling needs to see a complete trace, which no single application process ever does.
- **Control cost.** Drop health-check spans, aggregate before export, reduce cardinality — all without touching application code.

Every one of those is a thing you, on the DevOps side, want to control without filing tickets against application teams.

## 5. The data model, concretely

Three nested layers, which you will see literally in the Phase 0 stdout:

**Resource** — *who* is emitting. `service.name`, `service.version`, `deployment.environment`, `host.name`, `k8s.pod.name`. Attached once per process and inherited by every span, metric and log it produces. Set via `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_SERVICE_NAME`.

**InstrumentationScope** — *which library* produced this. Lets you tell framework-generated spans from your own.

**The record itself** — the span, data point, or log record, carrying its own **attributes** (key-values specific to this one event, like `http.response.status_code` or `order.id`).

The Resource/attribute distinction matters for cost: resource attributes are deduplicated per-process and nearly free; record attributes multiply, and on *metrics* they multiply into cardinality, which is where the money goes.

## 6. Context propagation is the trick that makes traces distributed

A trace spans processes. Nothing magical connects them — it's an HTTP header, standardised by W3C:

```text
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             ^  ^                                ^                ^
             |  trace-id (16 bytes)              parent span-id   flags (01 = sampled)
             version
```

The client-side instrumentation injects it; the server-side instrumentation extracts it and makes the incoming span a child. That is the entire mechanism.

Which also tells you the main failure mode: **anything that drops or doesn't forward headers breaks your traces.** A proxy that strips unknown headers, a queue you publish to without carrying context, a thread pool that loses the context-local, a background task spawned without propagation — you get orphan traces. When traces look "broken into pieces", this is almost always why.

## 7. Where symptom-based monitoring fits

Classic alerting is **cause-based**: CPU > 80%, memory > 90%, disk filling. The problems are well known — these fire constantly when nothing is actually wrong (an efficient service *should* use its CPU), and they stay silent during real outages that have a novel cause.

**Symptom-based** alerting means paging on what the *user experiences*: requests failing, requests slow, work not completing. The cause-level data still gets collected — it is what you look at *after* the page, during diagnosis — but it is not what wakes a human at 3am.

The standard vocabulary:

- **RED** (for request-driven services): **R**ate, **E**rrors, **D**uration.
- **USE** (for resources like CPU, disk, pools): **U**tilisation, **S**aturation, **E**rrors.
- **The four golden signals** (Google SRE): latency, traffic, errors, saturation — essentially RED plus saturation.

Phase 5 turns this into real alert rules, including the multi-window multi-burn-rate pattern that gives you fast pages for severe burns and slow tickets for gradual ones, without the flapping that naive thresholds produce.

One early and important nuance, already baked into this lab's code: **not every error is an incident.** Our `payments` service returns `402 card declined` (a normal business outcome, possibly thousands per day) and `503 payment processor unreachable` (a genuine incident). An alert that counts all non-2xx responses will page you for customers mistyping their card numbers. Distinguishing these is an instrumentation decision made at design time, not something you can fix later in PromQL.

## 8. Who owns what

Auto-instrumentation covers roughly 80% — inbound/outbound HTTP, database calls, cache calls, queue operations, and the context propagation between them — with no application code change at all. The remaining 20% is business context, which no agent can infer.

| | Owns |
|---|---|
| **Platform / DevOps** | Collector and pipelines, sampling policy, resource attributes, backend choice, SLOs and alert rules, the injection mechanism, SDK config via env vars, **cardinality governance** |
| **Application developers** | Business attributes (`order.id`, `customer.tier`), custom spans around domain logic, business metrics, `record_exception` on real failures |

The healthy pattern: platform ships auto-instrumentation plus a thin internal helper library, so a developer adds observability in one line instead of twenty. We build that helper in Phase 1.

---

## Exercise

Run the lab with `OTEL_ENABLED=false`, confirm the app works and produces nothing. Flip to `true`, recreate, and read the raw OTLP in the collector's stdout. Find, by eye:

1. The **Resource** block, and the attributes you set via `OTEL_RESOURCE_ATTRIBUTES`.
2. A **server span** in `orders` and the **client spans** it produced for payments and inventory.
3. The **same trace ID** appearing in all five services.
4. The **parent span ID** on a child span, and match it to its parent.
5. A span whose duration you can explain from reading the application code.

When you can do all five from raw stdout, the UIs in later phases stop being magic.

---

## What Phase 0 actually produced

Reconstructed with `tools/waterfall.py` from nothing but the collector's stdout — no Tempo, no UI, and no OpenTelemetry import anywhere in `app/`:

```text
trace 9cab0b4fb3e9501bddf902dc3ba3a920   29 spans   5 services   89.8ms total

loadgen  POST                                        |##############################################|    89.8ms  Client
  gateway  POST /checkout                            |    ######################################### |    80.3ms  Server
    gateway  POST                                    |     ######################################## |    78.4ms  Client
      orders  POST /orders                           |         #################################### |    70.4ms  Server
        orders  POST                                 |         ##################################   |    66.5ms  Client
          payments  POST /charge                     |             ##############################   |    59.3ms  Server
        orders  POST                                 |            ###                               |     7.1ms  Client
          inventory  POST /reserve                   |             ##                               |     5.1ms  Server
            inventory  GET                           |             #                                |     1.4ms  Client
            inventory  SELECT                        |              #                               |     0.8ms  Client
            inventory  SETEX                         |               #                              |     0.1ms  Client
            inventory  UPDATE                        |               #                              |     0.9ms  Client
            inventory  DEL                           |               #                              |     0.1ms  Client
        orders  INSERT                               |                                            # |     1.5ms  Client
```

Five things worth staring at:

**The fan-out is visible as parallelism.** The two `orders POST` client spans start at the same offset. Payments takes 59.3ms, inventory 7.1ms, and the parent is bounded by the *slower* one. You can read concurrency off a waterfall — if those bars were sequential, you'd have found a bug by looking at a picture.

**Nobody wrote the database spans.** `GET`, `SELECT`, `SETEX`, `UPDATE`, `DEL` — Redis and Postgres calls, each with timing. `app/inventory/main.py` contains no instrumentation. That is auto-instrumentation monkey-patching the `redis` and `asyncpg` client libraries.

**The cache-miss path is legible.** `GET` (miss) then `SELECT` (fall back to Postgres) then `SETEX` (populate cache). A cache *hit* produces a visibly shorter, differently-shaped trace. Same endpoint, two populations, one average that describes neither — this is the Phase 2 histogram argument, drawn for you.

**Context crossed four process boundaries** on nothing but the `traceparent` header. Nothing in the app arranged for that.

**There is already noise.** Those `http receive` / `http send` spans are ASGI protocol events — real, but useless for debugging, and in production they'd be a meaningful fraction of your spend. Dropping them is a *collector* concern, not an application one, and we do exactly that in Phase 4. Noticing that telemetry has a cost, this early, is the right instinct.
