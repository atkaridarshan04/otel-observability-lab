# Phase 2 — Metrics

Stack added: **Prometheus**. The collector now re-exposes everything it receives in Prometheus format, and Prometheus scrapes *the collector*.

That direction change is worth a moment. The applications push OTLP to the collector. Prometheus pulls from the collector. **No application has a `/metrics` endpoint, a Prometheus client library, or any knowledge that Prometheus exists.** The collector is the adapter between a push protocol and a pull system, which is why swapping Prometheus for Mimir, Thanos or a vendor is a config change rather than a fleet redeploy.

Grafana now ships a provisioned **RED overview** dashboard. Prometheus is on <http://localhost:9090>.

## The four instrument types

Picking the wrong one is the most common metrics mistake, and it is usually unfixable after the fact.

| Instrument | Use for | Query as |
|---|---|---|
| **Counter** | monotonic totals — requests, errors, orders, bytes | `rate(...)`, never the raw value |
| **UpDownCounter** | values that rise and fall — queue depth, in-flight, pool checkouts | directly |
| **Histogram** | distributions — latency, payload size, order value | `histogram_quantile(...)` |
| **Gauge** | point-in-time samples where only the latest matters — disk free, temperature | directly |

**The decision rule: if you will ever want a p99, it must be a histogram.** You cannot recover a percentile from an average, and there is no migration path — you get to start collecting data again from zero.

One trap worth naming: an UpDownCounter that only ever gets incremented is a memory leak with extra steps. `orders.in_flight` decrements in a `finally` block precisely so error paths cannot strand it.

## Temporality: delta vs cumulative

Every counter and histogram carries a **temporality**, and this is the concept that most confuses people arriving from a Prometheus background.

- **Cumulative** — "47,221 requests since this process started." Restart the process and it resets to zero. Prometheus is natively cumulative and detects resets for you.
- **Delta** — "312 requests since my last export." Stateless and cheap for the SDK, natural for push-based vendors.

```bash
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative   # what this lab uses
```

We send cumulative because it passes straight into Prometheus. Choosing `delta` would require a `deltatocumulative` processor in the collector, and skipping that step produces metrics that look plausible and are wrong. If you are exporting to a vendor like Datadog or New Relic, delta is usually what they want instead.

## Why averages lie, with evidence

Phase 1 measured the same endpoint split by `cache.hit`:

```
cache HIT      p50=2.72ms   p95=4.76ms
cache MISS     p50=3.87ms   p95=6.05ms
BLENDED mean = 3.36ms   (hits mean 2.67ms, misses mean 4.05ms)
```

The blended mean describes neither population. Worse, an average cannot tell you about the tail at all: a service where 98% of requests take 10ms and 2% take 30 seconds has an average of about 610ms, which looks like a mildly slow service rather than one timing out for one user in fifty.

A histogram keeps the shape. `histogram_quantile(0.99, ...)` answers "how bad is it for the unluckiest 1%", which is the number your users actually experience.

**And never average a percentile.** The mean of three instances' p95 values is not the p95 of anything. Always aggregate the *buckets*, then compute the quantile:

```promql
# correct — sum buckets first, quantile last
histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_milliseconds_bucket[1m])))

# meaningless — averaging percentiles
avg by (job) (histogram_quantile(0.95, rate(http_server_duration_milliseconds_bucket[1m])))
```

## Exemplars: the metrics → traces link

A histogram tells you p99 latency hit 2 seconds. It cannot tell you *why*. An exemplar is a pointer stored alongside a bucket, carrying the `trace_id` of one real request that landed in it:

```
http_server_duration_milliseconds_bucket{job="shop/gateway",le="100.0"} 521 \
  # {trace_id="d38781c6a2d456e3b66d1a4687820158",span_id="83639c722f2ec871"} 78.0 1.7918e+09
```

Everything after the ` # ` is the exemplar. That is the click from "p99 is bad" to "here is the exact trace", and it is the single highest-value feature in this whole stack.

Three things must all be true or it silently does not work:

1. **The SDK records them.** `OTEL_METRICS_EXEMPLAR_FILTER=trace_based` (the default) records an exemplar only when a *sampled* span is active — which is also why aggressive head sampling quietly starves your exemplars.
2. **The collector exports them.** `enable_open_metrics: true` on the prometheus exporter. Exemplars exist only in the OpenMetrics exposition format.
3. **Prometheus stores them.** `--enable-feature=exemplar-storage`. Still a feature flag in Prometheus 3.x; without it they are discarded at ingest.

Then Grafana needs `exemplarTraceIdDestinations` on the Prometheus datasource to know that `trace_id` should be looked up in Tempo. Miss any one of the four and you get no error — just no dots on the chart.

## Cardinality: where the money goes

**A time series is created for every unique combination of label values.** Not per request — per combination. This is the one piece of metrics arithmetic worth internalising, because it is how observability bills reach six figures.

We measured it. Baseline, with `orders.*` labelled by `tier` (3 values) and `sku` (5):

```
prometheus TSDB total series: 2,264
  orders_value_bucket                 240
```

Then we added exactly one label, `customer_id`, and drove 500 distinct customers:

```
prometheus TSDB total series: 8,598      <- 3.8x, from ONE label
  orders_value_bucket               4,896
  customer_id: 258 distinct values        <- and only half the customers had been seen
```

The arithmetic, for a single histogram with 16 buckets:

```
tier x sku                              15 combos x 16 =       240 series   bounded, known at design time
tier x sku x customer (500)          7,500 combos x 16 =   120,000 series   unbounded in practice
tier x sku x customer x region (10) 75,000 combos x 16 = 1,200,000 series   the next "harmless" label
```

One histogram. One label. Four orders of magnitude.

**The rule: never put an unbounded value in a metric label.** User IDs, order IDs, email addresses, session IDs, raw URLs with IDs in them, error messages containing a timestamp.

**Where that data belongs is a span.** Phase 1 put `order.id` and `customer.id` on spans, where the cost is linear in traffic and sampling controls it. The same values on a metric are multiplicative and nothing controls them. That is the whole reason both signals exist.

Watch for it with:

```promql
topk(10, count by (__name__)({__name__=~".+"}))
```

or Prometheus's TSDB status page at <http://localhost:9090/tsdb-status>.

A live example in this lab: auto-instrumented HTTP metrics carry an `http_target` label. Our paths are fixed, so it is harmless. Point this instrumentation at `/orders/{id}` without templating and it becomes a cardinality bomb you did not write.

## RED, and what to leave out

```promql
# Rate
sum by (job) (rate(http_server_duration_milliseconds_count[1m]))

# Errors — 5xx ONLY
sum by (job) (rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[1m]))

# Duration
histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_milliseconds_bucket[1m])))
```

The error query deliberately excludes `402` (card declined) and `403` (fraud rejected). Those are business outcomes. An error rate that counts them pages you because customers mistyped card numbers, and no amount of PromQL fixes it later if the instrumentation did not distinguish them. This is the Phase 1 decision paying off.

### Business metrics are not optional

"Is checkout up?" is an HTTP question. "Did revenue stop?" is not:

```promql
sum by (tier) (rate(orders_placed_total[1m]))
sum by (reason) (rate(orders_rejected_total[1m]))
```

A deploy that silently rejects every enterprise order leaves every HTTP metric perfectly green. `orders_rejected_total` is split by reason so a fraud-rule misfire, a payment outage and a genuine stockout are three different lines rather than one undifferentiated number.

## Four traps we hit building this

**The collector's self-telemetry address moved and defaults to localhost.** `service.telemetry.metrics.address` was deprecated in collector v0.123.0 and is now *ignored*. The replacement is a `readers` block, and the default bind is `127.0.0.1:8888` — unreachable from another container. On Docker or Kubernetes you must set `host: '0.0.0.0'` explicitly or Prometheus scrapes nothing:

```yaml
service:
  telemetry:
    metrics:
      readers:
        - pull:
            exporter:
              prometheus:
                host: '0.0.0.0'
                port: 8888
                without_type_suffix: true
                without_units: true
```

**The 60-second export interval makes a lab feel broken.** The spec default for `OTEL_METRIC_EXPORT_INTERVAL` is 60s. We spent several rounds debugging a pipeline that was working perfectly and simply had not exported yet. Set it to 10s for the lab; leave it at the default in production, where it costs real network and storage.

**Units become part of the metric name.** `unit="USD"` produced `orders_value_USD_bucket`. Braces mark an *annotation* rather than a real unit, and annotations are dropped in translation, so `unit="{USD}"` gives the clean `orders_value_bucket`. Use `{order}`, `{request}`, `{connection}` for countable things.

**Renaming a metric is not free.** After the fix, both `orders_value_USD_bucket` and `orders_value_bucket` were exposed — the old name lingers for `metric_expiration` (5m) in the collector and then for the full retention period in Prometheus. Dashboards and alert rules referencing the old name break silently. Name metrics carefully the first time.

## PromQL survival kit

```promql
rate(x_total[5m])                      per-second rate of a counter. ALWAYS rate a counter.
increase(x_total[1h])                  total over a window
sum by (job) (...)                     aggregate, keeping only `job`
histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))
topk(5, ...)                           the worst offenders
a / b                                  ratios — error rate = errors / total
```

Two things that trip people up constantly:

- The range in `rate(x[1m])` must cover **at least four scrape intervals**, or you get gaps and noise. We scrape every 5s, so `[1m]` is comfortable.
- The `job` label here is `shop/orders`, not `orders`. The collector derives it from `service.namespace` + `service.name`, which is set via `OTEL_RESOURCE_ATTRIBUTES`.

## Exercises

1. **Set off the bomb yourself.** Set `ORDERS_CARDINALITY_DEMO=true` in `lab/.env`, `docker compose up -d orders`, drive traffic, then watch <http://localhost:9090/tsdb-status>. Turn it off and note that the series do *not* disappear — they persist for the retention period. Cardinality mistakes are not instantly reversible.
2. **Follow an exemplar.** Open the RED dashboard, find a dot on the Duration panel, click it, and land in Tempo on that exact request.
3. **Starve your exemplars.** Set `OTEL_TRACES_SAMPLER_ARG=0.01` and watch the exemplar dots mostly vanish — sampling and exemplars are coupled.
4. **Prove you cannot average percentiles.** Chart `histogram_quantile(0.95, sum by (le) (...))` against `avg(histogram_quantile(0.95, ...))` on one panel and watch them disagree.
5. **Switch to delta temporality.** Set `OTEL_METRICS_TEMPORALITY=delta` in `.env`, recreate, and watch the counters in Prometheus become nonsense. Then read up on the `deltatocumulative` processor, which Phase 4 covers.
6. **Find a metric nobody is using.** Every series costs money forever. Pick one from `/tsdb-status` and justify its existence or delete it.
