#!/usr/bin/env python3
"""Reconstruct a trace waterfall from the collector's `debug` exporter stdout.

Phase 0 aid: proves you can rebuild what Tempo's UI shows using nothing but the
raw OTLP text. Once this makes sense, the UIs in later phases are just faster.

    cd lab && docker compose logs otel-collector --no-log-prefix | python3 ../tools/waterfall.py
"""
import collections
import re
import sys
from datetime import datetime

SPAN_RE = re.compile(r"Span #\d+\n(.*?)(?=\nSpan #|\nScopeSpans|\nResourceSpans|\Z)", re.S)


def field(block: str, name: str) -> str:
    m = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+)$", block, re.M)
    return m.group(1).strip() if m else ""


def parse_ts(s: str):
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)", s)
    if not m:
        return None
    t = m.group(1)
    if "." not in t:
        t += ".0"
    t = t[:26] if len(t) > 26 else t
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S.%f")


def main() -> None:
    txt = sys.stdin.read()
    spans = []
    for blk in re.split(r"ResourceSpans #\d+", txt):
        m = re.search(r"service\.name: Str\(([^)]+)\)", blk)
        if not m:
            continue
        svc = m.group(1)
        for sm in SPAN_RE.finditer(blk):
            b = sm.group(1)
            tid, sid = field(b, "Trace ID"), field(b, "ID")
            if not tid or not sid:
                continue
            spans.append({
                "svc": svc, "trace": tid, "id": sid, "parent": field(b, "Parent ID"),
                "name": field(b, "Name"), "kind": field(b, "Kind").replace("Spankind", ""),
                "start": parse_ts(field(b, "Start time")), "end": parse_ts(field(b, "End time")),
                "status": field(b, "Status code"),
            })

    by_trace = collections.defaultdict(list)
    for s in spans:
        by_trace[s["trace"]].append(s)

    # pick the richest complete-looking trace
    best = max(by_trace.values(), key=lambda v: (len({s["svc"] for s in v}), len(v)), default=None)
    if not best or not all(s["start"] and s["end"] for s in best):
        print("no complete trace found - pipe in more collector output", file=sys.stderr)
        sys.exit(1)

    t0 = min(s["start"] for s in best)
    total_ms = max((s["end"] - t0).total_seconds() for s in best) * 1000 or 1.0
    kids = collections.defaultdict(list)
    roots = []
    ids = {s["id"] for s in best}
    for s in best:
        (kids[s["parent"]] if s["parent"] in ids else roots).append(s)

    print(f"trace {best[0]['trace']}   {len(best)} spans   {len({s['svc'] for s in best})} services   {total_ms:.1f}ms total\n")
    W = 46

    def draw(s, depth=0):
        off = (s["start"] - t0).total_seconds() * 1000
        dur = (s["end"] - s["start"]).total_seconds() * 1000
        lpad = int(off / total_ms * W)
        blen = max(1, int(dur / total_ms * W))
        bar = " " * lpad + "#" * min(blen, W - lpad)
        err = "  !ERROR" if s["status"] not in ("", "Unset", "Ok") else ""
        label = f"{'  ' * depth}{s['svc']}  {s['name']}"
        print(f"{label:<52.52s} |{bar:<{W}s}| {dur:7.1f}ms  {s['kind']}{err}")
        for k in sorted(kids[s["id"]], key=lambda x: x["start"]):
            draw(k, depth + 1)

    for r in sorted(roots, key=lambda x: x["start"]):
        draw(r)


if __name__ == "__main__":
    main()
