---
runbook_id: RB_OOM
alertname: KubePodOOMKilled
title: OOMKilled Remediation
description: Container was terminated due to out-of-memory
---

# OOMKilled Remediation

## Problem
Container was terminated with reason `OOMKilled`, indicating it exceeded its memory limit.

## Diagnostic Steps

1. **Check memory usage history**
   - Query Prometheus: `container_memory_working_set_bytes{pod="{pod}",namespace="{namespace}"}`
   - Compare against memory limit

2. **Check container logs** around termination
   - Query: `kubectl logs {pod} -n {namespace} -c {container} --previous`
   - Look for: memory leak indicators, large data processing

3. **Verify memory limits**
   - Check current memory limit vs actual usage
   - Determine if limit is too low or if there's a memory leak

## Remediation Actions

### Action 1: Increase Memory Limit
- **action_id**: `increase_memory_limit`
- **description**: Increase container memory limit to at least 256Mi; if already >=256Mi, double it (bounded increment)
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","resources":{"limits":{"memory":"{new_limit}"}}}]}}}}'`
- **calculation**: `new_limit = max(256Mi, current_limit * 2)` (max 4Gi for safety)
- **conditions**:
  - Requires: `namespace`, `pod`, `container` labels
  - Pod must be owned by a Deployment
  - Current limit must be known

### Action 2: Restart Pod (if sudden spike)
- **action_id**: `restart_pod`
- **description**: Delete pod to force recreation (if memory spike was transient)
- **command**: `kubectl delete pod {pod} -n {namespace}`

## Success Criteria

- Pod runs without OOMKilled termination
- Memory usage stays below new limit
- No OOMKilled events for 15 minutes

## Notes

- Always increase limits incrementally (bounded per action)
- If memory keeps growing, investigate for memory leaks
- Consider checking if batch jobs are causing spikes

