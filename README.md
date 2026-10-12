# Learning OpenTelemetry, from scratch, the DevOps way

A hands-on course built around one small application that we deliberately break, observed by a real local stack.

The guiding principle: **nothing is introduced until a problem demands it.** No tool appears in this repo before the phase where you feel its absence.

## The golden path

Everything we build exists to make this five-step drill-down possible:

```
symptom alert fires           "users are seeing checkout errors"
  -> RED dashboard            which service is responsible?
    -> exemplar               jump from the latency histogram to one real trace
      -> trace waterfall      which span is slow, and why?
        -> logs by trace_id   the actual line: "pool exhausted, waited 4012ms"
```

If a piece of telemetry doesn't help you travel that path, it's cost without value. That is the filter for every decision here.

## The system under observation

```
loadgen -> gateway -> orders -+-> payments
                              +-> inventory -> postgres
                                            -> redis
```

Four small Python services, about 120 lines each — small enough to read in full, with a topology rich enough to produce every trace shape you'll meet in production: a fan-out, a database, a cache, and a real failure surface.

The application code contains **zero OpenTelemetry imports**. That is deliberate and it is the Phase 0 lesson.

## Phases

| # | Phase | Stack added | Doc |
|---|---|---|---|
| 0 | Mental model + lab skeleton | collector -> stdout | [00-mental-model.md](docs/00-mental-model.md) **done** |
| 1 | Tracing | + Tempo, Grafana | [01-tracing.md](docs/01-tracing.md) **done** |
| 2 | Metrics | + Prometheus | [02-metrics.md](docs/02-metrics.md) **done** |
| 3 | Logs | + Loki | _pending_ |
| 4 | The Collector in depth | — | _pending_ |
| 5 | Symptom-based alerting | + Alertmanager | _pending_ |
| 6 | Break it on purpose | — | _pending_ |
| 7 | Lift to Kubernetes | kind + OTel Operator | _pending_ |

## Quick start

Requires Docker Desktop to be running.

```bash
cd lab

# 1. Start with observability OFF. This is your "before" picture.
docker compose up -d --build
docker compose logs -f otel-collector     # silence. nothing is being sent.
curl -s localhost:8080/health             # the app works fine without telemetry.

# 2. Flip ONE environment variable. Change no code.
sed -i '' 's/OTEL_ENABLED=false/OTEL_ENABLED=true/' .env
docker compose up -d --force-recreate

# 3. Read the raw OTLP data model.
docker compose logs -f otel-collector
```

Step 2 is the entire argument for auto-instrumentation: a service that emitted nothing now emits full distributed traces, metrics and logs, and not one line of `app/` changed. The switch lives in `app/run.sh` and is controlled from `lab/.env` — a DevOps lever, not a developer one.

## Layout

```
app/            the services. business code + a thin layer of business spans.
  run.sh        the OTEL_ENABLED switch - read this file first
  otelkit/      the internal telemetry helper a platform team would ship
lab/
  .env          chaos knobs + the observability switch
  docker-compose.yml
  collector/    the config we iterate on all course
  postgres/
docs/           one lesson per phase
chaos/          Phase 6 failure scenarios
tools/
  waterfall.py  rebuild a trace waterfall from raw collector stdout
```

## Where to look

| | |
|---|---|
| Grafana | <http://localhost:3000> (anonymous admin, lab only) — RED overview dashboard |
| Prometheus | <http://localhost:9090> · [cardinality](http://localhost:9090/tsdb-status) |
| Tempo API | <http://localhost:3200> |
| Collector metrics | <http://localhost:9464/metrics> (apps) · <http://localhost:8888/metrics> (itself) |
| The app | <http://localhost:8080> |

Some TraceQL to paste into Grafana's Explore:

```traceql
{ .customer.tier = "enterprise" }
{ .order.value_usd > 500.0 }
{ .cache.hit = false }
{ name = "fraud.check" && .fraud.score > 0.9 }
```

Some PromQL for Prometheus or Grafana:

```promql
sum by (job) (rate(http_server_duration_milliseconds_count[1m]))
sum by (job) (rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[1m]))
histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_milliseconds_bucket[1m])))
sum by (tier) (rate(orders_placed_total[1m]))
topk(10, count by (__name__)({__name__=~".+"}))          # cardinality offenders
```

## Reading raw OTLP

You can still reconstruct what Tempo shows you from the collector's stdout alone:

```bash
cd lab
# waterfall.py needs full attribute output, which Phase 1 dialled down
sed -i '' 's/verbosity: basic/verbosity: detailed/' collector/config.yaml
docker compose restart otel-collector
docker compose logs otel-collector --since 2m --no-log-prefix | python3 ../tools/waterfall.py
```

Do this until it stops feeling like magic. Every later UI is just a faster version of this.

**The collector reads its config once, at startup.** Editing the bind-mounted file does nothing until you `docker compose restart otel-collector`. To check what is actually loaded:

```bash
docker compose logs otel-collector | grep -oE '"otelcol.component.id": "[^"]+"' | sort -u
```

## Useful commands

```bash
cd lab
docker compose ps                          # what's running
docker compose logs -f otel-collector      # the raw telemetry firehose
docker compose logs -f orders              # one service's app logs
docker compose restart payments            # apply a changed chaos knob
docker compose down -v                     # tear down, including volumes

curl -s localhost:8080/health
curl -s localhost:8080/checkout -H 'content-type: application/json' \
  -d '{"customer_id":"cust-0001","sku":"SKU-TELESCOPE","qty":1,"tier":"premium"}' | jq
```
