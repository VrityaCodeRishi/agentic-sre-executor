---
runbook_id: RB_IMAGEPULL
alertname: KubePodImagePullBackOff
title: ImagePullBackOff Remediation
description: Pod cannot pull container image from registry
workflow:
  - action_id: get_pod_events
  - action_id: patch_image
---

# ImagePullBackOff Remediation

## Problem
Pod container is in `ImagePullBackOff` state, indicating the image cannot be pulled from the registry.

## Diagnostic Steps

1. **Check pod events** for image pull errors
   - Query: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
   - Look for: authentication failures, image not found, network errors

2. **Verify image reference**
   - Check if image tag/digest exists in registry
   - Verify image name format is correct

3. **Check pull secrets**
   - Verify service account has imagePullSecrets configured
   - Check if pull secret exists and is valid

## Remediation Actions

### Action 1: Inspect Events
- **action_id**: `get_pod_events`
- **description**: Fetch recent events for the pod to confirm image pull errors and capture context.
- **command**: `kubectl get events -n {namespace} --field-selector involvedObject.name={pod}`
- **conditions**:
  - Requires: `namespace`, `pod` labels

### Action 2: Patch Image
- **action_id**: `patch_image`
- **description**: If pod is in ImagePullBackOff, patch the owning Deployment to a known-good fallback image.
- **command**: `kubectl patch deployment {deployment} -n {namespace} -p '{"spec":{"template":{"spec":{"containers":[{"name":"{container}","image":"{fallback_image}"}]}}}}'`
- **fallback_image**: `us-docker.pkg.dev/google-samples/containers/gke/hello-app:1.0`
- **conditions**:
  - Requires: `namespace`, `pod` labels (and optional `container`)
  - Pod must be owned by a Deployment

## Success Criteria

- Pod transitions from `ImagePullBackOff` to `Running`
- No new `ImagePullBackOff` events for 5 minutes
- Container starts successfully

## Notes

- This runbook assumes a known good fallback image exists
- If multiple containers in pod, patch only the affected container

