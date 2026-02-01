---
runbook_id: RB_NODE_NOTREADY
alertname: KubeNodeNotReady
title: Node NotReady Remediation
description: Node is NotReady; cordon+drain (no reboot)
workflow:
  - action_id: get_node_ready
  - action_id: get_node_conditions
  - action_id: cordon_node
    when_all:
      - node_ready.not_ready
  - action_id: drain_node
    when_all:
      - node_ready.not_ready
---

# Node NotReady Remediation

## Problem
Node is reporting `Ready=False` (NotReady), which can cause scheduling failures and workload disruption.

## Diagnostic Steps
1. **Confirm Ready**
   - `kubectl get node {node}`
2. **Inspect conditions**
   - `kubectl describe node {node}`

## Remediation Actions

### Action 1: Check Node Ready
- **action_id**: `get_node_ready`
- **description**: Verify the node Ready condition (NotReady detection).
- **command**: `kubectl get node {node}`
- **conditions**:
  - Requires: `node` label

### Action 2: Check Node Conditions
- **action_id**: `get_node_conditions`
- **description**: Record node conditions (pressure/unavailable and NPD conditions).
- **command**: `kubectl describe node {node}`
- **conditions**:
  - Requires: `node` label

### Action 3: Cordon Node
- **action_id**: `cordon_node`
- **description**: Mark node unschedulable before draining/rebooting.
- **command**: `kubectl cordon {node}`
- **conditions**:
  - Requires: `node` label

### Action 4: Drain Node
- **action_id**: `drain_node`
- **description**: Evict non-daemonset pods from the node (best-effort).
- **command**: `kubectl drain {node} --ignore-daemonsets --delete-emptydir-data --force`
- **conditions**:
  - Requires: `node` label

## Success Criteria
- Node returns to `Ready=True`
- Workloads reschedule and recover

## Notes
- Draining a NotReady node can fail if the control plane cannot evict pods; treat drain as best-effort.
- If the node stays NotReady, follow your cloud-provider repair workflow.

