# Phase 5 — Symptom-based alerting

Stack added: **Alertmanager**, plus an `alertsink` service standing in for PagerDuty or Slack so you can read the actual payload a page would carry.

This is the phase where the Phase 1 decision to keep `402 card declined` distinct from `503 processor unreachable` finally pays for itself. That distinction could not have been added later.

Alertmanager: <http://localhost:9093> · SLO dashboard: <http://localhost:3000/d/slo-overview> · `docker compose logs -f alertsink`

## Cause-based alerting, and why it fails

The classic alert set is `CPU > 80%`, `memory > 90%`, `disk filling`, `queue deep`. Two failure modes, both fatal:

**It fires when nothing is wrong.** An efficient service *should* use its CPU. A healthy queue *has* depth. Every one of these produces pages that the on-call learns to acknowledge without reading, which is how a team ends up with alerting that technically works and is practically ignored.

**It stays silent when something is wrong.** You can only alert on causes you predicted. The outage that wakes you at 3am is almost always the one nobody wrote a rule for — a dependency returning 200 with an empty body, a cert expiring, a config flag inverted.

**Symptom-based alerting** inverts this: page on what the *user experiences*. One rule covers every cause, including the ones nobody imagined, because they all converge on the same symptom — requests failing or requests being slow.

The cause-level data still gets collected. It is what you look at *after* the page, during diagnosis. It is simply not what wakes a human.

## SLI, SLO, error budget

- **SLI** — the measurement. "Fraction of checkout requests that return 5xx."
- **SLO** — the target. "99.5% succeed over 30 days."
- **Error budget** — what the SLO permits you to spend. 100% − 99.5% = **0.5%**.

The error budget is the useful idea, because it converts an argument into arithmetic. "Should we ship the risky feature or spend the sprint on reliability?" stops being a matter of opinion: budget remaining, ship; budget spent, fix.

The SLO targets are themselves recorded as series, so the dashboard, the alerts and the budget calculation all read one number and changing the SLO is a one-line edit:

```yaml
- record: slo:checkout_availability:objective
  expr: "0.995"
- record: slo:checkout_availability:budget
  expr: "0.005"
```

### The SLI that makes all of this work

```promql
sum(rate(http_server_duration_milliseconds_count{job="shop/gateway",http_status_code=~"5.."}[5m]))
/ clamp_min(sum(rate(http_server_duration_milliseconds_count{job="shop/gateway"}[5m])), 0.001)
```

`5..` and nothing else. The lab's live traffic includes a steady trickle of 402s and 403s, and **none of them burn error budget**. An SLI that counted them would page someone every time customers mistyped card numbers.

`clamp_min` on the denominator avoids a divide-by-zero producing `NaN` at low traffic — a real source of phantom alerts at 4am when traffic drops.

## Burn rate, and the multi-window pattern

**Burn rate** is how many times faster than "exactly exhausting the budget over the whole period" you are currently spending. Burn rate 1 means the budget runs out precisely at the end of the period. Burn rate 14.4 means a 30-day budget is gone in about two days.

The naive alert — "error rate > 1%" — cannot be made to work, because the two failure modes want opposite windows:

| Window | Problem |
|---|---|
| Short (1m) | Fires on a 30-second blip. Pages at 3am. Resolves before you open the laptop. |
| Long (30m) | A total outage takes half an hour to page anyone. |

The fix (Google SRE Workbook ch. 5) is to AND **two** windows:

- **Long window** — "this has been going on long enough to be real"
- **Short window** — "AND it is still happening right now"

```yaml
- alert: CheckoutErrorBudgetBurnFast
  expr: |
    slo:checkout_availability:error_ratio1h > (14.4 * 0.005)
    and
    slo:checkout_availability:error_ratio5m > (14.4 * 0.005)
  for: 2m
  labels: {severity: page, slo: checkout-availability, window: 1h/5m}
```

The standard tiers:

| Severity | Long | Short | Factor | Budget consumed |
|---|---|---|---|---|
| page | 1h | 5m | 14.4 | 2% |
| page | 6h | 30m | 6 | 5% |
| ticket | 3d | 6h | 1 | 10% |

### The short window, demonstrated

We induced a 35% payment failure rate, let it page, then turned it off and watched:

```
r1: 1m=0.0%  5m=19.5%  alerts[firing=1 pending=1]
r2: 1m=0.0%  5m=19.5%  alerts[pending=1]        <- page CLEARED here
r3: 1m=0.0%  5m=19.5%  alerts[pending=1]
...
```

The incident ended, the **1m window dropped to zero immediately**, and the page cleared — while the 5m window still read 19.5% and would keep reading it for another five minutes. **The short window is what makes an alert resolve promptly.** Without it you stare at a red dashboard for the length of the long window after the problem is already fixed.

The 6h/30m rule stayed `PENDING` and never fired, which is also correct: its 30m short window fell below threshold before its 6h long window did, and both must hold.

### Lab-compressed windows

`slo-alerts.yml` also carries a `slo_checkout_burn_lab` group with windows shrunk about 12x (5m/1m), so an induced failure pages within a couple of minutes rather than an hour. **Do not ship those.** Short windows are exactly what makes alerting flappy; they exist so a learning session can watch the mechanism work.

## Alertmanager: routing, grouping, inhibition

Prometheus decides *whether* an alert fires. Alertmanager decides *who hears about it, how often, and which alerts are suppressed*. These are different jobs and conflating them is the root of most alert fatigue.

**Grouping** is the biggest anti-noise lever. Without it, a failure that trips 40 alerts sends 40 pages:

```yaml
group_by: [alertname, slo]
group_wait: 10s        # wait for siblings before the first notification
group_interval: 1m     # minimum gap between updates for a group
repeat_interval: 4h    # re-notify an unchanged firing group this often
```

**Routing** sends different severities down different paths — pages get a 5s `group_wait` because batching an outage is batching downtime; tickets get a 1m wait and a 12h repeat because nothing in them justifies interrupting a human.

**Inhibition** suppresses an alert when a more important one already explains it:

```yaml
inhibit_rules:
  - source_matchers: [alertname = "CheckoutErrorBudgetBurnFast"]
    target_matchers: [alertname = "CheckoutErrorBudgetBurnSlow"]
    equal: [slo]

  # The important one: if the collector is dropping spans, every SLO number is
  # computed from incomplete data. Page about the blindness, suppress the
  # alerts you can no longer believe.
  - source_matchers: [alertname = "TelemetryDataLoss"]
    target_matchers: [slo =~ ".+"]
```

## What a page actually looks like

Captured from `alertsink` during the induced incident:

```
==============================================================================
  FIRING    -> channel:pager   group={'alertname': 'LabCheckoutErrorBudgetBurnFast',
                                      'slo': 'checkout-availability'}   (1 alert(s))
==============================================================================
  [FIRING] LabCheckoutErrorBudgetBurnFast   severity=page
           slo=checkout-availability   window=5m/1m
      summary : [LAB] Checkout burning error budget 14.4x too fast
      detail  : 11.37% of checkout requests failing with 5xx.
      runbook : docs/05-alerting.md#checkouterrorbudgetburnfast
      board   : http://localhost:3000/d/golden-path
      started : 2026-10-12T17:57:22.98Z
==============================================================================
```

Every alert carries a **runbook link** and a **dashboard link**. An alert that says only "error rate high" hands the on-call a puzzle at 3am; one that says what broke, what it means for users, where to look and what to do hands them a next step. Write annotations for the person who did not design the system and is not fully awake.

## The alerts this lab defines

| Alert | Severity | Catches |
|---|---|---|
| `CheckoutErrorBudgetBurnFast` | page | Users seeing failures right now |
| `CheckoutErrorBudgetBurnSlow` | page | Sustained elevated errors |
| `CheckoutLatencyBudgetBurnFast` | page | Too slow to be usable |
| `OrdersStopped` | page | Revenue stopped while HTTP looks green |
| `OrderRejectionSpike` | ticket | Misfiring fraud rule or payment provider |
| `TelemetryDataLoss` | page | You are blind |
| `TelemetryQueueFilling` | page | You are about to be blind |
| `TelemetryExportFailing` | ticket | Backend struggling, queue retrying |
| `CollectorDown` | page | You are blind |

Telemetry alerts are cause-based, and earn the exception: they do not describe an unhealthy system, they describe **you being unable to see**. The test for whether a cause-based alert deserves to page is *does the symptom alert still work if this is broken?* For telemetry loss, the answer is no.

### What this lab deliberately does NOT alert on

Written down in `platform-alerts.yml`, because the absences are as much a design decision as the presences:

- **CPU / memory high** — an efficient service should use its CPU. Collect it; diagnose with it; never page on it.
- **402 card decline rate** — a business outcome.
- **403 fraud rejection rate** — a ticket (`OrderRejectionSpike`), never a page.
- **Disk low, pod restarted, OOMKilled** — ticket-worthy. They page only when they cause a user-visible symptom, and at that point the SLO burn alert fires anyway, for every cause including the unpredicted ones.
- **Per-service latency** — if `payments` is slow but checkout still meets its SLO, nothing is wrong from the user's point of view.

---

## Runbooks

### CheckoutErrorBudgetBurnFast

Users are seeing failed checkouts. Treat as an outage.

1. [Golden path dashboard](http://localhost:3000/d/golden-path) — which service shows the **earliest** rise? Errors propagate upward, so the earliest is usually the cause, not the loudest.
2. Click an exemplar on the p99 panel to land on a failing trace.
3. From the span, follow to logs; filter Loki by `trace_id` for the exact error text.
4. Common causes here: a dependency returning 5xx, connection pool exhaustion, a cascading timeout.
5. Mitigate first, diagnose second. Roll back the recent deploy if there was one.

### CheckoutErrorBudgetBurnSlow

Lower rate, sustained. Not an outage; still spending budget faster than the period allows.

1. [SLO dashboard](http://localhost:3000/d/slo-overview) — how much budget is left? That decides urgency.
2. Check whether the rise correlates with a deploy.
3. Look at `sum by (reason) (rate(orders_rejected_total[5m]))` — a single dominant reason usually names the problem.

### CheckoutLatencyBudgetBurnFast

More than 1.44% of checkouts are slower than 500ms.

1. [Service detail](http://localhost:3000/d/service-detail) — compare p95 per service to find the slow hop.
2. Check **Outbound p95 by target** on the slow service; latency usually arrives from downstream.
3. Check `cache.hit` in Tempo — a collapsed cache hit rate is a classic cause.
4. Check `orders_in_flight` / `http_server_active_requests`: a rising floor with flat request rate means saturation.

### OrdersStopped

Traffic is flowing and no orders are completing. Every HTTP dashboard can be green.

1. Was there a deploy? Roll it back.
2. `{service_namespace="shop"} | severity_text="ERROR"` in Loki.
3. Check `orders_rejected_total` by reason — if rejections equal traffic, a validation or fraud rule is rejecting everything.
4. Check the database and cache are reachable.

### OrderRejectionSpike

1. `sum by (reason) (rate(orders_rejected_total[5m]))` — which reason dominates?
2. `fraud` → a rule or threshold changed. `card_declined` → suspect the payment provider. `out_of_stock` → genuine, probably not an incident.
3. In Tempo: `{ name = "fraud.check" && .fraud.score > 0.9 }`.

### TelemetryDataLoss

The collector is refusing spans. **Everything else you are looking at is now unreliable.**

1. [Pipeline health](http://localhost:3000/d/pipeline-health) — which tier?
2. Agent tier: usually a host memory problem, or one application suddenly emitting far more telemetry.
3. Gateway tier: check queue depth and whether a backend is slow or down.
4. Short-term mitigations: raise `memory_limiter` limits, scale the gateway, or increase sampling aggressiveness to cut volume.

### TelemetryQueueFilling

The backend is accepting data more slowly than it arrives. This is the warning before the loss.

1. Which exporter? Check that backend's health directly.
2. Tempo/Loki/Prometheus ingestion problems show up here first.
3. The persistent queue buys time, not immunity.

### TelemetryExportFailing

1. Is the backend up? `curl localhost:3200/ready`, `curl localhost:3100/ready`.
2. Check gateway logs for the underlying error.
3. With a persistent queue this self-heals once the backend returns — confirm the queue is draining rather than growing.

### CollectorDown

1. `docker compose ps otel-agent otel-gateway`
2. `docker compose logs otel-gateway --tail 50` — remember a single failing component takes the whole process down.
3. Check `curl localhost:13133/` (agent) and `curl localhost:13134/` (gateway).

---

## Two traps worth your time

**Prometheus normalises `le` label values, and the exposition endpoint lies to you.** The collector exposes `le="500"`; Prometheus stores `le="500.0"`. A selector copied from `/metrics` matches **nothing** — and the rule still reports `health: "ok"`, because an empty result is not an error. Our entire latency SLO silently produced no data until we checked:

```bash
curl -s localhost:9090/api/v1/label/le/values
```

Always verify a recording rule actually produces a series. `health: ok` only means it did not error.

**Rules evaluate on their own schedule.** More than once we queried a recorded series immediately after a change, saw nothing, and started debugging something that was fine. Recording rules run on `interval` (15s here), and alerts additionally wait out their `for:` duration in `PENDING`. Give it a cycle before concluding anything.

## Exercises

1. **Fire a page yourself.** Set `PAYMENTS_FAILURE_RATE=0.35`, `docker compose up -d payments`, and watch `docker compose logs -f alertsink`. Then set it back to `0.0` and watch RESOLVED arrive.
2. **Watch the two windows diverge.** Open the SLO dashboard's multi-window panel during that exercise. The short line spikes and recovers; the long line lags. That gap is the whole design.
3. **Break the SLI deliberately.** Change `5..` to `[45]..` in `slo-recording.yml` so 402s count, reload Prometheus, and watch the budget drain from ordinary declined cards.
4. **Prove inhibition works.** Make `TelemetryDataLoss` fire (drop `memory_limiter` to `limit_percentage: 1`) and confirm the SLO alerts get suppressed in Alertmanager.
5. **Trigger OrdersStopped.** Stop the `orders` service but leave the gateway running. Note that it pages for a reason no HTTP metric would reveal.
6. **Write one runbook.** Pick an alert and write what *you* would want to read at 3am. That is the real test of an alert's design.
