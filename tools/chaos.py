#!/usr/bin/env python3
"""Induce and observe failure scenarios.

    python3 tools/chaos.py list
    python3 tools/chaos.py start pool-exhaustion
    python3 tools/chaos.py observe
    python3 tools/chaos.py stop

Scenarios change environment variables or container state only - never code.
In production you cannot patch the application to reproduce an incident, so the
lab does not let you either.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAB = ROOT / "lab"
ENV = LAB / ".env"
SPEC = yaml.safe_load((ROOT / "chaos" / "scenarios.yaml").read_text())

PROM, TEMPO, LOKI, ALERTS = (
    "http://localhost:9090", "http://localhost:3200",
    "http://localhost:3100", "http://localhost:8081")


# ----------------------------------------------------------------- helpers --
def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=LAB,
                          capture_output=True, text=True).stdout


def get(url: str, timeout: int = 20):
    try:
        return json.load(urllib.request.urlopen(url, timeout=timeout))
    except Exception:
        return None


def promq(query: str):
    d = get(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query}))
    return (d or {}).get("data", {}).get("result", []) if d else []


def scalar(query: str, default=0.0) -> float:
    r = promq(query)
    return float(r[0]["value"][1]) if r else default


def set_env(updates: dict[str, str]) -> None:
    lines = ENV.read_text().splitlines()
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV.write_text("\n".join(out) + "\n")


def find(name: str) -> dict:
    for s in SPEC["scenarios"]:
        if s["name"] == name:
            return s
    sys.exit(f"unknown scenario: {name}\nrun `python3 tools/chaos.py list`")


# ------------------------------------------------------------- subcommands --
def cmd_list() -> None:
    print("\nScenarios:\n")
    for s in SPEC["scenarios"]:
        print(f"  \033[1m{s['name']}\033[0m")
        print(f"      {s['title']}")
        print(f"      {' '.join(s['story'].split())}")
        if s.get("expect"):
            print(f"      expects: {', '.join(s['expect'])}")
        print()


def cmd_start(name: str) -> None:
    s = find(name)
    print(f"\n=== {s['title']} ===")
    print(f"{' '.join(s['story'].split())}\n")

    if s.get("env"):
        set_env(s["env"])
        print("  env changes:")
        for k, v in s["env"].items():
            print(f"    {k}={v}")
    if s.get("stop_services"):
        for svc in s["stop_services"]:
            compose("stop", svc)
            print(f"  stopped container: {svc}")
    if s.get("restart"):
        compose("up", "-d", *s["restart"])
        print(f"  recreated: {', '.join(s['restart'])}")

    (LAB / ".chaos-active").write_text(name)
    print(f"\n  WATCH FOR: {' '.join(s['look_for'].split())}")
    print(f"\n  Give it ~2 minutes, then: python3 tools/chaos.py observe")
    print(f"  Restore with:             python3 tools/chaos.py stop\n")


def cmd_stop() -> None:
    set_env(SPEC["defaults"])
    marker = LAB / ".chaos-active"
    active = marker.read_text().strip() if marker.exists() else None
    if active:
        s = find(active)
        for svc in s.get("stop_services", []):
            compose("start", svc)
            print(f"  started container: {svc}")
        marker.unlink()
    compose("up", "-d")
    print("  all knobs restored to healthy defaults; services recreated")
    print("  (metrics keep their history - windows take a few minutes to decay)\n")


def cmd_observe(window: int = 180) -> None:
    marker = LAB / ".chaos-active"
    active = marker.read_text().strip() if marker.exists() else None
    s = find(active) if active else None

    print("\n" + "=" * 78)
    print(f"  OBSERVING: {s['title'] if s else 'no scenario active (healthy baseline)'}")
    print("=" * 78)

    # ---- 1. the symptom, as a user would experience it --------------------
    print("\n-- 1. SYMPTOM (what the user sees) --")
    total = scalar(f'sum(rate(http_server_duration_milliseconds_count{{job="shop/gateway"}}[{window}s]))')
    err = scalar(f'sum(rate(http_server_duration_milliseconds_count{{job="shop/gateway",http_status_code=~"5.."}}[{window}s]))')
    p50 = scalar(f'histogram_quantile(0.50, sum by (le) (rate(http_server_duration_milliseconds_bucket{{job="shop/gateway"}}[{window}s])))')
    p99 = scalar(f'histogram_quantile(0.99, sum by (le) (rate(http_server_duration_milliseconds_bucket{{job="shop/gateway"}}[{window}s])))')
    print(f"  checkout throughput : {total:8.2f} req/s")
    print(f"  checkout error rate : {100*err/total if total else 0:8.2f} %")
    print(f"  checkout p50        : {p50:8.1f} ms")
    print(f"  checkout p99        : {p99:8.1f} ms")
    print(f"  budget remaining    : {scalar('slo:checkout_availability:budget_remaining6h', float('nan')):8.3f}")
    print(f"  burn rate (1h)      : {scalar('slo:checkout_availability:burn_rate1h'):8.2f}x")

    # ---- 2. alerts --------------------------------------------------------
    print("\n-- 2. ALERTS --")
    d = get(f"{PROM}/api/v1/alerts")
    rows = (d or {}).get("data", {}).get("alerts", [])
    if not rows:
        print("  (none)")
    for a in rows:
        lab = a["labels"]
        print(f"  {a['state'].upper():<8} {lab.get('alertname'):<34} sev={lab.get('severity','-')}")

    # ---- 3. which service ------------------------------------------------
    print("\n-- 3. WHICH SERVICE (RED per service) --")
    print(f"  {'service':<18}{'req/s':>9}{'5xx%':>8}{'p50 ms':>10}{'p99 ms':>10}")
    jobs = sorted({r["metric"].get("job", "") for r in
                   promq('sum by (job) (rate(http_server_duration_milliseconds_count[1m]))')})
    for job in jobs:
        t = scalar(f'sum(rate(http_server_duration_milliseconds_count{{job="{job}"}}[{window}s]))')
        e = scalar(f'sum(rate(http_server_duration_milliseconds_count{{job="{job}",http_status_code=~"5.."}}[{window}s]))')
        q50 = scalar(f'histogram_quantile(0.50, sum by (le) (rate(http_server_duration_milliseconds_bucket{{job="{job}"}}[{window}s])))')
        q99 = scalar(f'histogram_quantile(0.99, sum by (le) (rate(http_server_duration_milliseconds_bucket{{job="{job}"}}[{window}s])))')
        print(f"  {job:<18}{t:>9.2f}{100*e/t if t else 0:>8.2f}{q50:>10.1f}{q99:>10.1f}")

    inflight = promq('sum by (job) (http_server_active_requests)')
    if inflight:
        print("\n  in-flight requests (saturation):")
        for r in inflight:
            print(f"    {r['metric'].get('job',''):<18}{float(r['value'][1]):>8.0f}")

    # ---- 4. a representative trace ---------------------------------------
    print("\n-- 4. A REPRESENTATIVE TRACE --")
    now = int(time.time())
    trace = None
    for q in ('{ status = error }', '{ duration > 900ms }', '{ name = "POST /checkout" }'):
        u = f"{TEMPO}/api/search?" + urllib.parse.urlencode(
            {"q": q, "limit": 1, "start": now - window, "end": now})
        res = (get(u) or {}).get("traces") or []
        if res:
            trace, picked = res[0]["traceID"], q
            break
    if not trace:
        print("  (no trace found in window)")
    else:
        print(f"  matched {picked} -> {trace}")
        td = get(f"{TEMPO}/api/traces/{trace}")
        rows = []
        for b in (td or {}).get("batches", []):
            svc = next((a["value"].get("stringValue") for a in b["resource"]["attributes"]
                        if a["key"] == "service.name"), "?")
            for ss in b.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    st, en = int(sp["startTimeUnixNano"]), int(sp["endTimeUnixNano"])
                    status = (sp.get("status") or {}).get("code", 0)
                    rows.append((st, svc, sp["name"], (en - st) / 1e6, status))
        rows.sort()
        if rows:
            t0 = rows[0][0]
            span = max((r[0] - t0) / 1e6 + r[3] for r in rows) or 1.0
            for st, svc, name, dur, status in rows[:18]:
                off = (st - t0) / 1e6
                bar = " " * int(off / span * 34) + "#" * max(1, int(dur / span * 34))
                flag = " ERROR" if status == 2 else ""
                print(f"  {svc:<10}{name:<26}|{bar:<34}|{dur:8.1f}ms{flag}")

    # ---- 5. the actual log lines -----------------------------------------
    print("\n-- 5. THE ACTUAL LOG LINES --")
    lq = urllib.parse.urlencode({
        "query": '{service_namespace="shop"} | severity_text=~"ERROR|WARN"',
        "limit": 8, "start": (now - window) * 10**9, "end": now * 10**9})
    streams = (get(f"{LOKI}/loki/api/v1/query_range?{lq}") or {}).get("data", {}).get("result", [])
    lines = sorted((int(ts), st["stream"], msg) for st in streams for ts, msg in st["values"])
    if not lines:
        print("  (no errors or warnings in window)")
    for _, meta, msg in lines[-8:]:
        extra = {k: v for k, v in meta.items() if k in
                 ("dependency", "error", "status", "available", "requested", "order_id", "sku")}
        print(f"  {meta.get('service_name',''):<10} {meta.get('severity_text',''):<5} {msg}")
        if extra:
            print(f"             {extra}")
        if meta.get("trace_id"):
            print(f"             trace_id={meta['trace_id']}")

    # ---- 6. telemetry integrity ------------------------------------------
    print("\n-- 6. IS THE TELEMETRY ITSELF OK? --")
    refused = scalar('sum(rate(otelcol_receiver_refused_spans[2m]))')
    failed = scalar('sum(rate(otelcol_exporter_send_failed_spans[2m]))')
    keep_in = scalar('sum(rate(otelcol_receiver_accepted_spans{tier="gateway"}[2m]))')
    keep_out = scalar('sum(rate(otelcol_exporter_sent_spans{exporter="otlp/tempo"}[2m]))')
    print(f"  spans refused   : {refused:.3f}/s {'  <-- DATA LOSS' if refused > 0 else ''}")
    print(f"  export failures : {failed:.3f}/s {'  <-- DELIVERY PROBLEM' if failed > 0 else ''}")
    print(f"  sampling keeps  : {100*keep_out/keep_in if keep_in else 0:.1f}% of spans")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "start":
        cmd_start(sys.argv[2])
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "observe":
        cmd_observe(int(sys.argv[2]) if len(sys.argv) > 2 else 180)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
