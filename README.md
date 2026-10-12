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

telemetry:  apps -> otel-agent -> otel-gateway -> tempo / prometheus / loki
                    (per host)    (scaled: tail sampling, PII scrub,
                                   noise filter, span metrics)
```

Four small Python services, about 120 lines each — small enough to read in full, with a topology rich enough to produce every trace shape you'll meet in production: a fan-out, a database, a cache, and a real failure surface.

The application code contains **zero OpenTelemetry imports**. That is deliberate and it is the Phase 0 lesson.

## Phases

| # | Phase | Stack added | Doc |
|---|---|---|---|
| 0 | Mental model + lab skeleton | collector -> stdout | [00-mental-model.md](docs/00-mental-model.md) **done** |
| 1 | Tracing | + Tempo, Grafana | [01-tracing.md](docs/01-tracing.md) **done** |
| 2 | Metrics | + Prometheus | [02-metrics.md](docs/02-metrics.md) **done** |
| 3 | Logs | + Loki | [03-logs.md](docs/03-logs.md) **done** |
| 4 | The Collector in depth | agent + gateway tiers | [04-collector.md](docs/04-collector.md) **done** |
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
                (spans, metrics, structured trace-aware logging)
lab/
  .env          chaos knobs + the observability switch
  docker-compose.yml
  collector/    agent.yaml (per host) + gateway.yaml (scaled)
  postgres/
docs/           one lesson per phase
chaos/          Phase 6 failure scenarios
tools/
  waterfall.py       rebuild a trace waterfall from raw collector stdout
  golden_path.py     walk log -> trace -> logs from the terminal
  gen_dashboards.py  generate the Grafana dashboards
```

## Where to look

| | |
|---|---|
| Grafana | <http://localhost:3000> (anonymous admin, lab only) |
| Prometheus | <http://localhost:9090> · [cardinality](http://localhost:9090/tsdb-status) |
| Tempo API | <http://localhost:3200> |
| Loki API | <http://localhost:3100> |
| Collector (gateway) | <http://localhost:9464/metrics> app+span metrics · <http://localhost:8889/metrics> self |
| Collector (agent) | <http://localhost:8888/metrics> self · health <http://localhost:13133> |
| zpages | agent <http://localhost:55679/debug/pipelinez> · gateway <http://localhost:55680/debug/pipelinez> |
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

# pipeline integrity — the four numbers that matter
sum by (tier) (rate(otelcol_receiver_refused_spans[1m]))         # data loss NOW
sum(rate(otelcol_exporter_send_failed_spans[1m]))
max(otelcol_exporter_queue_size)
sum(rate(otelcol_exporter_sent_spans{exporter="otlp/tempo"}[2m]))
  / sum(rate(otelcol_receiver_accepted_spans{tier="gateway"}[2m]))  # sampling keep-rate
```

## Dashboards

Four, generated by `tools/gen_dashboards.py` rather than hand-written JSON — a dashboard clicked together in a browser exists only in one person's history.

| Dashboard | For |
|---|---|
| [RED overview](http://localhost:3000/d/red-overview) | The daily driver — RED per service, business metrics, log volume, live errors |
| [Service detail](http://localhost:3000/d/service-detail) | Templated by `$service`: its RED, its dependencies, its logs |
| [Golden path](http://localhost:3000/d/golden-path) | The drill-down laid out top to bottom: symptom → service → log line |
| [Telemetry pipeline health](http://localhost:3000/d/pipeline-health) | The collector watching itself — refused spans, queue depth, memory |

```bash
python3 tools/gen_dashboards.py
docker compose -f lab/docker-compose.yml restart grafana
```

## Walk the golden path from the terminal

```bash
python3 tools/golden_path.py
```

Picks a recent error log, proves the same `trace_id` resolves in Tempo, and pulls back every log line for that one request across all services. If this works, correlation is wired correctly.

Some LogQL for Grafana:

```logql
{service_namespace="shop"} | severity_text="ERROR"
{service_namespace="shop"} | trace_id="<id>"            every line for ONE request
{service_name="orders"} | customer_id="cust-0126"       everything one customer did
sum by (service_name) (rate({service_namespace="shop"}[1m]))
```

## Reading raw OTLP

You can still reconstruct what Tempo shows you from the collector's stdout alone:

Read it at the **agent**, where data is still unfiltered and unsampled:

```bash
cd lab
# add a debug exporter to the agent's traces pipeline, then:
docker compose restart otel-agent
docker compose logs otel-agent --since 2m --no-log-prefix | python3 ../tools/waterfall.py
```

The gateway declares a `debug` exporter but deliberately leaves it out of every pipeline — a live reminder that declaring a component does nothing; only `service.pipelines` activates it.

Do this until it stops feeling like magic. Every later UI is just a faster version of this.

**Bind-mounted config is read once, at startup.** This applies to the collector *and* Grafana — editing the file does nothing until the container restarts, and `docker compose up -d` will not recreate a container whose compose definition has not changed. Symptom: a new exporter, datasource or dashboard that silently never appears.

```bash
docker compose restart otel-agent otel-gateway   # after collector/*.yaml
docker compose restart grafana                   # after provisioning/ or dashboards/
docker compose restart prometheus                # after prometheus.yml

# what a collector ACTUALLY loaded:
docker compose logs otel-gateway | grep -oE '"otelcol.component.id": "[^"]+"' | sort -u

# exact component IDs available in this build:
docker run --rm otel/opentelemetry-collector-contrib:0.162.0 components
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
