## Monitoring (metrics + logs via Helm)

This repo uses:
- **kube-prometheus-stack** for **metrics/alerts/dashboards** (Prometheus Operator, Prometheus, Alertmanager, Grafana, kube-state-metrics, node-exporter)
- **Loki + Grafana Alloy** for **logs** (pod logs + node journald)

### Install / upgrade (latest charts)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  -n observability --create-namespace \
  -f application/monitoring/kube-prometheus-stack-values.yaml

helm upgrade --install loki grafana/loki \
  -n observability \
  -f application/monitoring/loki-values.yaml

helm upgrade --install alloy grafana/alloy \
  -n observability \
  -f application/monitoring/alloy-values.yaml

kubectl apply -f application/monitoring/mvp-alerts.yaml
```

If Loki install fails due to TLS/caching issues on your machine, see the fallback commands at the bottom of this README.

### Access Grafana

```bash
kubectl -n observability port-forward svc/monitoring-grafana 3000:80
```

Login: `admin` / `admin`

Grafana is configured to use:
- **Prometheus** (default)
- **Loki** (additional datasource; see `kube-prometheus-stack-values.yaml`)

### Grafana Ingress (GKE)

Grafana is exposed via a **GCE Ingress** by default. Right now it’s configured with **no host** (matches any Host header).
Later, if you want DNS, set `grafana.ingress.hosts[0]` in `kube-prometheus-stack-values.yaml`.

After `helm upgrade`, check:

```bash
kubectl -n observability get ingress
kubectl -n observability describe ingress monitoring-grafana
```

### Logs coverage

- **Pod logs**: all pod/container logs (stdout/stderr) via Kubernetes API (pods/log)
- **Node system logs**: journald via `/var/log/journal` (and `/run/log/journal` if present)

### Loki labels you can query

With Alloy, you can query:
- **Pod logs**: `{pod=~".+"}` (and filter by `namespace`, `pod`, `container`, etc. as present)
- **Journald**: `{job="node-journald"}`

### If you see *no logs at all*

After upgrading Loki + Alloy, verify:

```bash
kubectl -n observability get pods -l app.kubernetes.io/name=alloy -o wide
kubectl -n observability logs -l app.kubernetes.io/name=alloy --tail=200
kubectl -n observability get svc | grep loki
```

Typical causes:
- **Alloy not scheduled on nodes** (taints/tolerations) → we set `controller.tolerations: [{operator: Exists}]`
- **Alloy can’t reach Loki service** → ensure Loki is `Running` and service name resolves (`http://loki.observability.svc.cluster.local:3100`)
- **Loki rejects streams** (limits / cardinality) → check Alloy logs for HTTP 400/429 and trim labels in `alloy-values.yaml`

### Verify MVP alerts

```bash
kubectl -n observability get prometheusrule mvp-runbook-alerts
kubectl -n observability port-forward svc/monitoring-kube-prometheus-alertmanager 9093:9093
# open Alertmanager UI on http://localhost:9093
```

### Loki install fallback (TLS/caching issues)

If you hit errors like:
- `tls: failed to verify certificate: x509: ...`
- Helm cache write errors under `~/Library/Caches/...`

Use workspace-local Helm dirs and install from a locally-downloaded chart:

```bash
cd /path/to/agent-sre-executor
export HELM_CACHE_HOME="$PWD/.helm/cache" HELM_CONFIG_HOME="$PWD/.helm/config" HELM_DATA_HOME="$PWD/.helm/data" XDG_CACHE_HOME="$PWD/.cache"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME" "$XDG_CACHE_HOME"

# Download the chart tarball (adjust version if needed)
curl -L -k -o loki.tgz "https://github.com/grafana/helm-charts/releases/download/helm-loki-6.44.0/loki-6.44.0.tgz"

# Install from local tgz
helm upgrade --install loki ./loki.tgz -n observability --create-namespace -f application/monitoring/loki-values.yaml
```

### Notes (GKE)

On **managed control planes** (like GKE), some control-plane component targets (scheduler/controller-manager) may not be reachable. If you see targets consistently down, disable them in `kube-prometheus-stack-values.yaml`:
- `kubeControllerManager.enabled: false`
- `kubeScheduler.enabled: false`

### Prometheus metric labels (resource identity)

You should already have rich labels from the stack:
- **Node** identity: `kube_node_info` (kube-state-metrics), and kubelet/node-exporter series include `instance` (node address) + node labels.
- **Pod** identity: `kube_pod_info` includes `namespace`, `pod`, and `node` (where scheduled).

We also set a stable cluster label on all series via `prometheus.prometheusSpec.externalLabels.cluster` in `kube-prometheus-stack-values.yaml`.


