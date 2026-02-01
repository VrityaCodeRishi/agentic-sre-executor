---
runbook_id: RB_NODE_UNSCHEDULABLE
alertname: KubeNodeUnschedulable
title: Node Unschedulable (Cordon) Remediation
description: Node is cordoned/unschedulable; uncordon only if node is healthy
workflow:
  - action_id: get_node_ready
  - action_id: get_node_conditions
  - action_id: uncordon_node
    when_all:
      - node_ready.ready
      - node_ready.unschedulable
      - node_conditions.healthy
---

# Node Unschedulable (Cordon) Remediation

## Problem
Node is marked `unschedulable` (cordoned), preventing new pods from scheduling.

## Diagnostic Steps

1. **Confirm node Ready**
   - Query: `kubectl get node {node}`
   - Look for: `Ready` is `True`

2. **Check node conditions**
   - Query: `kubectl describe node {node}`
   - Look for: `MemoryPressure`, `DiskPressure`, `PIDPressure`, `NetworkUnavailable` are not indicating problems

## Remediation Actions

### Action 1: Check Node Ready
- **action_id**: `get_node_ready`
- **description**: Verify the node Ready condition.
- **command**: `kubectl get node {node} -o jsonpath='{.status.conditions[?(@.type=="Ready")]}'`
- **conditions**:
  - Requires: `node` label

### Action 2: Check Node Conditions
- **action_id**: `get_node_conditions`
- **description**: Verify the node has no unhealthy conditions (pressure/unavailable).
- **command**: `kubectl get node {node} -o jsonpath='{.status.conditions}'`
- **conditions**:
  - Requires: `node` label

### Action 3: Uncordon Node (if healthy)
- **action_id**: `uncordon_node`
- **description**: If node is Ready and conditions are healthy, make it schedulable.
- **command**: `kubectl uncordon {node}`
- **conditions**:
  - Requires: `node` label
  - Only execute if Ready is true and conditions are healthy

## Success Criteria

- Node becomes schedulable (`spec.unschedulable=false`)
- New pods can schedule to the node

## Notes

- Do not uncordon if the node is NotReady or has pressure/unavailable conditions.
- If the node was cordoned intentionally (maintenance), confirm with the on-call before uncordoning.


