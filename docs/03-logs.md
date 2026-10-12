# Phase 3 — Logs

Stack added: **Loki**. All three signals now have a real backend, all three carry the same `trace_id`, and Grafana knows how to walk between them.

This phase is short on new concepts and long on payoff: it is the one that makes the other two useful.

## Loki ingests OTLP natively

There is no Loki-specific exporter in the collector any more. Loki 3.x accepts OTLP at `/otlp`, so this is a plain `otlphttp` exporter:

```yaml
exporters:
  otlphttp/loki:
    endpoint: http://loki:3100/otlp
```

Older guides reference a `loki` exporter with label hints like `loki.attribute.labels`. That component has been removed. If you find a tutorial using it, the tutorial is out of date.

## Loki has Prometheus's cardinality problem, wearing a different hat

An **index label** in Loki defines a *stream*. Every unique label combination gets its own index entry and its own chunks. High-cardinality labels are exactly as ruinous here as in Prometheus — the failure mode is just slower and uglier.

And Loki ships a landmine: **out of the box it promotes 17 OTLP resource attributes to index labels, one of which is `service.instance.id`** — a UUID regenerated on every process start. Leave that alone and every container restart permanently creates a fresh set of streams, forever. On Kubernetes, where pods churn constantly, this is a slow-motion outage.

So the config turns the defaults off and promotes only what we actually query by:

```yaml
limits_config:
  otlp_config:
    resource_attributes:
      ignore_defaults: true
      attributes_config:
        - action: index_label
          attributes:
            - service.name
            - service.namespace
            - deployment.environment
```

Verified afterwards:

```
$ curl -s localhost:3100/loki/api/v1/labels
index labels: ['deployment_environment', 'service_name', 'service_namespace']
```

Three labels. `service_instance_id` is still *present and searchable* — it just lands as **structured metadata** instead of an index label. That is the key distinction:

- **Index label** — defines a stream, cheap to filter on, expensive to have many of.
- **Structured metadata** — attached per log line, still queryable with `| key="value"`, costs nothing in stream cardinality.

High-cardinality things belong in structured metadata. Which is exactly where `trace_id`, `order_id` and `customer_id` ended up, without us configuring anything.

## Structured logging, and getting trace context in for free

Two properties make logs worth having:

**They must be structured.** `grep` across 40 services is not an investigation strategy. The change is small:

```python
# before — context welded into a string, only findable by substring match
log.info("order placed order_id=%s customer_id=%s amount=%.2f", order_id, cid, amount)

# after — context as fields
log.info("order placed", extra={"order_id": order_id, "customer_id": cid,
                                "amount_usd": amount, "tier": tier, "sku": sku})
```

Those `extra` keys flow automatically into OTLP log attributes and arrive in Loki as structured metadata you can filter on. No mapping config anywhere.

**They must carry trace_id.** `otelkit.TraceContextJSONFormatter` reads the active span and stamps every line:

```json
{"ts": "2026-10-12T17:28:55.820Z", "level": "INFO", "logger": "orders",
 "msg": "order placed",
 "trace_id": "fb3f50ddca98ef6e5b91eb5b2983ae0a", "span_id": "1a21800de28dbcc9",
 "order_id": "8a14f583-...", "customer_id": "cust-0126",
 "amount_usd": 68.5, "tier": "standard", "sku": "SKU-FILTER"}
```

`format(ctx.trace_id, "032x")` is the canonical rendering — the same lowercase hex Tempo and Loki use, so the IDs match exactly across all three signals. Get the formatting wrong and everything looks fine until no link ever resolves.

### One trap that will bite you

```python
logging.basicConfig(force=True)   # DO NOT
```

Auto-instrumentation has already attached its own `LoggingHandler` to the root logger, and that handler is what ships log records over OTLP. `force=True` wipes the handler list, silently killing log export while stdout keeps looking perfectly healthy. `setup_logging()` removes only plain `StreamHandler`s and leaves anything from the `opentelemetry` package alone.

### Do not log what you already trace

Turning on auto-instrumentation made `httpx` emit a line per outbound request:

```
{"msg": "HTTP Request: POST http://payments:8000/charge \"HTTP/1.1 200 OK\"", "trace_id": "..."}
```

Every one of those duplicates a span that already exists, with less detail. Logs are usually the largest line on an observability bill, so this is pure cost for no signal. `setup_logging()` raises `httpx`, `httpcore`, `uvicorn.access` and `asyncio` to WARNING — keeping the failures, dropping the narration.

## LogQL

```logql
{service_name="orders"}                                  a stream selector — ALWAYS start here
{service_namespace="shop"} | severity_text="ERROR"       filter by structured metadata
{service_namespace="shop"} | trace_id="fb3f50dd..."      every line for ONE request
{service_name="orders"} | customer_id="cust-0126"        everything one customer did
{service_name="payments"} |= "declined"                  plain substring match

sum by (service_name) (rate({service_namespace="shop"}[1m]))                    log volume
sum by (service_name) (rate({service_namespace="shop"} | severity_text="ERROR" [1m]))
topk(5, sum by (msg) (count_over_time({service_namespace="shop"} | severity_text="ERROR" [5m])))
```

The stream selector `{...}` is mandatory and must use index labels. Everything after `|` filters within the selected streams. A selector that is too broad is slow; one that uses a high-cardinality label is worse.

## The triangle, closed

Four links in `datasources.yaml` turn three databases into one investigation surface:

| Direction | Mechanism |
|---|---|
| metrics → traces | `exemplarTraceIdDestinations` on Prometheus |
| traces → logs | `tracesToLogsV2` on Tempo |
| traces → metrics | `tracesToMetrics` on Tempo |
| logs → traces | `derivedFields` on Loki |

Verified from the command line with `tools/golden_path.py`:

```
STEP 1 — a log line  (Loki)
  service   : payments
  level     : WARN
  message   : card declined
  structured: {'amount_usd': '12', 'customer_id': 'cust-0121',
               'order_id': 'b96c31dd-...', 'tier': 'premium'}
  trace_id  : 19b2112a4f9677f5ec4cd470885bdf64

STEP 2 — the SAME trace_id resolves in Tempo
  25 spans across 5 services
    +  0.0ms  loadgen    POST                      64.98ms
    +  1.1ms  gateway    POST /checkout            63.38ms
    +  3.0ms  orders     POST /orders              59.56ms
    +  3.5ms  orders     fraud.check                4.28ms
    + 10.3ms  payments   POST /charge              50.40ms

STEP 3 — every log line for that one request, across all services
  payments   WARN  card declined
```

One ID, three backends, no manual correlation. That is the deliverable.

## Dashboards

Four, generated by `tools/gen_dashboards.py` rather than hand-written JSON — a dashboard clicked together in a browser exists only in one person's history. These are code, reviewable in a diff, and every panel carries a description explaining why it exists.

| Dashboard | For |
|---|---|
| **RED overview** | The daily driver. RED per service, business metrics, log volume, live errors. |
| **Service detail** | Templated by `$service`. Its RED, its outbound dependencies, its logs. Where you land after the overview tells you which service. |
| **Golden path** | The drill-down itself, laid out top to bottom: symptom → which service → the actual log lines. |
| **Telemetry pipeline health** | The collector watching itself. Refused spans, queue depth, batch sizes, memory. |

That last one matters more than it looks. **A collector silently dropping data looks exactly like a quiet system.** `otelcol_receiver_refused_spans` above zero means you are losing telemetry, and nothing else on any other dashboard will tell you.

All 44 panel queries were verified to return data through Grafana's own API before this was committed.

Regenerate after editing:

```bash
python3 tools/gen_dashboards.py && docker compose -f lab/docker-compose.yml restart grafana
```

## A trap that generalises

Phase 1 established that the collector reads its config once at startup. **Grafana does exactly the same with provisioning**, and we hit it again: edited `datasources.yaml`, ran `docker compose up -d grafana`, and compose declined to recreate a container whose definition had not changed. The Loki datasource simply never appeared — no error, just two datasources where three were expected.

The general rule for this lab: **after editing any bind-mounted config, `restart` the container that reads it.** `up -d` is not enough.

```bash
docker compose restart otel-collector   # after collector/config.yaml
docker compose restart grafana          # after provisioning/ or dashboards/
docker compose restart prometheus       # after prometheus.yml (or use /-/reload)
```

## Exercises

1. **Walk the path in the UI.** Open the Golden path dashboard, click an exemplar dot on the p99 panel, land in Tempo, click a span, select "Logs for this span", and read the log line. Then do the reverse: find an error log, click its TraceID link.
2. **Follow one customer.** `{service_namespace="shop"} | customer_id="cust-0126"` — everything that customer did, across every service. Try doing that with unstructured logs.
3. **Break correlation on purpose.** Remove `trace_id` from `TraceContextJSONFormatter`, rebuild, and watch the Tempo→Loki link return nothing. The logs are still there and still perfectly readable, and they are now useless for an incident.
4. **Find the cardinality landmine.** Set `ignore_defaults: false` in `loki.yaml`, restart Loki, then check `/loki/api/v1/labels`. Count the labels. Restart a service a few times and watch `service_instance_id` accumulate values.
5. **Measure the noise you removed.** Set `httpx` back to INFO and compare `sum(rate({service_namespace="shop"}[1m]))` before and after. That multiple is what logging-what-you-trace costs.
