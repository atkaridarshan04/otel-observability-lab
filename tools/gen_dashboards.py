#!/usr/bin/env python3
"""Generate the lab's Grafana dashboards.

Dashboards are generated rather than hand-written JSON for the same reason the
datasources are provisioned: a dashboard clicked together in a UI exists only in
one person's browser history. These are code, reviewable in a diff, and every
panel carries a description explaining why it is there.

    python3 tools/gen_dashboards.py          # writes lab/grafana/dashboards/*.json
"""
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "lab" / "grafana" / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# --------------------------------------------------------------------- helpers


def _targets(ds, exprs, exemplar=False):
    out = []
    for i, (expr, legend) in enumerate(exprs):
        t = {"refId": chr(65 + i), "datasource": ds, "expr": expr,
             "legendFormat": legend, "editorMode": "code", "range": True}
        if ds is PROM:
            t["exemplar"] = exemplar
        else:
            t["queryType"] = "range"
        out.append(t)
    return out


def ts(title, ds, exprs, gp, unit=None, desc="", exemplar=False, stack=False, decimals=None):
    return {
        "type": "timeseries", "title": title, "description": desc,
        "datasource": ds, "gridPos": gp,
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "custom": {"lineWidth": 2, "fillOpacity": 25 if stack else 8,
                       "showPoints": "never",
                       "stacking": {"mode": "normal" if stack else "none"}},
        }, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": _targets(ds, exprs, exemplar),
    }


def stat(title, ds, exprs, gp, unit=None, desc="", thresholds=None, decimals=None):
    steps = [{"color": "green", "value": None}]
    for color, value in (thresholds or []):
        steps.append({"color": color, "value": value})
    return {
        "type": "stat", "title": title, "description": desc,
        "datasource": ds, "gridPos": gp,
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "thresholds": {"mode": "absolute", "steps": steps},
            "color": {"mode": "thresholds"},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area",
                    "textMode": "auto", "justifyMode": "auto"},
        "targets": _targets(ds, exprs),
    }


def logs(title, expr, gp, desc=""):
    return {
        "type": "logs", "title": title, "description": desc,
        "datasource": LOKI, "gridPos": gp,
        "options": {"showTime": True, "showLabels": False, "wrapLogMessage": True,
                    "prettifyLogMessage": True, "enableLogDetails": True,
                    "dedupStrategy": "none", "sortOrder": "Descending"},
        "targets": [{"refId": "A", "datasource": LOKI, "expr": expr,
                     "queryType": "range", "editorMode": "code"}],
    }


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}


def g(h, w, x, y):
    return {"h": h, "w": w, "x": x, "y": y}


def dashboard(uid, title, panels, templating=None, refresh="10s", frm="now-30m"):
    d = {"uid": uid, "title": title, "tags": ["lab"], "timezone": "browser",
         "schemaVersion": 39, "version": 1, "editable": True, "refresh": refresh,
         "time": {"from": frm, "to": "now"}, "panels": panels,
         "templating": {"list": templating or []}}
    (OUT / f"{uid}.json").write_text(json.dumps(d, indent=2))
    return d


# -------------------------------------------------------- 1. RED overview ----
SHOP = 'service_namespace="shop"'
HSD = "http_server_duration_milliseconds"

red = [
    row("RED — the three symptom signals", 0),
    ts("Rate — requests/sec by service", PROM,
       [(f"sum by (job) (rate({HSD}_count[1m]))", "{{job}}")], g(8, 8, 0, 1), "reqps",
       "Traffic. Not a health signal alone, but every other number is meaningless without it: a 50% error rate on 2 requests is noise."),
    ts("Errors — 5xx/sec by service", PROM,
       [(f'sum by (job) (rate({HSD}_count{{http_status_code=~"5.."}}[1m]))', "{{job}} 5xx")],
       g(8, 8, 8, 1), "reqps",
       "ONLY 5xx. 402 card-declined and 403 fraud-rejected are business outcomes, not incidents. Counting them pages you because a customer mistyped a card number."),
    ts("Duration — gateway percentiles (click a dot → trace)", PROM,
       [(f'histogram_quantile(0.50, sum by (le) (rate({HSD}_bucket{{job="shop/gateway"}}[1m])))', "p50"),
        (f'histogram_quantile(0.95, sum by (le) (rate({HSD}_bucket{{job="shop/gateway"}}[1m])))', "p95"),
        (f'histogram_quantile(0.99, sum by (le) (rate({HSD}_bucket{{job="shop/gateway"}}[1m])))', "p99")],
       g(8, 8, 16, 1), "ms", exemplar=True,
       desc="Percentiles from a histogram, never an average. The dots are EXEMPLARS — each is a real request carrying its trace_id. Click one to land on that trace."),

    row("Business — what HTTP metrics cannot tell you", 9),
    ts("Orders placed /sec by tier", PROM,
       [("sum by (tier) (rate(orders_placed_total[1m]))", "{{tier}}")], g(8, 8, 0, 10), "reqps",
       "'Is checkout up?' is an HTTP question. 'Did revenue stop?' is this one. A deploy that silently rejects every enterprise order leaves HTTP metrics green."),
    ts("Rejections /sec by reason", PROM,
       [("sum by (reason) (rate(orders_rejected_total[1m]))", "{{reason}}")], g(8, 8, 8, 10), "reqps",
       "Split by reason so a fraud-rule misfire, a payment outage and a genuine stockout are three lines rather than one undifferentiated number.", stack=True),
    ts("Order value — p50 / p95", PROM,
       [("histogram_quantile(0.50, sum by (le) (rate(orders_value_bucket[5m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(orders_value_bucket[5m])))", "p95")],
       g(8, 8, 16, 10), "currencyUSD",
       "A histogram of a non-latency quantity. Instrument type is about the SHAPE of the data, not what it measures."),

    row("Logs — the third signal", 18),
    ts("Log lines/sec by service and level", LOKI,
       [(f"sum by (service_name, severity_text) (rate({{{SHOP}}}[1m]))", "{{service_name}} {{severity_text}}")],
       g(8, 16, 0, 19), "logs/s",
       "Log VOLUME is itself a signal. A service that suddenly logs 50x more is usually retrying something.", stack=True),
    logs("Live errors and warnings (click a line → trace)",
         f'{{{SHOP}}} | severity_text=~"ERROR|WARN"', g(8, 8, 16, 19),
         "Every line carries trace_id, so expanding one gives a direct link into Tempo. This is the last step of the golden path."),
]
dashboard("red-overview", "RED overview — shop", red)


# ------------------------------------------------------ 2. Service detail ----
JOB = '{job="$service"}'
svc = [
    row("$service — RED", 0),
    stat("Request rate", PROM, [(f"sum(rate({HSD}_count{JOB}[1m]))", "")], g(4, 4, 0, 1), "reqps",
         "Traffic into this service.", decimals=2),
    stat("Error rate (5xx)", PROM,
         [(f'sum(rate({HSD}_count{{job="$service",http_status_code=~"5.."}}[1m])) / clamp_min(sum(rate({HSD}_count{JOB}[1m])), 0.001)', "")],
         g(4, 4, 4, 1), "percentunit", "Fraction of requests failing with 5xx.",
         thresholds=[("yellow", 0.01), ("red", 0.05)], decimals=3),
    stat("p95 latency", PROM,
         [(f"histogram_quantile(0.95, sum by (le) (rate({HSD}_bucket{JOB}[1m])))", "")],
         g(4, 4, 8, 1), "ms", "95th percentile. The experience of the unluckiest 1 in 20.",
         thresholds=[("yellow", 250), ("red", 1000)], decimals=0),
    stat("p99 latency", PROM,
         [(f"histogram_quantile(0.99, sum by (le) (rate({HSD}_bucket{JOB}[1m])))", "")],
         g(4, 4, 12, 1), "ms", "99th percentile. Where timeouts and saturation show up first.",
         thresholds=[("yellow", 500), ("red", 2000)], decimals=0),
    stat("In-flight requests", PROM, [(f"sum(http_server_active_requests{JOB})", "")], g(4, 4, 16, 1),
         None, "Concurrency. A rising floor here while rate is flat means requests are piling up — the earliest saturation signal you get.", decimals=0),
    stat("Log errors/sec", LOKI,
         [(f'sum(rate({{service_name=~".*", {SHOP}}} | severity_text="ERROR" [1m]))', "")],
         g(4, 4, 20, 1), None, "Error log volume across the shop.", decimals=2),

    ts("Rate by status code", PROM,
       [(f"sum by (http_status_code) (rate({HSD}_count{JOB}[1m]))", "{{http_status_code}}")],
       g(8, 12, 0, 5), "reqps",
       "Broken out by code so you can separate 5xx incidents from 4xx business outcomes at a glance.", stack=True),
    ts("Latency percentiles (exemplars on)", PROM,
       [(f"histogram_quantile(0.50, sum by (le) (rate({HSD}_bucket{JOB}[1m])))", "p50"),
        (f"histogram_quantile(0.90, sum by (le) (rate({HSD}_bucket{JOB}[1m])))", "p90"),
        (f"histogram_quantile(0.99, sum by (le) (rate({HSD}_bucket{JOB}[1m])))", "p99")],
       g(8, 12, 12, 5), "ms", exemplar=True,
       desc="Click any exemplar dot to open the trace behind that exact measurement."),

    row("$service — outbound dependencies", 13),
    ts("Outbound call rate by target", PROM,
       [(f'sum by (http_host) (rate(http_client_duration_milliseconds_count{JOB}[1m]))', "-> {{http_host}}")],
       g(8, 12, 0, 14), "reqps",
       "What this service calls. Client spans are the other half of the service graph."),
    ts("Outbound p95 by target", PROM,
       [(f'histogram_quantile(0.95, sum by (http_host, le) (rate(http_client_duration_milliseconds_bucket{JOB}[1m])))', "-> {{http_host}}")],
       g(8, 12, 12, 14), "ms",
       "When your own latency rises, this panel usually says whose fault it is."),

    row("$service — logs", 22),
    logs("Errors and warnings from $service",
         '{service_name=~"${service:regex}"} | severity_text=~"ERROR|WARN"', g(9, 24, 0, 23),
         "Filtered to this service. Expand a line to follow trace_id into Tempo."),
]
service_var = [{
    "name": "service", "label": "Service", "type": "query", "datasource": PROM,
    "query": {"query": f"label_values({HSD}_count, job)", "refId": "A"},
    "refresh": 1, "includeAll": False, "multi": False,
    "current": {"text": "shop/gateway", "value": "shop/gateway"},
}]
dashboard("service-detail", "Service detail", svc, templating=service_var)


# -------------------------------------------------- 3. Telemetry pipeline ----
pipe = [
    row("Is the collector keeping up?", 0),
    stat("Spans received/sec", PROM, [("sum(rate(otelcol_receiver_accepted_spans[1m]))", "")],
         g(4, 5, 0, 1), None, "Spans entering the collector.", decimals=1),
    stat("Spans REFUSED/sec", PROM, [("sum(rate(otelcol_receiver_refused_spans[1m]))", "")],
         g(4, 5, 5, 1), None,
         "Data the collector rejected — usually memory_limiter pushing back. Anything above zero is silent data loss.",
         thresholds=[("red", 0.001)], decimals=2),
    stat("Export queue size", PROM, [("max(otelcol_exporter_queue_size)", "")],
         g(4, 5, 10, 1), None,
         "A queue that is growing rather than draining means the backend is slower than ingest. The next thing that happens is dropped data.",
         thresholds=[("yellow", 1000), ("red", 5000)], decimals=0),
    stat("Collector memory (RSS)", PROM, [("max(otelcol_process_memory_rss)", "")],
         g(4, 5, 15, 1), "bytes", "Collector resident memory.", decimals=0),
    stat("Uptime", PROM, [("max(otelcol_process_uptime)", "")], g(4, 4, 20, 1), "s",
         "Resets to zero on restart. A collector that keeps restarting is dropping data every time.", decimals=0),

    row("Throughput by signal", 6),
    ts("Received/sec by signal", PROM,
       [("sum(rate(otelcol_receiver_accepted_spans[1m]))", "spans"),
        ("sum(rate(otelcol_receiver_accepted_metric_points[1m]))", "metric points"),
        ("sum(rate(otelcol_receiver_accepted_log_records[1m]))", "log records")],
       g(8, 12, 0, 7), None,
       "Relative volume of the three signals. Logs usually dominate, which is why they are usually the biggest bill."),
    ts("Sent/sec by exporter", PROM,
       [("sum by (exporter) (rate(otelcol_exporter_sent_spans[1m]))", "{{exporter}} spans"),
        ("sum by (exporter) (rate(otelcol_exporter_sent_metric_points[1m]))", "{{exporter}} metrics"),
        ("sum by (exporter) (rate(otelcol_exporter_sent_log_records[1m]))", "{{exporter}} logs")],
       g(8, 12, 12, 7), None,
       "Per destination. Compare with the received panel: a persistent gap is data going missing in the pipeline."),

    row("Backends", 15),
    ts("Collector CPU seconds/sec", PROM,
       [("sum(rate(otelcol_process_cpu_seconds[1m]))", "cpu cores")], g(7, 8, 0, 16), None,
       "Effective cores consumed. The collector is usually CPU-bound on serialisation before it is memory-bound."),
    ts("Batch send size (spans per batch)", PROM,
       [("histogram_quantile(0.50, sum by (le) (rate(otelcol_processor_batch_batch_send_size_bucket[1m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(otelcol_processor_batch_batch_send_size_bucket[1m])))", "p95")],
       g(7, 8, 8, 16), None,
       "Batches far below send_batch_size mean the timeout is firing first — you are paying per-request overhead for small payloads."),
    ts("Heap allocated", PROM,
       [("max(otelcol_process_runtime_heap_alloc_bytes)", "heap")], g(7, 8, 16, 16), "bytes",
       "Watch alongside queue size. Memory climbing with a full queue is the classic pre-OOM shape."),
]
dashboard("pipeline-health", "Telemetry pipeline health", pipe, frm="now-1h")


# --------------------------------------------------------- 4. Golden path ----
gold = [
    row("1. The symptom — what the user feels", 0),
    ts("Checkout error rate (5xx) — the only thing worth paging on", PROM,
       [(f'sum(rate({HSD}_count{{job="shop/gateway",http_status_code=~"5.."}}[1m])) / clamp_min(sum(rate({HSD}_count{{job="shop/gateway"}}[1m])), 0.001)', "error ratio")],
       g(7, 12, 0, 1), "percentunit",
       "Symptom-based: the fraction of user requests that failed. Not CPU, not memory, not queue depth — what the user actually experienced.", decimals=4),
    ts("Checkout latency p99 — click an exemplar to start the drill-down", PROM,
       [(f'histogram_quantile(0.99, sum by (le) (rate({HSD}_bucket{{job="shop/gateway"}}[1m])))', "p99")],
       g(7, 12, 12, 1), "ms", exemplar=True,
       desc="STEP 2 of the golden path. Each dot is a real request. Click one and Grafana opens its trace in Tempo."),

    row("2. Narrowing — which service, which dependency", 8),
    ts("p95 by service — who is actually slow?", PROM,
       [(f"histogram_quantile(0.95, sum by (job, le) (rate({HSD}_bucket[1m])))", "{{job}}")],
       g(7, 12, 0, 9), "ms",
       "The gateway being slow usually means something downstream is slow. This panel names it."),
    ts("5xx by service", PROM,
       [(f'sum by (job) (rate({HSD}_count{{http_status_code=~"5.."}}[1m]))', "{{job}}")],
       g(7, 12, 12, 9), "reqps",
       "Errors propagate upward, so the service with the EARLIEST rise is usually the cause, not the one with the most errors."),

    row("3. The evidence — the actual log lines", 16),
    logs("Errors across all services — expand a line for its trace link",
         f'{{{SHOP}}} | severity_text="ERROR"', g(10, 24, 0, 17),
         "STEP 5, the end of the golden path. These lines carry trace_id, order_id and customer_id as structured fields, so you can filter on any of them rather than grepping."),
]
dashboard("golden-path", "Golden path — alert to log line", gold, frm="now-1h")

names = sorted(p.name for p in OUT.glob("*.json"))
print("generated:")
for n in names:
    print("  ", n)
