# Phase 7 — Lift to Kubernetes

The same application, the same images, the same two collector tiers — now on a real cluster. The backends stay on the Docker host, which is also how most organisations run it: workloads in Kubernetes, telemetry to a managed or separately-operated backend.

```text
kind cluster "otel-lab"                           Docker host
┌──────────────────────────────────────┐         ┌──────────────────┐
│ ns: shop                             │         │  Tempo  :14317   │
│   gateway x2  orders x2              │         │  Loki   :3100    │
│   payments x2 inventory x2           │         │  Prom   :9090    │
│   loadgen     postgres  redis        │         └──────────────────┘
│        │ (annotation-injected SDK)             ▲
│        ▼                                       │
│ ns: observability                              │
│   otel-agent    (DaemonSet)  ──load_balancing──┤
│   otel-gateway  (Deployment x2) ───────────────┘
└──────────────────────────────────────┘
```

## Setup

```bash
kind create cluster --config k8s/kind-cluster.yaml
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.yaml
kubectl wait --for=condition=Available --timeout=240s deployment --all -n cert-manager
kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/download/v0.160.0/opentelemetry-operator.yaml
kubectl wait --for=condition=Available --timeout=300s deployment -n opentelemetry-operator-system --all

for i in gateway orders payments inventory loadgen; do
  kind load docker-image --name otel-lab "lab-${i}:latest"     # NOTE THE BRACES
done

kubectl create namespace shop
kubectl create namespace observability
kubectl create configmap pg-init --from-file=init.sql=lab/postgres/init.sql -n shop

kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/external-backends.yaml
kubectl apply -f k8s/collectors.yaml
kubectl apply -f k8s/instrumentation.yaml    # BEFORE the app — see below
kubectl apply -f k8s/app.yaml
```

Teardown: `kind delete cluster --name otel-lab`.

## The headline: instrumentation by annotation

`k8s/app.yaml` contains **zero OpenTelemetry configuration**. No SDK env vars, no endpoint, no sidecar, no changed image. One line per Deployment:

```yaml
annotations:
  instrumentation.opentelemetry.io/inject-python: "true"
```

The Operator's mutating webhook rewrites the pod at admission. Verified on a running pod:

```text
init containers injected:
  opentelemetry-auto-instrumentation-python

OTEL env vars injected into the app container:
  OTEL_NODE_IP, OTEL_POD_IP, OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL,
  OTEL_SERVICE_NAME, OTEL_TRACES_EXPORTER, OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER,
  OTEL_PROPAGATORS, OTEL_TRACES_SAMPLER, OTEL_RESOURCE_ATTRIBUTES, ... (18 total)

PYTHONPATH=/otel-auto-instrumentation-python/opentelemetry/instrumentation/auto_instrumentation:
           /otel-auto-instrumentation-python
```

Note the mechanism: the Operator puts the SDK on `PYTHONPATH` rather than wrapping the entrypoint, so the container command is untouched and `opentelemetry-instrument` is never invoked. **These are the identical images Compose uses.** Nothing was rebuilt.

Valid annotations: `inject-java`, `inject-python`, `inject-nodejs`, `inject-dotnet`, `inject-go`, `inject-sdk` (Deno).

### Ordering is a real footgun

The `Instrumentation` resource must exist **before** annotated pods start. A pod admitted while it is missing gets nothing injected — silently, with no error anywhere. The fix is `kubectl rollout restart`, but you have to notice first.

## What changed, honestly

| | Compose | Kubernetes |
|---|---|---|
| Turning telemetry on | env var per service | one pod annotation |
| SDK config | 12 env vars in compose.yml | one `Instrumentation` resource |
| Agent tier | a container | `mode: daemonset` |
| Gateway tier | a container | `mode: deployment`, `replicas: 2` |
| Pod/deployment metadata | impossible | `k8s_attributes` processor |
| Log collection | docker log driver | `file_log` reading `/var/log/pods` |
| Scaled tail sampling | N/A (one instance) | `load_balancing` + `routing_key: traceID` |
| Permissions | none | RBAC, because the collector now reads the API |

**About 85% transfers unchanged.** Every processor, every OTTL statement, every tail-sampling policy, every PromQL query and every dashboard from Phases 1–6 works here untouched. What changed is deployment shape, enrichment, and log plumbing.

## k8s_attributes — the biggest Kubernetes-specific win

Every span, metric and log now carries where it came from:

```text
  k8s.cluster.name     = otel-lab
  k8s.namespace.name   = shop
  k8s.deployment.name  = orders
  k8s.replicaset.name  = orders-86959dc589
  k8s.pod.name         = orders-86959dc589-prlt7
  k8s.pod.uid          = 481e23b9-8d17-49c7-8727-82b130ec3da8
  k8s.node.name        = otel-lab-worker
  k8s.container.name   = orders
```

That is what makes "which deployment regressed?" and "is it only pods on that node?" answerable. On Compose it was not merely unconfigured — it was impossible, because there was no API to ask.

```yaml
k8s_attributes:
  auth_type: serviceAccount
  filter:
    node_from_env_var: K8S_NODE_NAME     # <-- do not omit this
```

`node_from_env_var` restricts each agent to pods on **its own node**. Without it, every agent on every node watches every pod in the cluster. On a large cluster that is how you take down the API server with your observability stack.

## load_balancing — what makes scaled tail sampling correct

The gateway has 2 replicas. A plain OTLP exporter round-robins spans, so each gateway would see **fragments** of every trace — and a tail sampler deciding on a fragment makes garbage decisions. **It does not error. It just produces wrong data.**

```yaml
load_balancing:
  routing_key: traceID        # consistent-hash every span of a trace to one pod
  protocol:
    otlp: { tls: { insecure: true } }
  resolver:
    k8s:
      service: otel-gateway-collector.observability    # watches EndpointSlices
```

Accepted `routing_key` values: `service`, `traceID`, `resource`, `metric`, `streamID`, `attributes`, `randomness`.

**How we verified it works:** 23 of 25 sampled traces span all five services, across four different pods.

```text
    +   0.0ms  loadgen   POST                71.5ms   pod=loadgen-6cb5dd796f-45gms
    +   0.7ms  gateway   POST /checkout      70.6ms   pod=gateway-5468499ffb-ksh5q
    +   1.8ms  orders    POST /orders        68.7ms   pod=orders-86959dc589-prlt7
    +  15.5ms  payments  POST /charge        53.1ms   pod=payments-699bbd5f88-5xjn9
    +  15.7ms  inventory POST /reserve        3.0ms   pod=inventory-5f5446fb4-lt4mt
```

Complete traces surviving tail sampling across two gateway replicas *is* the proof of trace affinity. Broken routing would show up as truncated traces, not as an error message.

## file_log — logs the Kubernetes way

There is no Docker log driver to lean on. The agent reads container stdout straight off the node filesystem:

```yaml
file_log:
  include: [/var/log/pods/*/*/*.log]
  exclude: [/var/log/pods/observability_*/**/*.log]   # don't ingest your own logs
  start_at: end
  operators:
    - type: container      # parses the CRI format and recovers names from the file PATH
      id: container-parser
```

That `exclude` matters: without it the collector reads its own logs, which it then logs about, which it then reads. Verified result — full k8s metadata *and* `trace_id`, so the golden path works unchanged:

```text
  line: order placed
    k8s_deployment_name  = orders
    k8s_namespace_name   = shop
    k8s_node_name        = otel-lab-worker
    k8s_pod_name         = orders-86959dc589-prlt7
    service_name         = orders
    trace_id             = 60c3f1438a9da1b8428706e07c420a4d
```

## Metrics: the direction flips

Prometheus cannot scrape pods inside kind, so the gateway **pushes**:

```yaml
prometheus_remote_write:
  endpoint: http://prometheus-external.observability.svc.cluster.local:9090/api/v1/write
  target_info: { enabled: true }
  resource_to_telemetry_conversion: { enabled: false }
```

Requires `--web.enable-remote-write-receiver` on Prometheus. Same trade-off you face at any network boundary.

`resource_to_telemetry_conversion: false` keeps resource attributes out of every series (Phase 2's cardinality lesson) and puts them in `target_info` instead. The cost is that filtering by them requires a join:

```promql
http_server_duration_milliseconds_count
  * on (job, instance) group_left(k8s_node_name) target_info
```

## Five traps, all of which cost real time

**1. OTLP ports are not interchangeable.** The Operator's Python default protocol is `http/protobuf`, and our `Instrumentation` pointed at **4317** (gRPC). Result:

```text
BadStatusLine('\x00\x00\x06\x04\x00\x00\x00\x00\x00\x00\x05\x00\x00@\x00')
```

That is a Python HTTP/1.1 client trying to parse an HTTP/2 gRPC frame as a status line. Use **4318** for `http/protobuf`, **4317** for `grpc`, and nothing else.

**2. `host.docker.internal` resolves to IPv6 only inside kind.** `curl` quietly fell back to IPv4 and reported success; gRPC picked IPv6 and failed with `network is unreachable`. **Test connectivity with the protocol you will actually use.** The fix is a selector-less Service plus Endpoints pointing at the kind network's IPv4 gateway — which is also the standard pattern for any external dependency:

```bash
docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}'
```

**3. Your DaemonSet does not cover every node.** Two nodes, `desiredNumberScheduled: 1` — the control-plane node carries a `NoSchedule` taint, so no agent runs there and anything scheduled on it emits nothing. Production agents need tolerations.

```text
  otel-lab-control-plane   [map[effect:NoSchedule key:node-role.kubernetes.io/control-plane]]
  otel-lab-worker          <none>
```

**4. Two environments collide in one backend.** Both the Compose stack and the cluster set `service.namespace=shop`, so `job="shop/orders"` now has 12 `target_info` series spanning `deployment.environment` of `lab` and `kind`. Queries mix them and joins fail with `found duplicate series for the match group`. If you send multiple environments to one Prometheus, make `service.namespace` or an external label distinguish them from day one.

**5. zsh ate `:latest`.** `kind load docker-image "$img:latest"` silently became `lab-ordersatest`, because `:l` is zsh's lowercase parameter modifier. Use `"${img}:latest"`.

## Production notes this lab simplifies

**Node-local agent routing.** The `Instrumentation` points at the agent's *Service*, which load-balances across all agents and defeats node-locality. The real pattern uses the downward API:

```yaml
env:
  - name: NODE_IP
    valueFrom: { fieldRef: { fieldPath: status.hostIP } }
exporter:
  endpoint: http://$(NODE_IP):4318
```

**Also missing here, and worth adding next:** the `k8s_cluster` receiver (single replica only) for cluster-state metrics, `kubelet_stats` for container CPU/memory, alerts on `CrashLoopBackOff` / `OOMKilled` / pods Pending / HPA at max, and a `PodDisruptionBudget` plus `HorizontalPodAutoscaler` on the gateway.

## Exercises

1. **Watch the webhook work.** `kubectl get pod -o yaml` on an app pod, find the init container and the 18 injected env vars, then remove the annotation, restart, and watch telemetry vanish without a single error.
2. **Create the ordering bug deliberately.** Delete the `Instrumentation`, restart a Deployment, and confirm the pods come up healthy and silent.
3. **Break trace affinity.** Swap `load_balancing` for a plain `otlp_grpc` exporter to the gateway Service, then count services-per-trace. Fragments, no errors.
4. **Scale the gateway.** `kubectl scale deployment otel-gateway-collector -n observability --replicas=4` and confirm the k8s resolver picks up the new pods without a restart.
5. **Add tolerations** so the agent DaemonSet also covers the control-plane node.
6. **Fix the label collision.** Change the cluster's `service.namespace` to `shop-kind` and make the `target_info` join work.
7. **Run a Phase 6 scenario here.** Set `PAYMENTS_FAILURE_RATE=0.4` on the Deployment and confirm the same signature appears — now with pod and node attribution the Compose version could not provide.
