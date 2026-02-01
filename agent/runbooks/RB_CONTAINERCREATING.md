---
runbook_id: RB_CONTAINERCREATING
alertname: KubePodContainerCreatingStuck
title: ContainerCreating Stuck Remediation
description: Pod container stuck in ContainerCreating for an extended period
workflow:
  - action_id: get_pod_events
  - action_id: check_imagepullbackoff
  - action_id: patch_image
    when: imagepull.imagepull_detected
  - action_id: check_oom
  - action_id: increase_memory_limit
    when: oom.oom_detected
---

# ContainerCreating Stuck Remediation

## Problem
Pod container is stuck in `ContainerCreating`, which usually indicates an underlying issue during startup (image pull, volume mount, CNI networking, node resource pressure, etc).

## Diagnostic Steps

1. **Check recent events** for the Pod
   - Query: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
   - Look for: `FailedMount`, `FailedAttachVolume`, `FailedCreatePodSandBox`, `ErrImagePull`, `ImagePullBackOff`, `OOMKilled`

2. **Check container status**
   - Query: `kubectl get pod {pod} -n {namespace} -o wide`
   - Inspect: `Events:` section, node name, and any warning messages

## Remediation Actions

### Action 1: Inspect Events
- **action_id**: `get_pod_events`
- **description**: Fetch recent events for the pod to determine the root cause.
- **command**: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 2: Check ImagePullBackOff
- **action_id**: `check_imagepullbackoff`
- **description**: Detect whether the Pod is failing due to ImagePullBackOff/ErrImagePull (status + events).
- **command**: `kubectl describe pod {pod} -n {namespace}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 3: Patch Image (if ImagePullBackOff)
- **action_id**: `patch_image`
- **description**: Patch the owning Deployment to a known-good fallback image.
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","image":"{fallback_image}"}]}}}}'`
- **fallback_image**: `us-docker.pkg.dev/google-samples/containers/gke/hello-app:1.0`
- **conditions**:
  - Requires: `namespace`, `pod` labels (and optional `container`)
  - Pod must be owned by a Deployment

### Action 4: Check OOM (status/events)
- **action_id**: `check_oom`
- **description**: Detect whether the Pod/container is failing due to OOMKilled (container status or pod events).
- **command**: `kubectl describe pod {pod} -n {namespace}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 5: Increase Memory Limit (if OOM-related)
- **action_id**: `increase_memory_limit`
- **description**: If events indicate `OOMKilled` (or OOM-like termination), increase memory limit to at least 256Mi; if already >=256Mi, double it (bounded increment).
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","resources":{"limits":{"memory":"{new_limit}"}}}]}}}}'`
- **conditions**:
  - Requires: `namespace`, `pod`, `container` labels
  - Pod must be owned by a Deployment
  - Current limit must be known

## Success Criteria

- Pod transitions from `ContainerCreating` to `Running`
- No new warning events for 10 minutes

## Notes

- If events show `ErrImagePull` / `ImagePullBackOff`, use the ImagePull runbook.
- If events show volume or CNI issues, escalate to infrastructure owners.


