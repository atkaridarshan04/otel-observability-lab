#!/usr/bin/env python3
"""Walk the golden path end to end, from the command line.

Picks a recent WARN/ERROR log line, then proves the same trace_id resolves in
Tempo and pulls back every log line for that one request across all services.

    python3 tools/golden_path.py

This is the whole point of the stack, demonstrated without a UI. If this script
works, correlation is wired correctly; if it does not, something in the chain
(trace context in logs, Loki structured metadata, Tempo ingest) is broken.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

LOKI = "http://localhost:3100"
TEMPO = "http://localhost:3200"
WINDOW_S = 900


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=20))


def loki(query, now, limit=50):
    qs = urllib.parse.urlencode({"query": query, "limit": limit,
                                 "start": (now - WINDOW_S) * 10**9, "end": now * 10**9})
    return get(f"{LOKI}/loki/api/v1/query_range?{qs}")["data"]["result"]


def main() -> None:
    now = int(time.time())

    streams = loki('{service_namespace="shop"} | severity_text=~"WARN|ERROR"', now, 20)
    pick = next((s for s in streams if s["stream"].get("trace_id")), None)
    if not pick:
        print("No WARN/ERROR log carrying a trace_id in the last 15 minutes.")
        print("Either the system is healthy, or correlation is broken. Run a chaos")
        print("scenario from chaos/ and try again.")
        sys.exit(1)

    tid = pick["stream"]["trace_id"]
    s = pick["stream"]

    print("=" * 72)
    print("STEP 1 — a log line  (Loki)")
    print("=" * 72)
    print(f"  service   : {s.get('service_name')}")
    print(f"  level     : {s.get('severity_text')}")
    print(f"  message   : {pick['values'][0][1]}")
    business = {k: v for k, v in s.items() if k in (
        "order_id", "customer_id", "amount_usd", "tier", "sku",
        "dependency", "status", "available", "requested", "error")}
    print(f"  structured: {business}")
    print(f"  trace_id  : {tid}")

    print()
    print("=" * 72)
    print("STEP 2 — the SAME trace_id resolves in Tempo")
    print("=" * 72)
    trace = get(f"{TEMPO}/api/traces/{tid}")
    rows = []
    for batch in trace.get("batches", []):
        svc = next((a["value"].get("stringValue") for a in batch["resource"]["attributes"]
                    if a["key"] == "service.name"), "?")
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                rows.append((start, svc, span["name"], (end - start) / 1e6))
    rows.sort()
    if rows:
        t0 = rows[0][0]
        print(f"  {len(rows)} spans across {len({r[1] for r in rows})} services")
        for start, svc, name, dur in rows[:12]:
            print(f"    +{(start - t0) / 1e6:6.1f}ms  {svc:<10} {name:<30} {dur:7.2f}ms")

    print()
    print("=" * 72)
    print("STEP 3 — every log line for that one request, across all services")
    print("=" * 72)
    found = loki('{service_namespace="shop"} | trace_id="%s"' % tid, now)
    lines = sorted((int(ts), st["stream"].get("service_name"),
                    st["stream"].get("severity_text"), line)
                   for st in found for ts, line in st["values"])
    for _, svc, level, line in lines:
        print(f"  {svc:<10} {level:<5} {line}")

    print()
    print(f"trace {tid[:16]}... -> {len(rows)} spans -> {len(lines)} log lines. Triangle closed.")


if __name__ == "__main__":
    main()
