# Phase 6 — Break it on purpose

No new infrastructure. Instead we deliberately break the system eight ways and walk each failure down the golden path, so you learn the *shape* of each one rather than the definition.

Every scenario changes environment variables or container state — never code. In production you cannot patch the application to reproduce an incident, so the lab does not let you either.

```bash
python3 tools/chaos.py list
python3 tools/chaos.py start pool-exhaustion
python3 tools/chaos.py observe        # symptom -> service -> trace -> log line
python3 tools/chaos.py stop
```

## Signature table — diagnose by shape

The fastest diagnosis is pattern recognition. These five columns distinguish every scenario below.

| Failure | Errors | p50 | p99 | In-flight | Where errors appear |
|---|---|---|---|---|---|
| **Downstream latency** | none | **up** | up | flat | nowhere |
| **Cascading timeout** | high | normal | **pinned at timeout** | mild | *upstream* of the cause |
| **Dependency failing** | high | normal | normal | flat | at the failing service |
| **Dependency down** | high | **fast** | fast | flat | connection-refused, no server span |
| **Slow query** | none | normal | **up** | mild | nowhere (bimodal) |
| **Pool exhaustion** | high | **normal** | **catastrophic** | **climbing** | timeouts upstream |
| **Retry storm** | high | up | up | up | dependency load > caller load |
| **Memory leak / OOM** | bursty | normal | normal | — | connection refused, process restarts |

The two most confusing rows are **cascading timeout** and **pool exhaustion**, because in both the service that is actually broken reports **zero errors**.

---

## Healthy baseline

Every comparison below is against this:

```
  checkout throughput :     4.62 req/s
  checkout error rate :     0.19 %
  checkout p50        :     75.0 ms
  checkout p99        :     99.6 ms
  budget remaining    :    0.915
  burn rate (1h)      :     0.14x

  service               req/s    5xx%    p50 ms    p99 ms
  shop/gateway           4.62    0.19      75.0      99.6
  shop/inventory         4.48    0.00       2.6       8.3
  shop/orders            4.59    0.19      70.6      99.5
  shop/payments          4.56    0.00      58.0      74.9

  in-flight requests: all 0
```

---

## 1. Connection pool exhaustion

**What broke:** inventory's Postgres pool cut to one connection, queries slowed to 300ms, traffic raised.

**The symptom:**

```
  checkout error rate :    19.82 %
  checkout p50        :     81.9 ms      <- barely moved
  checkout p99        :   7348.7 ms      <- catastrophic
  budget remaining    :   -8.533         <- budget overspent 8x over
  burn rate (1h)      :    12.53x

  service               req/s    5xx%    p50 ms    p99 ms
  shop/gateway           9.33   19.82      81.9    7348.7
  shop/inventory         6.44    0.00       3.0   10000.0
  shop/orders           11.27   26.27      77.4    7342.3
  shop/payments         11.11    0.00      57.7      74.8

  in-flight requests:
    shop/inventory         174        <-- THE SIGNATURE
    shop/orders             30
    shop/gateway            30
```

**The tell:** `p50` is *normal* while `p99` is 10 seconds, and in-flight requests climb into the hundreds. Requests either get a connection immediately (fast) or queue behind one (forever). An average over those two populations is meaningless.

Note inventory reports **0% errors**. It is not failing. It is answering, slowly, and the errors surface in `orders` as timeouts.

**The trace — and the part no dashboard shows:**

```
trace 143037859262b94a792ae2a0a6899f16

  inventory POST /reserve    start=+     23.8ms  end=+  28289.7ms  dur= 28265.9ms  [fastapi]
  inventory GET              start=+     24.5ms  end=+     24.8ms  dur=     0.3ms  [redis]
  inventory UPDATE           start=+  28287.0ms  end=+  28288.5ms  dur=     1.5ms  [asyncpg]
  inventory SELECT           start=+  28288.7ms  end=+  28289.0ms  dur=     0.2ms  [asyncpg]
  inventory SETEX            start=+  28289.1ms  end=+  28289.5ms  dur=     0.4ms  [redis]

  >>> Redis GET finishes at   +25ms
  >>> first Postgres call at  +28287ms
  >>> GAP = 28.3 SECONDS waiting for a pool connection
```

The `UPDATE` itself took **1.5ms**. Postgres was idle for the whole 28 seconds. Query duration, active connections, database CPU — every database metric looks perfectly healthy.

**That gap exists in exactly one place: the trace.** This is the single best argument for distributed tracing in this entire course. No metric has it, because nothing is happening — the request is waiting, and waiting is not an event anyone thought to instrument.

**A second lesson from the same trace:**

```
  >>> orders' client span gave up after 5.0s (its timeout)
  >>> inventory kept working for 28.3s on a request nobody was waiting for
```

**A client timeout does not cancel the server's work.** Under saturation this is actively harmful: abandoned requests keep holding the exact resource everyone else is queued for. Timeouts without cancellation propagation make a pile-up worse, not better.

**The log line:**

```
  orders  ERROR  upstream timed out
          {'dependency': 'inventory', 'order_id': '42895413-...'}
          trace_id=15f297476331d2597fe3878d219044e3
```

---

## 2. Cascading timeout

**What broke:** payments slowed to 6s, past orders' 5s client timeout.

```
  service               req/s    5xx%    p50 ms    p99 ms
  shop/gateway           4.87   98.07      88.8    7445.5
  shop/orders            4.80   98.28      85.0    7444.9
  shop/payments          3.17    0.00      65.2    7421.2     <-- ZERO errors
```

**The tell:** two services at 98% errors, and the service causing it reports **none**. Payments is answering every request successfully — just slower than someone else's patience.

A direct request confirms the mechanism:

```
$ time curl localhost:8080/checkout ...
http=502
5.032 total        <- orders' 5s timeout, not payments' 6s response
```

**Diagnostic rule: errors propagate upward, so look for the *earliest* rise, not the largest.** The loudest service is usually the furthest from the cause.

**How to tell it from pool exhaustion:** here p99 pins at exactly the timeout value (5s/7.4s) and in-flight stays modest. In pool exhaustion p99 runs *past* any timeout and in-flight climbs without bound.

---

## 3. Retry storm

**What broke:** payments failing 40%, orders configured with 3 retries.

Measured over a controlled window, counting `POST /charge` spans per request:

```
  attempts-per-request distribution: {0: 5, 1: 16, 2: 11, 3: 5, 4: 3}
  mean attempts per request        : 1.62      (1.00 = no retries)
```

Exactly the theoretical value: `1 + 0.4 + 0.4² + 0.4³ = 1.62`. The struggling dependency receives **62% more load precisely because it is struggling**.

Up to 4 attempts appear in a single request — one initial plus three retries, as configured.

**Why this is worse than it looks:** amplification *multiplies* down a chain. If the gateway also retried three times, payments would see `1.62 × 1.62 ≈ 2.6x`. Four services deep with three retries each is 16x. The mitigation becomes the outage.

**The tell:** a dependency's inbound request rate exceeds its caller's. Compare `sum(rate(...{job="shop/payments"}[2m]))` against the same for `shop/orders`.

**A measurement trap we hit:** our first attempt measured the ratio from tail-sampled traces and got 1.00x — no amplification at all. Tail sampling preferentially keeps error traces, so the sampled set is *not* a random sample and ratios computed from it are biased. Measure amplification from **metrics**, which see every request, or from traces in a tightly controlled window. Sampled traces answer "what happened in this request", not "how often does this happen".

---

## 4. Memory leak → OOMKill

**What broke:** orders leaking 400KB per request against a 256MB container limit.

```
  round  1: mem= 63.07MiB / 256MiB  restarts=0
  round  4: mem=105.8MiB  / 256MiB  restarts=0
  round  8: mem=186.4MiB  / 256MiB  restarts=0
  round 11: mem=249.3MiB  / 256MiB  restarts=0
  round 13: mem=256MiB    / 256MiB  restarts=0     <- pinned at the ceiling
  ...
  r5:       mem= 74.92MiB / 256MiB  restarts=1     <- killed and restarted
```

**The tell:** memory climbing *linearly with request count* — not with time, not with traffic spikes. A leak is proportional to work done.

**The error signature is connection-refused, not 5xx.** The process does not degrade and return errors; it vanishes mid-request. Everything in flight is lost without a response. If you are looking for 5xx you will miss it entirely.

**A gotcha worth knowing:** `docker inspect --format '{{.State.OOMKilled}}'` reported `false` even immediately after the kill, because that field describes the *current* state of a container that has already restarted. Use `RestartCount`, or Kubernetes' `lastState.terminated.reason`, instead.

---

## 5. Downstream latency (nothing errors)

**What broke:** payments slowed from 40–70ms to 700–1000ms. No errors anywhere. Every health check passes.

This is the failure mode a pure error-rate alert **misses entirely** — which is exactly why the SLO set in Phase 5 includes a latency objective alongside availability. One is not a substitute for the other.

**The tell:** the fan-out in `orders` is only as fast as its slower branch, so orders' duration tracks payments' almost exactly while inventory stays flat.

---

## 6. Dependency down

**What broke:** the payments container stopped. Not slow, not erroring — absent.

**The tell:** failures are *fast*, not slow. The log line is distinctive:

```
  orders  ERROR  upstream call failed
          {'dependency': 'payments', 'error': 'All connection attempts failed'}
```

And in the trace: a **client span with no corresponding server span**, because the server never saw the request. Compare with a 503, where both spans exist and the server span carries the error.

Learn these two shapes on sight — they call for completely different responses. Connection refused means the process is gone or the network is broken; a 503 means it is alive and saying no.

---

## 7. Slow query (bimodal latency)

**What broke:** inventory's stock lookup slowed to 600ms with a 2s cache TTL.

**The tell:** split inventory latency by `cache.hit` in Tempo and you get two clean, separate populations. p50 may look entirely acceptable while p99 is terrible — the Phase 1 `cache.hit` attribute exists precisely to make this one query possible.

```traceql
{ resource.service.name = "inventory" && .cache.hit = false }
```

---

## 8. Dependency failing (the baseline incident)

**What broke:** payments returning 503 for 40% of requests. The most ordinary incident there is, and the control case for everything above.

**The tell:** errors at the failing service *and* above it, with latency unchanged. This is the only scenario where the error rate points directly at the culprit — which is why the others are worth studying.

It also shows tail sampling adapting: the keep-rate rose from 15.4% to 27.3% because every error trace is retained.

---

## What this phase actually teaches

**Four of eight scenarios produce zero errors at the broken service.** Cascading timeout, pool exhaustion, downstream latency and slow query all report the culprit as healthy. If your mental model is "find the service with errors", you will diagnose four of these wrongly.

**The most valuable evidence was a gap, not an event.** The 28-second pool wait was invisible to every metric and every log line, because nothing happened — and "nothing happened for 28 seconds" is only representable in a trace.

**Sampled data answers "what happened here", not "how often".** Our retry amplification measured 1.00x from sampled traces and 1.62x from a controlled count. Use metrics for rates and ratios; use traces for structure and causality.

**Symptom-based alerting earns its keep here.** One rule — "checkout is failing or slow" — catches all eight, including the four that would defeat any cause-based alert you could have written in advance.

## Exercises

1. **Run all eight blind.** Have someone start a scenario without telling you which. Diagnose it from `observe` output alone, using the signature table. That skill is the entire point.
2. **Fix pool exhaustion two ways.** Raise `INVENTORY_POOL_SIZE`, then instead lower `INVENTORY_SLOW_QUERY_MS`. Both work. Which would you do at 3am, and which is the real fix?
3. **Add jittered backoff.** Set `ORDERS_RETRY_BACKOFF_MS=200` and re-measure the amplification. Then reason about why backoff alone is not enough without a circuit breaker.
4. **Find the cancellation bug.** In the pool-exhaustion trace, inventory worked 28s on a request abandoned at 5s. Make orders propagate cancellation and watch in-flight stop climbing.
5. **Write the missing alert.** Nothing currently pages on in-flight requests climbing. Should it? Write the rule, then argue against it using Phase 5's reasoning.
6. **Break something not on this list.** Stop Redis. Stop Postgres. Stop the collector. Predict the signature first, then check.
