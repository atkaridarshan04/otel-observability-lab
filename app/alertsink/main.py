"""alertsink - stands in for PagerDuty / Slack / Opsgenie.

Receives Alertmanager webhooks and prints them as readable, structured log
lines. The point is to make the ACTUAL alert payload visible: what Alertmanager
groups together, which labels survive routing, what the annotations say, and
what a resolution looks like.

    docker compose logs -f alertsink
"""
import logging
import os
from typing import Any

from fastapi import FastAPI, Request

from otelkit import get_logger

log = get_logger("alertsink")
app = FastAPI(title="alertsink")

SEEN: list[dict[str, Any]] = []
MAX_KEEP = int(os.getenv("ALERTSINK_KEEP", "200"))

BAR = "=" * 78


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "received": len(SEEN)}


@app.get("/alerts")
async def alerts() -> dict:
    """Everything this sink has seen, newest first. Handy for scripted checks."""
    return {"count": len(SEEN), "alerts": list(reversed(SEEN))}


@app.post("/alert/{channel}")
async def receive(channel: str, request: Request) -> dict:
    payload = await request.json()
    status = payload.get("status", "?")
    group = payload.get("groupLabels", {})
    alerts = payload.get("alerts", [])

    # The human-readable rendering: this is what a pager message would contain.
    print(f"\n{BAR}")
    print(f"  {status.upper():<9} -> channel:{channel}   group={group}   ({len(alerts)} alert(s))")
    print(BAR)
    for a in alerts:
        labels = a.get("labels", {})
        ann = a.get("annotations", {})
        print(f"  [{a.get('status','?').upper()}] {labels.get('alertname','?')}"
              f"   severity={labels.get('severity','-')}"
              f"   slo={labels.get('slo','-')}"
              f"   window={labels.get('window','-')}")
        if ann.get("summary"):
            print(f"      summary : {ann['summary']}")
        if ann.get("description"):
            print(f"      detail  : {' '.join(ann['description'].split())}")
        if ann.get("impact"):
            print(f"      impact  : {ann['impact']}")
        if ann.get("runbook"):
            print(f"      runbook : {ann['runbook']}")
        if ann.get("dashboard"):
            print(f"      board   : {ann['dashboard']}")
        print(f"      started : {a.get('startsAt','?')}")
    print(f"{BAR}\n", flush=True)

    # And the structured version, which flows to Loki like everything else.
    for a in alerts:
        labels = a.get("labels", {})
        log.warning(
            "alert %s" % a.get("status", "?"),
            extra={
                "channel": channel,
                "alertname": labels.get("alertname"),
                "severity": labels.get("severity"),
                "slo": labels.get("slo"),
                "window": labels.get("window"),
                "alert_status": a.get("status"),
                "summary": (a.get("annotations") or {}).get("summary"),
            },
        )
        SEEN.append({"channel": channel, "status": a.get("status"),
                     "labels": labels, "annotations": a.get("annotations", {}),
                     "startsAt": a.get("startsAt"), "endsAt": a.get("endsAt")})
    del SEEN[:-MAX_KEEP]
    return {"ok": True, "received": len(alerts)}
