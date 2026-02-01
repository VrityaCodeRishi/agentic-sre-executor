---
runbook_id: RB_CRASHLOOP
alertname: KubePodCrashLoopBackOff
title: CrashLoopBackOff Remediation
description: Pod container keeps crashing and restarting
workflow:
  - action_id: get_pod_events
  - action_id: check_imagepullbackoff
  - action_id: patch_image
    when: imagepull.imagepull_detected
  - action_id: check_oom
  - action_id: increase_memory_limit
    when: oom.oom_detected
---

# CrashLoopBackOff Remediation

## Problem
Pod container is in `CrashLoopBackOff` state, indicating the container starts but immediately crashes.

## Diagnostic Steps

1. **Check container logs** for crash errors
   - Query: `kubectl logs {pod} -n {namespace} -c {container} --previous`
   - Look for: segmentation faults, null pointer exceptions, config errors

2. **Check recent events**
   - Query: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
   - Look for: exit codes, termination reasons

3. **Verify configuration**
   - Check if required ConfigMaps/Secrets exist
   - Verify environment variables are set correctly
   - Check resource limits vs requests

## Remediation Actions

### Action 0: Inspect Events
- **action_id**: `get_pod_events`
- **description**: Fetch recent events for the pod to determine whether the crash loop is due to image pull or OOM.
- **command**: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 1: Check ImagePullBackOff
- **action_id**: `check_imagepullbackoff`
- **description**: Detect whether the Pod is failing due to ImagePullBackOff/ErrImagePull (status + events).
- **command**: `kubectl describe pod {pod} -n {namespace}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 2: Patch Image (if ImagePullBackOff)
- **action_id**: `patch_image`
- **description**: Patch the owning Deployment to a known-good fallback image.
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","image":"{fallback_image}"}]}}}}'`
- **fallback_image**: `us-docker.pkg.dev/google-samples/containers/gke/hello-app:1.0`
- **conditions**:
  - Requires: `namespace`, `pod` labels (and optional `container`)
  - Pod must be owned by a Deployment

### Action 3: Check OOM (status/events)
- **action_id**: `check_oom`
- **description**: Detect whether the Pod/container is failing due to OOMKilled (container status or pod events).
- **command**: `kubectl describe pod {pod} -n {namespace}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 4: Increase Memory Limit (if OOM-related)
- **action_id**: `increase_memory_limit`
- **description**: If OOM-related, increase memory limit to at least 256Mi; if already >=256Mi, double it (bounded increment).
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","resources":{"limits":{"memory":"{new_limit}"}}}]}}}}'`
- **conditions**:
  - Requires: `namespace`, `pod`, `container` labels
  - Pod must be owned by a Deployment
  - Current limit must be known

### Action 1: Check Logs and Rollback
- **action_id**: `rollback_deployment`
- **description**: Rollback deployment to previous working revision
- **command**: `kubectl rollout undo deployment/{deployment} -n {namespace}`
- **conditions**:
  - Requires: `namespace`, `pod` labels
  - Pod must be owned by a Deployment
  - Previous revision must exist

### Action 2: Restart with Resource Increase
- **action_id**: `increase_resources`
- **description**: Increase memory/CPU limits if OOM-related crashes
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","resources":{"limits":{"memory":"512Mi"}}}]}}}}'`

## Success Criteria

- Pod transitions from `CrashLoopBackOff` to `Running`
- No new crash events for 10 minutes
- Container stays running and passes health checks

## Notes

- Always check logs first to understand root cause
- Rollback is safest if recent deployment change occurred
- Consider checking readiness/liveness probe settings

