# Agentic SRE Executor

An autonomous Site Reliability Engineering agent that monitors a Kubernetes cluster, detects infrastructure incidents via Alertmanager webhooks, and automatically diagnoses and remediates them using LLM-guided runbook workflows — with full audit trails, historical pattern analysis, and a built-in incident management UI.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works — End-to-End Flow](#how-it-works--end-to-end-flow)
- [Runbooks](#runbooks)
- [Tools](#tools)
- [Agent Modes](#agent-modes)
- [Historical Pattern Analysis](#historical-pattern-analysis)
- [Deduplication](#deduplication)
- [Monitoring Stack](#monitoring-stack)
- [Database](#database)
- [API Reference](#api-reference)
- [Incident UI](#incident-ui)
- [Kubernetes Setup](#kubernetes-setup)
- [Configuration](#configuration)

---

## Overview

The Agentic SRE Executor bridges the gap between alert firing and remediation. When Prometheus detects an issue — a pod stuck in `ImagePullBackOff`, a node going `NotReady`, a container getting `OOMKilled` — Alertmanager fires a webhook to the agent. The agent then:

1. **Identifies** the correct runbook for the alert type
2. **Runs a structured diagnostic workflow** using LLM-guided tool calls against the Kubernetes API
3. **Takes action** (or recommends one) based on what the diagnostics reveal
4. **Generates a post-incident analysis** enriched with full historical context of past similar incidents
5. **Stores everything** — events, actions, analysis — in PostgreSQL for audit and review

No human needs to be paged for common, well-understood failure modes. For complex or recurring patterns, the analysis tells the SRE team exactly what happened and why the short-term fix is not enough.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                           │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  Demo App    │    │  Prometheus  │    │    Alertmanager      │  │
│  │  (workload)  │───▶│  (scraping)  │───▶│  (alert routing)     │  │
│  └──────────────┘    └──────────────┘    └──────────┬───────────┘  │
│                                                     │ webhook       │
│                            ┌────────────────────────▼────────────┐ │
│                            │         Agentic SRE Agent           │ │
│                            │                                      │ │
│                            │  ┌──────────┐   ┌────────────────┐  │ │
│                            │  │ FastAPI  │   │   LangGraph    │  │ │
│                            │  │ Webhook  │──▶│  State Machine │  │ │
│                            │  │ Server   │   └───────┬────────┘  │ │
│                            │  └──────────┘           │           │ │
│                            │                         │ tool calls │ │
│                            │  ┌──────────┐   ┌───────▼────────┐  │ │
│                            │  │PostgreSQL│   │  Kubernetes    │  │ │
│                            │  │  (audit) │   │   API (RBAC)   │  │ │
│                            │  └──────────┘   └────────────────┘  │ │
│                            │         │                            │ │
│                            │  ┌──────▼──────┐                    │ │
│                            │  │  OpenAI API │                    │ │
│                            │  │  (LLM calls)│                    │ │
│                            │  └─────────────┘                    │ │
│                            └─────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Role |
|-----------|------|
| **Prometheus + kube-state-metrics** | Scrape Kubernetes state and expose alert metrics |
| **Alertmanager** | Evaluate alert rules, group alerts, fire webhooks |
| **FastAPI Webhook Server** | Receive alerts, manage incidents, serve UI |
| **LangGraph State Machine** | Orchestrate runbook workflows as a directed graph |
| **OpenAI LLM** | Guide tool selection at each workflow step, generate incident analysis |
| **Kubernetes API** | Source of truth for pod/node state; target for remediation patches |
| **PostgreSQL** | Incident registry, full event audit trail, past-incident history |
| **Grafana + Loki** | Observability dashboards and log aggregation |

---

## How It Works — End-to-End Flow

### Phase 1 — Alert Ingestion

```
Alertmanager
    │
    │  POST /alertmanager  (webhook payload)
    ▼
FastAPI receives alert batch
    │
    ├── Extract labels (alertname, namespace, pod, container, node, runbook_id)
    ├── Compute fingerprint  →  alertname:namespace:pod:container
    ├── Upsert incident row in PostgreSQL  (fingerprint = dedup key)
    ├── Log webhook_received event
    │
    └── Try PostgreSQL advisory lock on fingerprint
           ├── Lock busy  →  log suppressed event, return immediately  (dedup)
           └── Lock acquired  →  proceed to Phase 2
```

### Phase 2 — LangGraph Execution

```
graph.invoke(state)
    │
    ▼
[ route node ]
    │  Inspect alert labels → map to runbook_id
    │
    ├── RB_IMAGEPULL        ──▶ [ imagepull_llm_patch ]
    ├── RB_OOM              ──▶ [ oom_llm_patch ]
    ├── RB_CONTAINERCREATING──▶ [ containercreating_llm_patch ]
    ├── RB_CRASHLOOP        ──▶ [ crashloop_llm_patch ]
    ├── RB_NODE_UNSCHEDULABLE──▶[ node_unschedulable_llm_patch ]
    ├── RB_NODE_NOTREADY    ──▶ [ node_notready_llm_patch ]
    └── RB_UNKNOWN          ──▶ END (no action)
```

### Phase 3 — Runbook Workflow Execution

Each runbook handler follows the same pattern:

```
Load runbook YAML frontmatter
    │
    ▼
For each workflow step:
    │
    ├── Check 'when' / 'when_all' gate conditions
    │      └── Condition false? → skip step, continue
    │
    ├── Ask LLM: "You MUST call tool: {expected_tool}. Use this alert context."
    │      └── LLM returns tool call with arguments
    │
    ├── Validate LLM chose the allowed tool (not an arbitrary one)
    │
    ├── Execute tool against Kubernetes API
    │      ├── mode=auto    → mutate immediately (patch, delete, evict)
    │      └── mode=recommend → return action string only
    │
    ├── Store result in tool_results (for next step's context)
    └── Append to rb_steps audit trail
```

### Phase 4 — Analysis & Persistence

```
Graph completes
    │
    ├── Update incident.runbook_id in DB
    ├── Log final event (full agent state + rb_steps)
    │
    ├── Query past similar incidents (same alertname / namespace+pod / node)
    │      └── Full history, no time window, up to 50 records
    │
    ├── Call generate_incident_analysis() with:
    │      ├── Alert labels + annotations
    │      ├── Final agent state (action taken/recommended, rb_steps)
    │      └── Past incidents list (for pattern detection)
    │
    ├── LLM writes structured markdown analysis including:
    │      ├── Summary
    │      ├── What happened (evidence)
    │      ├── Root cause hypothesis
    │      ├── Action taken / recommended
    │      ├── Why that action
    │      ├── Historical pattern & SRE recommendation  ← enriched with history
    │      └── Follow-ups
    │
    ├── Log analysis event to PostgreSQL
    └── Release advisory lock
```

---

## Runbooks

Runbooks are YAML-frontmatter Markdown files that define the ordered workflow steps, conditional gates, and metadata for each alert type. The agent loads them at runtime and executes their `workflow` array step by step.

### RB_IMAGEPULL — ImagePullBackOff

**Trigger**: Pod stuck waiting with `ImagePullBackOff` or `ErrImagePull`

**Workflow**:
```
1. get_pod_events      → Confirm image pull errors in event stream
2. patch_image         → Patch Deployment to fallback image
```

**Safety**: Uses a pre-configured fallback image from the runbook metadata. Never invents image names.

---

### RB_OOM — OOMKilled

**Trigger**: Container terminated with reason `OOMKilled`

**Workflow**:
```
1. increase_memory_limit → Double current limit (min 256Mi, cap 4Gi)
```

**Strategy**: Conservative step-up: if current limit < 256Mi → set to 256Mi. Otherwise 2× current, capped at 4Gi. Historical analysis will flag if this action keeps recurring.

---

### RB_CONTAINERCREATING — ContainerCreating Stuck

**Trigger**: Container stuck in `ContainerCreating` state beyond threshold

**Workflow** (conditional):
```
1. get_pod_events           → Diagnose root cause from event stream
2. check_imagepullbackoff   → Detect image pull failures
3. patch_image              → (only if imagepull detected)
4. check_oom                → Detect OOM-related failures
5. increase_memory_limit    → (only if OOM detected)
```

**Logic**: Diagnosis first, then targeted remediation. Steps 3 and 5 are gated — only run if the preceding check confirms that failure mode.

---

### RB_CRASHLOOP — CrashLoopBackOff

**Trigger**: Container in `CrashLoopBackOff`

**Workflow** (conditional — same gates as ContainerCreating):
```
1. get_pod_events           → Diagnose crash pattern
2. check_imagepullbackoff   → Detect image errors
3. patch_image              → (only if imagepull detected)
4. check_oom                → Detect OOM cause of crash
5. increase_memory_limit    → (only if OOM detected)
```

**Note**: Future actions `rollback_deployment` and `increase_resources` are defined in the runbook but not yet wired to tools.

---

### RB_NODE_UNSCHEDULABLE — Node Cordoned

**Trigger**: Node has `spec.unschedulable = true`

**Workflow** (all-gate on uncordon):
```
1. get_node_ready      → Check Ready condition
2. get_node_conditions → Check pressure / unavailable conditions
3. uncordon_node       → (only if: node is Ready AND unschedulable AND all conditions healthy)
```

**Safety**: Will NOT uncordon a node that has memory pressure, disk pressure, or other unhealthy conditions — even if it was manually cordoned.

---

### RB_NODE_NOTREADY — Node NotReady

**Trigger**: Node `Ready` condition is false

**Workflow**:
```
1. get_node_ready      → Confirm NotReady state
2. get_node_conditions → Record which conditions are failing
3. cordon_node         → Prevent new pods from scheduling (when NotReady confirmed)
4. drain_node          → Evict non-daemonset, non-system pods
```

**Safety**: Skips DaemonSet-managed pods, kube-system pods, and mirror pods during drain.

---

## Tools

The agent has 12 tools it can call against the Kubernetes API. Each tool returns a structured result that feeds into the next step of the workflow.

### Diagnostic Tools (read-only)

| Tool | Purpose | Key Output Fields |
|------|---------|-------------------|
| `get_pod_events` | Fetch recent K8s events for a pod | `oom_detected`, `sandbox_failure_detected`, `events[]` |
| `check_imagepullbackoff` | Detect ImagePullBackOff via pod status + events | `imagepull_detected`, `reasons[]` |
| `check_oom` | Detect OOMKilled via pod status + events | `oom_detected`, `reasons[]` |
| `get_node_ready` | Check node Ready condition | `ready`, `not_ready`, `unschedulable` |
| `get_node_conditions` | Check all non-Ready node conditions | `healthy`, `problems[]`, `conditions[]` |
| `get_runbook` | Fetch runbook config (fallback image etc.) | `fallback_image`, `runbook_id` |

### Remediation Tools (write operations)

| Tool | What It Does | Modes |
|------|-------------|-------|
| `fix_imagepullbackoff` | Patch Deployment image → fallback image | auto / recommend |
| `increase_memory_limit` | Patch Deployment memory limit (2× or 256Mi floor, 4Gi cap) | auto / recommend |
| `delete_pod` | Delete pod to force recreation (safe for Deployments) | auto / recommend |
| `cordon_node` | Set `spec.unschedulable = true` on node | auto / recommend |
| `uncordon_node` | Set `spec.unschedulable = false` on node | auto / recommend |
| `drain_node` | Evict non-daemonset pods via Eviction API | auto / recommend |

All remediation tools traverse the ownership chain (pod → ReplicaSet → Deployment) before patching, ensuring mutations hit the correct controller object.

---

## Agent Modes

The agent operates in one of two modes, set via the `AGENT_MODE` environment variable.

### `recommend` (default)

The agent diagnoses the incident and determines the correct action but **does not execute** any Kubernetes mutations. Instead, it returns a human-readable action string describing what it would do.

```
action_recommended: "patch_image:demo/app-deployment/app:us-docker.../hello-app:1.0"
```

Use this when you want the agent to advise on-call engineers without touching the cluster autonomously.

### `auto`

The agent executes remediations immediately — patching Deployments, deleting pods, cordoning/draining nodes — as soon as it determines the correct action.

```
action_taken: "patch_memory_limit:demo/app-deployment/app:256Mi→512Mi"
```

Use this for fully autonomous operation on well-understood, low-risk alert types.

Both modes produce a complete audit trail in PostgreSQL, including every diagnostic step, tool call result, and the final analysis.

---

## Historical Pattern Analysis

One of the key production-readiness features is the agent's ability to look at the **entire past history** of similar incidents when generating its post-incident analysis.

### How It Works

After every alert is processed, before writing the analysis, the agent queries PostgreSQL for all past incidents that share any of:

- The same **alert name** (e.g., all past `KubePodOOMKilled` alerts)
- The same **namespace + pod** (e.g., all past incidents for `demo/my-app-pod`)
- The same **node** (e.g., all past incidents touching `node-worker-3`)

Up to 50 past incidents are fetched, with no time window — the full history of the database is searched. For each past incident, the query returns:
- What runbook was used
- What action was taken or recommended
- Whether the action succeeded or errored
- When it happened

This history is passed directly to the LLM when generating the analysis, which then adds a **"Historical pattern & SRE recommendation"** section:

```
## Historical pattern & SRE recommendation

This pod has triggered KubePodOOMKilled 6 times in the past 3 weeks.
Each time, the agent increased the memory limit. The limit has now been
raised from 128Mi → 256Mi → 512Mi → 1Gi across multiple incidents.

The recurring pattern strongly suggests a memory leak rather than
under-provisioning. Recommended actions for the SRE team:
- Profile the application for memory leaks (heap dumps, pprof)
- Review recent code changes for unbounded cache/buffer growth
- Consider adding memory usage alerts at 80% to catch issues earlier
- If the application is stateless, a periodic pod restart may provide
  temporary relief while the root cause is investigated
```

### Re-generating Analysis

Any existing incident can have its analysis re-generated on demand via the **↻ Re-generate Analysis** button in the UI, or by calling `POST /api/incidents/{id}/regenerate-analysis`. This is useful for incidents that were processed before the history feature was available, or when new related incidents have since occurred.

---

## Deduplication

Alertmanager can fire the same alert multiple times while it is firing (typically every 30–60 seconds). The agent prevents duplicate processing using a two-layer mechanism:

### Layer 1 — Fingerprint-based Upsert

Every alert is mapped to a stable fingerprint:

```
{alertname}:{namespace}:{pod}:{container}
```

Or, if Alertmanager provides a `fingerprint` or `groupKey`, those are used directly. The `incidents` table has a `UNIQUE` constraint on `fingerprint`, so `upsert_incident()` is idempotent — repeated calls update the `updated_at` timestamp rather than creating duplicate rows.

### Layer 2 — PostgreSQL Advisory Locks

After upserting, the agent attempts to acquire a non-blocking PostgreSQL advisory lock keyed on the fingerprint's SHA-256 hash:

```
If lock acquired  → process this alert, release lock when done
If lock busy      → log a 'suppressed' event, return immediately
```

This ensures that even if two webhook calls for the same alert arrive simultaneously, only one agent workflow runs at a time. The entire lock + process + unlock cycle is wrapped in a `try/finally` so the lock is always released, even if the workflow errors.

---

## Monitoring Stack

The full observability stack is deployed alongside the agent.

### Components

| Component | Purpose |
|-----------|---------|
| **Prometheus** | Metric scraping and alert evaluation |
| **kube-state-metrics** | Expose Kubernetes object state as metrics |
| **node-exporter** | Node-level OS/hardware metrics |
| **Alertmanager** | Alert grouping, routing, and webhook dispatch |
| **Grafana** | Dashboards for metrics and logs |
| **Loki** | Log aggregation backend |
| **Grafana Alloy** | Log collection from pods and nodes |

### Alert Rules

Seven Prometheus alert rules trigger the agent:

| Alert | Condition | Fires After | Severity | Runbook |
|-------|-----------|-------------|----------|---------|
| `KubePodImagePullBackOff` | Pod waiting with `ImagePullBackOff` reason | 30s | warning | RB_IMAGEPULL |
| `KubePodOOMKilled` | Container last terminated with `OOMKilled` | 30s | warning | RB_OOM |
| `KubePodMemoryNearLimit` | Container memory > 90% of limit | 2m | warning | RB_OOM |
| `KubePodContainerCreatingStuck` | Pod stuck in `ContainerCreating` | 2m | warning | RB_CONTAINERCREATING |
| `KubePodCrashLoopBackOff` | Pod in `CrashLoopBackOff` | 2m | warning | RB_CRASHLOOP |
| `KubeNodeUnschedulable` | Node has `spec.unschedulable = true` | 1m | warning | RB_NODE_UNSCHEDULABLE |
| `KubeNodeNotReady` | Node `Ready` condition is false | 2m | critical | RB_NODE_NOTREADY |

Each alert carries a `runbook_id` label that the agent uses for direct runbook routing, bypassing alert name inference.

---

## Database

PostgreSQL stores all incident data. No time-series database is required — the agent's full state is captured in structured JSON events.

### `incidents` table

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigserial | Primary key |
| `fingerprint` | text (unique) | Dedup key for this alert instance |
| `alertname` | text | Prometheus alert name |
| `namespace` | text | Kubernetes namespace |
| `pod` | text | Pod name |
| `severity` | text | `warning` or `critical` |
| `runbook_id` | text | Which runbook handled this incident |
| `status` | text | `open`, `resolved`, or `suppressed` |
| `agent_mode` | text | `auto` or `recommend` at time of incident |
| `summary` | text | Human-readable one-line summary |
| `summary_embedding` | vector(1536) | Reserved for semantic similarity search |
| `created_at` | timestamptz | First seen |
| `updated_at` | timestamptz | Last updated |

### `incident_events` table

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigserial | Primary key |
| `incident_id` | bigint | Foreign key → incidents |
| `ts` | timestamptz | Event timestamp |
| `event_type` | text | `webhook_received`, `suppressed`, `final`, `analysis` |
| `payload` | jsonb | Event-specific structured data |

### Event Types

| Event Type | When Written | Key Payload Fields |
|------------|-------------|-------------------|
| `webhook_received` | On every alert arrival | `labels`, `annotations`, `cluster`, `startsAt`, `fingerprint` |
| `suppressed` | When dedup lock is busy | `reason`, `fingerprint` |
| `final` | After graph completes | `runbook_id`, `state` (action_taken, action_recommended, rb_steps, llm_trace) |
| `analysis` | After LLM generates analysis | `analysis_markdown`, `runbook_id`, `regenerated` |

---

## API Reference

### Webhook

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/alertmanager` | Receive Alertmanager webhook payload. Main entry point. |

### Incidents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/incidents` | List incidents, paginated (`limit`, `offset` query params) |
| `GET` | `/api/incidents/{id}` | Get single incident with events, analysis, and past similar incidents |
| `POST` | `/api/incidents/{id}/regenerate-analysis` | Re-generate analysis with full history context |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Kubernetes readiness probe endpoint |

### Response: `GET /api/incidents/{id}`

```json
{
  "incident": {
    "id": 42,
    "alertname": "KubePodOOMKilled",
    "namespace": "demo",
    "pod": "my-app-abc123",
    "runbook_id": "RB_OOM",
    "severity": "warning",
    "agent_mode": "auto",
    "action_taken": "patch_memory_limit:demo/my-app/app:256Mi→512Mi",
    "updated_at": "2024-01-15T10:30:00Z"
  },
  "events": [ ... ],
  "analysis_markdown": "## Summary\n...",
  "past_incidents": [
    {
      "id": 38,
      "alertname": "KubePodOOMKilled",
      "namespace": "demo",
      "pod": "my-app-abc123",
      "runbook_id": "RB_OOM",
      "action_taken": "patch_memory_limit:demo/my-app/app:128Mi→256Mi",
      "action_recommended": null,
      "action_error": null,
      "created_at": "2024-01-08T09:15:00Z"
    }
  ]
}
```

---

## Incident UI

The agent ships with a built-in web UI accessible at `/` on port 8080.

### Incidents List (`/`)

A table of all incidents ordered by most recently updated, showing:
- Incident ID (links to detail page)
- Alert name
- Namespace / Pod
- Node
- Runbook used
- Severity (colour-coded pill)
- Last updated timestamp

### Incident Detail (`/incident/{id}`)

Four sections per incident:

**Analysis** — The LLM-generated post-incident report rendered as formatted markdown, covering summary, evidence, root cause hypothesis, action taken, why that action was chosen, historical pattern assessment, and follow-up recommendations. Includes a **↻ Re-generate Analysis** button that re-runs the analysis with the latest history context on demand.

**Past Similar Incidents** — A table of all past incidents in the database that share the same alert name, namespace/pod, or node. Each row shows the action outcome colour-coded:
- Green ✓ — Action was executed (`action_taken`)
- Blue → — Action was recommended (`action_recommended`)
- Red ✗ — Action errored (`action_error`)

**Agent Timeline** — The raw JSON event stream for the incident in chronological order, showing every tool call, LLM decision, and step result.

---

## Kubernetes Setup

### Namespaces

| Namespace | Purpose |
|-----------|---------|
| `agentic` | Agent deployment and PostgreSQL |
| `demo` | Demo workloads for testing alert triggers |
| `observability` | Prometheus, Alertmanager, Grafana, Loki, Alloy |

### RBAC

The agent's `ServiceAccount` is granted a `ClusterRole` with the minimum permissions needed:

| Resource | Verbs | Purpose |
|----------|-------|---------|
| `pods`, `events`, `nodes` | get, list, watch | Read pod/node state and events |
| `nodes` | patch, update | Cordon / uncordon nodes |
| `pods/eviction` | create | Drain node via Eviction API |
| `deployments`, `replicasets` | get, list, watch | Traverse ownership chain |
| `deployments` | patch, update | Patch image or memory limits |
| `pods` (demo namespace) | delete | Force pod recreation |

### Required Secrets

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: openai-api-key
  namespace: agentic
type: Opaque
data:
  api-key: <base64-encoded-openai-api-key>
```

---

## Configuration

All configuration is supplied via environment variables on the agent Deployment.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key for LLM calls |
| `AGENT_MODE` | No | `recommend` | `auto` to execute remediations, `recommend` to propose only |
| `CLUSTER_NAME` | No | `unknown` | Cluster identifier included in incident analysis |
| `OPENAI_MODEL` | No | `gpt-5.2` | OpenAI model to use for tool calls and analysis |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Monitoring Stack Access

```bash
# Grafana UI
kubectl -n observability port-forward svc/monitoring-grafana 3000:80
# Login: admin / admin

# Agent UI
kubectl -n agentic port-forward svc/agentic-sre-agent 8080:8080
# Open: http://localhost:8080
```
