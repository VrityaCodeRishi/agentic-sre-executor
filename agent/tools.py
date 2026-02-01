from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from kubernetes import client, config

from agent.runbook_loader import load_runbook

logger = logging.getLogger("agentic_sre.tools")


def tool_get_runbook(*, runbook_id: str) -> Dict[str, Any]:
    """
    Tool: fetch a runbook and return the minimal structured fields the agent needs.
    MVP: for RB_IMAGEPULL we only care about patch_image.fallback_image.
    """
    rb = load_runbook(runbook_id)
    if not rb:
        logger.warning("tool=get_runbook runbook_id=%s ok=false error=runbook_not_found", runbook_id)
        return {"ok": False, "error": "runbook_not_found", "runbook_id": runbook_id}

    action = rb.get_action("patch_image")
    if not action:
        logger.warning("tool=get_runbook runbook_id=%s ok=false error=missing_patch_image_action", runbook_id)
        return {"ok": False, "error": "missing_patch_image_action", "runbook_id": runbook_id}

    fallback_image = (action.extra or {}).get("fallback_image")
    if not fallback_image:
        logger.warning("tool=get_runbook runbook_id=%s ok=false error=missing_fallback_image", runbook_id)
        return {"ok": False, "error": "missing_fallback_image", "runbook_id": runbook_id}

    logger.info("tool=get_runbook runbook_id=%s ok=true", runbook_id)
    return {
        "ok": True,
        "runbook_id": runbook_id,
        "action_id": "patch_image",
        "fallback_image": str(fallback_image).strip(),
    }


def tool_fix_imagepullbackoff(
    *,
    namespace: str,
    pod: str,
    container: str,
    fallback_image: str,
    mode: str = "recommend",
) -> Dict[str, Any]:
    """
    Tool: remediate ImagePullBackOff by patching the owning Deployment image.
    - Finds owning Deployment by following Pod -> ReplicaSet -> Deployment.
    - Patches the Deployment template container image.
    """
    if not namespace or not pod or not container or not fallback_image:
        logger.warning("tool=fix_imagepullbackoff ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        apps_v1 = client.AppsV1Api()
        core_v1 = client.CoreV1Api()

        p = core_v1.read_namespaced_pod(name=pod, namespace=namespace)

        deployment: Optional[str] = None
        for ref in (p.metadata.owner_references or []):
            if ref.kind != "ReplicaSet":
                continue
            rs = apps_v1.read_namespaced_replica_set(name=ref.name, namespace=namespace)
            for rs_ref in (rs.metadata.owner_references or []):
                if rs_ref.kind == "Deployment":
                    deployment = rs_ref.name
                    break
            if deployment:
                break

        if not deployment:
            logger.warning("tool=fix_imagepullbackoff ns=%s pod=%s ok=false error=pod_not_owned_by_deployment", namespace, pod)
            return {"ok": False, "error": "pod_not_owned_by_deployment"}

        action_msg = f"patch_image:{namespace}/{deployment}/{container}:{fallback_image}"

        if mode == "auto":
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": container, "image": fallback_image}],
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(name=deployment, namespace=namespace, body=patch)
            logger.info("tool=fix_imagepullbackoff ok=true mode=auto ns=%s deployment=%s", namespace, deployment)
            return {"ok": True, "action": action_msg, "deployment": deployment, "mode": "auto"}

        logger.info("tool=fix_imagepullbackoff ok=true mode=recommend ns=%s deployment=%s", namespace, deployment)
        return {"ok": True, "action": action_msg, "deployment": deployment, "mode": "recommend"}
    except Exception as e:
        logger.exception("tool=fix_imagepullbackoff ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


_MEMORY_UNITS_BINARY = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}

_MEMORY_UNITS_DECIMAL = {
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def _parse_k8s_quantity_bytes(qty: str) -> int:
    """
    Parse a Kubernetes resource quantity (memory) into bytes.
    Supports binary (Mi/Gi) and decimal (M/G) suffixes, plus plain integers.
    """
    s = (qty or "").strip()
    if not s:
        raise ValueError("empty_quantity")

    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]+)?", s)
    if not m:
        raise ValueError(f"invalid_quantity:{qty}")

    num_s, unit = m.group(1), m.group(2) or ""
    num = float(num_s)

    if unit in _MEMORY_UNITS_BINARY:
        return int(num * _MEMORY_UNITS_BINARY[unit])
    if unit in _MEMORY_UNITS_DECIMAL:
        return int(num * _MEMORY_UNITS_DECIMAL[unit])
    if unit == "":
        return int(num)

    # Kubernetes also supports 'm' for CPU, but not for memory. Treat as invalid here.
    raise ValueError(f"unsupported_quantity_unit:{unit}")


def _bytes_to_mi_rounded_up(n_bytes: int) -> Tuple[int, str]:
    mi = 1024**2
    n_mi = int(math.ceil(max(0, n_bytes) / mi))
    return n_mi, f"{n_mi}Mi"


def tool_increase_memory_limit(
    *,
    namespace: str,
    pod: str,
    container: str,
    mode: str = "recommend",
    min_limit: str = "256Mi",
    multiplier: float = 2.0,
    max_limit: str = "4Gi",
) -> Dict[str, Any]:
    """
    Tool: remediate OOMKilled by increasing the owning Deployment's container memory limit.

    Matches `agent/runbooks/RB_OOM.md` Action 1 (current MVP policy):
      - if current_limit < 256Mi -> set to 256Mi
      - else new_limit = current_limit * 2
      - bounded to max_limit (default 4Gi) for safety
    """
    if not namespace or not pod or not container:
        logger.warning("tool=increase_memory_limit ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        apps_v1 = client.AppsV1Api()
        core_v1 = client.CoreV1Api()

        p = core_v1.read_namespaced_pod(name=pod, namespace=namespace)

        deployment: Optional[str] = None
        for ref in (p.metadata.owner_references or []):
            if ref.kind != "ReplicaSet":
                continue
            rs = apps_v1.read_namespaced_replica_set(name=ref.name, namespace=namespace)
            for rs_ref in (rs.metadata.owner_references or []):
                if rs_ref.kind == "Deployment":
                    deployment = rs_ref.name
                    break
            if deployment:
                break

        if not deployment:
            logger.warning("tool=increase_memory_limit ns=%s pod=%s ok=false error=pod_not_owned_by_deployment", namespace, pod)
            return {"ok": False, "error": "pod_not_owned_by_deployment"}

        d = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
        tmpl = getattr(getattr(d, "spec", None), "template", None)
        pod_spec = getattr(tmpl, "spec", None) if tmpl else None
        containers = list(getattr(pod_spec, "containers", None) or [])

        current_limit: Optional[str] = None
        for c in containers:
            if getattr(c, "name", None) != container:
                continue
            resources = getattr(c, "resources", None)
            limits = getattr(resources, "limits", None) if resources else None
            if limits and isinstance(limits, dict):
                current_limit = limits.get("memory")
            break

        if not current_limit:
            logger.warning(
                "tool=increase_memory_limit ns=%s deployment=%s container=%s ok=false error=missing_current_memory_limit",
                namespace,
                deployment,
                container,
            )
            return {"ok": False, "error": "missing_current_memory_limit"}

        cur_bytes = _parse_k8s_quantity_bytes(str(current_limit))
        min_bytes = _parse_k8s_quantity_bytes(str(min_limit))
        max_bytes = _parse_k8s_quantity_bytes(str(max_limit))
        if cur_bytes >= max_bytes:
            logger.info(
                "tool=increase_memory_limit ok=true noop=true reason=current_limit_at_or_above_max ns=%s deployment=%s container=%s current_limit=%s max_limit=%s",
                namespace,
                deployment,
                container,
                current_limit,
                max_limit,
            )
            return {
                "ok": True,
                "noop": True,
                "reason": "current_limit_at_or_above_max",
                "deployment": deployment,
                "container": container,
                "old_limit": str(current_limit),
                "new_limit": str(current_limit),
                "mode": mode,
            }
        if cur_bytes < min_bytes:
            target_bytes = min_bytes
        else:
            target_bytes = int(cur_bytes * float(multiplier))
        target_bytes = min(target_bytes, max_bytes)

        # Round up to a whole Mi for nicer patches (and to avoid fractional quantities).
        _, new_limit = _bytes_to_mi_rounded_up(target_bytes)

        action_msg = f"patch_memory_limit:{namespace}/{deployment}/{container}:{current_limit}->{new_limit}"

        if mode == "auto":
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": container,
                                    "resources": {"limits": {"memory": new_limit}},
                                }
                            ]
                        }
                    }
                }
            }
            apps_v1.patch_namespaced_deployment(name=deployment, namespace=namespace, body=patch)
            logger.info("tool=increase_memory_limit ok=true mode=auto ns=%s deployment=%s", namespace, deployment)
            return {
                "ok": True,
                "action": action_msg,
                "deployment": deployment,
                "container": container,
                "old_limit": str(current_limit),
                "new_limit": str(new_limit),
                "mode": "auto",
            }

        logger.info("tool=increase_memory_limit ok=true mode=recommend ns=%s deployment=%s", namespace, deployment)
        return {
            "ok": True,
            "action": action_msg,
            "deployment": deployment,
            "container": container,
            "old_limit": str(current_limit),
            "new_limit": str(new_limit),
            "mode": "recommend",
        }
    except Exception as e:
        logger.exception("tool=increase_memory_limit ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_get_pod_events(*, namespace: str, pod: str, limit: int = 25) -> Dict[str, Any]:
    """
    Tool: fetch recent events for a Pod.
    Used to triage ambiguous states like ContainerCreating.
    """
    if not namespace or not pod:
        logger.warning("tool=get_pod_events ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()

        # Events are namespaced. Filter by involvedObject.name (and kind=Pod where supported).
        field_selector = f"involvedObject.name={pod}"
        ev = core_v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)
        items = list(getattr(ev, "items", None) or [])

        # Keep newest-ish events first; many clusters don’t guarantee ordering.
        def _ts(e: Any) -> str:
            # Use last_timestamp if present, else event_time, else metadata creation timestamp.
            for attr in ("last_timestamp", "event_time"):
                v = getattr(e, attr, None)
                if v:
                    return str(v)
            meta = getattr(e, "metadata", None)
            return str(getattr(meta, "creation_timestamp", "") or "")

        items.sort(key=_ts, reverse=True)
        items = items[: max(1, int(limit or 25))]

        events: List[Dict[str, Any]] = []
        oom_matches: List[str] = []
        sandbox_matches: List[str] = []
        for e in items:
            reason = str(getattr(e, "reason", "") or "")
            message = str(getattr(e, "message", "") or "")
            etype = str(getattr(e, "type", "") or "")
            count = getattr(e, "count", None)
            rec = {
                "type": etype,
                "reason": reason,
                "message": message,
                "count": count,
                "ts": _ts(e),
            }
            events.append(rec)

            msg_l = (reason + " " + message).lower()
            # Common variants:
            # - "OOMKilled" (kube reason)
            # - "OOM-killed" (runtime / kubelet messages)
            # - "out of memory"
            # - "memory limit too low" (heuristic hint)
            if (
                re.search(r"\boom[- ]?killed\b", msg_l)
                or "oomkilled" in msg_l
                or "out of memory" in msg_l
                or "memory limit too low" in msg_l
            ):
                oom_matches.append(f"{reason}: {message}".strip(": ").strip())

            # Sandbox creation/start failures often show up while stuck in ContainerCreating.
            # Example: FailedCreatePodSandBox ... "cannot start a stopped process"
            if "failedcreatepodsandbox" in msg_l or "pod sandbox" in msg_l:
                if "cannot start a stopped process" in msg_l or "cannot start a container that has stopped" in msg_l:
                    sandbox_matches.append(f"{reason}: {message}".strip(": ").strip())

        res = {
            "ok": True,
            "namespace": namespace,
            "pod": pod,
            "events": events,
            "oom_detected": len(oom_matches) > 0,
            "oom_matches": oom_matches[:5],
            "sandbox_failure_detected": len(sandbox_matches) > 0,
            "sandbox_failure_matches": sandbox_matches[:5],
        }
        logger.info(
            "tool=get_pod_events ok=true ns=%s pod=%s events=%d oom_detected=%s sandbox_failure_detected=%s",
            namespace,
            pod,
            len(events),
            res["oom_detected"],
            res["sandbox_failure_detected"],
        )
        return res
    except Exception as e:
        logger.exception("tool=get_pod_events ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_delete_pod(*, namespace: str, pod: str, mode: str = "recommend") -> Dict[str, Any]:
    """
    Tool: delete a pod to force recreation (a safe "restart" for Deployments).
    Useful for transient kubelet/container runtime issues like sandbox start failures.
    """
    if not namespace or not pod:
        logger.warning("tool=delete_pod ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        action_msg = f"delete_pod:{namespace}/{pod}"

        if mode == "auto":
            core_v1.delete_namespaced_pod(name=pod, namespace=namespace)
            logger.info("tool=delete_pod ok=true mode=auto ns=%s pod=%s", namespace, pod)
            return {"ok": True, "action": action_msg, "mode": "auto"}

        logger.info("tool=delete_pod ok=true mode=recommend ns=%s pod=%s", namespace, pod)
        return {"ok": True, "action": action_msg, "mode": "recommend"}
    except Exception as e:
        logger.exception("tool=delete_pod ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_get_node_ready(*, node: str) -> Dict[str, Any]:
    """
    Tool: check whether a node is Ready.
    Kept separate so it can be reused by a future NotReady alert flow.
    """
    if not node:
        logger.warning("tool=get_node_ready ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        n = core_v1.read_node(name=node)

        conds = list(getattr(getattr(n, "status", None), "conditions", None) or [])
        ready = False
        ready_rec: Dict[str, Any] = {}
        for c in conds:
            if getattr(c, "type", None) == "Ready":
                status = str(getattr(c, "status", "") or "")
                ready = status == "True"
                ready_rec = {
                    "type": "Ready",
                    "status": status,
                    "reason": str(getattr(c, "reason", "") or ""),
                    "message": str(getattr(c, "message", "") or ""),
                    "last_transition_time": str(getattr(c, "last_transition_time", "") or ""),
                }
                break

        # Unschedulable flag is on spec.
        unschedulable = bool(getattr(getattr(n, "spec", None), "unschedulable", False))

        res = {
            "ok": True,
            "node": node,
            "ready": ready,
            "not_ready": not ready,
            "ready_condition": ready_rec,
            "unschedulable": unschedulable,
        }
        logger.info("tool=get_node_ready ok=true node=%s ready=%s unschedulable=%s", node, ready, unschedulable)
        return res
    except Exception as e:
        logger.exception("tool=get_node_ready ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_get_node_conditions(*, node: str) -> Dict[str, Any]:
    """
    Tool: check node conditions (excluding the Ready gate).
    Returns unhealthy conditions for safe decision-making.
    """
    if not node:
        logger.warning("tool=get_node_conditions ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        n = core_v1.read_node(name=node)

        conds = list(getattr(getattr(n, "status", None), "conditions", None) or [])
        by_type: Dict[str, Dict[str, Any]] = {}
        for c in conds:
            ctype = str(getattr(c, "type", "") or "")
            if not ctype:
                continue
            by_type[ctype] = {
                "type": ctype,
                "status": str(getattr(c, "status", "") or ""),
                "reason": str(getattr(c, "reason", "") or ""),
                "message": str(getattr(c, "message", "") or ""),
                "last_transition_time": str(getattr(c, "last_transition_time", "") or ""),
            }

        # Define what "healthy" means for non-Ready conditions.
        #
        # We treat the node as "healthy" only when ALL non-Ready conditions are False.
        # This matches GKE Node Problem Detector style conditions (False => not detected).
        problems: List[Dict[str, Any]] = []
        for ctype, rec in by_type.items():
            if ctype == "Ready":
                continue
            if rec.get("status") != "False":
                problems.append(rec)

        healthy = len(problems) == 0
        res = {
            "ok": True,
            "node": node,
            "healthy": healthy,
            "problems": problems,
            "conditions": by_type,
        }
        logger.info("tool=get_node_conditions ok=true node=%s healthy=%s problems=%d", node, healthy, len(problems))
        return res
    except Exception as e:
        logger.exception("tool=get_node_conditions ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_uncordon_node(*, node: str, mode: str = "recommend") -> Dict[str, Any]:
    """
    Tool: make a node schedulable by clearing spec.unschedulable.
    """
    if not node:
        logger.warning("tool=uncordon_node ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        action_msg = f"uncordon_node:{node}"

        if mode == "auto":
            patch = {"spec": {"unschedulable": False}}
            core_v1.patch_node(name=node, body=patch)
            logger.info("tool=uncordon_node ok=true mode=auto node=%s", node)
            return {"ok": True, "action": action_msg, "mode": "auto"}

        logger.info("tool=uncordon_node ok=true mode=recommend node=%s", node)
        return {"ok": True, "action": action_msg, "mode": "recommend"}
    except Exception as e:
        logger.exception("tool=uncordon_node ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_cordon_node(*, node: str, mode: str = "recommend") -> Dict[str, Any]:
    """
    Tool: mark a node unschedulable (cordon) by setting spec.unschedulable=true.
    """
    if not node:
        logger.warning("tool=cordon_node ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        action_msg = f"cordon_node:{node}"
        if mode == "auto":
            patch = {"spec": {"unschedulable": True}}
            core_v1.patch_node(name=node, body=patch)
            logger.info("tool=cordon_node ok=true mode=auto node=%s", node)
            return {"ok": True, "action": action_msg, "mode": "auto"}
        logger.info("tool=cordon_node ok=true mode=recommend node=%s", node)
        return {"ok": True, "action": action_msg, "mode": "recommend"}
    except Exception as e:
        logger.exception("tool=cordon_node ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_drain_node(*, node: str, mode: str = "recommend", timeout_seconds: int = 300) -> Dict[str, Any]:
    """
    Tool: best-effort drain via Eviction API:
    - lists pods on the node
    - evicts non-daemonset, non-mirror pods
    """
    if not node:
        logger.warning("tool=drain_node ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        policy_v1 = client.PolicyV1Api()

        pods = core_v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node}").items or []

        evict_targets: List[tuple[str, str]] = []
        skipped: List[Dict[str, Any]] = []
        for p in pods:
            ns = str(getattr(getattr(p, "metadata", None), "namespace", "") or "")
            name = str(getattr(getattr(p, "metadata", None), "name", "") or "")
            anns = getattr(getattr(p, "metadata", None), "annotations", None) or {}

            # Mirror pods (static pods) have this annotation.
            if isinstance(anns, dict) and "kubernetes.io/config.mirror" in anns:
                skipped.append({"namespace": ns, "pod": name, "reason": "mirror_pod"})
                continue

            # Skip DaemonSet-managed pods.
            owners = getattr(getattr(p, "metadata", None), "owner_references", None) or []
            if any(getattr(o, "kind", "") == "DaemonSet" for o in owners):
                skipped.append({"namespace": ns, "pod": name, "reason": "daemonset"})
                continue

            # Skip kube-system control plane-ish components to be safe (best-effort).
            if ns == "kube-system":
                skipped.append({"namespace": ns, "pod": name, "reason": "kube-system"})
                continue

            evict_targets.append((ns, name))

        action_msg = f"drain_node:{node}:evict={len(evict_targets)}"
        if mode != "auto":
            logger.info("tool=drain_node ok=true mode=recommend node=%s evict=%d", node, len(evict_targets))
            return {"ok": True, "action": action_msg, "mode": "recommend", "evict_targets": evict_targets, "skipped": skipped}

        errors: List[str] = []
        start = time.time()
        for ns, name in evict_targets:
            try:
                eviction = client.V1Eviction(
                    metadata=client.V1ObjectMeta(name=name, namespace=ns),
                    delete_options=client.V1DeleteOptions(grace_period_seconds=30),
                )
                policy_v1.create_namespaced_pod_eviction(name=name, namespace=ns, body=eviction)
            except Exception as e:
                errors.append(f"{ns}/{name}:{e}")

        # Wait for pods to leave the node (best-effort).
        while time.time() - start < max(1, int(timeout_seconds)):
            remaining = core_v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node}").items or []
            remaining = [
                p
                for p in remaining
                if str(getattr(getattr(p, "metadata", None), "namespace", "") or "") != "kube-system"
                and not (isinstance(getattr(getattr(p, "metadata", None), "annotations", None) or {}, dict) and "kubernetes.io/config.mirror" in (getattr(getattr(p, "metadata", None), "annotations", None) or {}))
            ]
            if not remaining:
                break
            time.sleep(5)

        ok = len(errors) == 0
        logger.info("tool=drain_node ok=%s mode=auto node=%s errors=%d", ok, node, len(errors))
        return {
            "ok": ok,
            "action": action_msg,
            "mode": "auto",
            "errors": errors,
            "skipped": skipped,
        }
    except Exception as e:
        logger.exception("tool=drain_node ok=false error=%s", e)
        return {"ok": False, "error": str(e)}




def tool_check_imagepullbackoff(*, namespace: str, pod: str, container: str = "") -> Dict[str, Any]:
    """
    Tool: detect whether a pod is in ImagePullBackOff/ErrImagePull via pod status and events.
    Returns a boolean and best-effort affected container name.
    """
    if not namespace or not pod:
        logger.warning("tool=check_imagepullbackoff ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        p = core_v1.read_namespaced_pod(name=pod, namespace=namespace)

        detected = False
        detected_container = ""
        reasons: List[str] = []

        statuses = list(getattr(getattr(p, "status", None), "container_statuses", None) or [])
        for cs in statuses:
            name = str(getattr(cs, "name", "") or "")
            if container and name != container:
                continue
            st = getattr(cs, "state", None)
            waiting = getattr(st, "waiting", None) if st else None
            w_reason = str(getattr(waiting, "reason", "") or "") if waiting else ""
            if w_reason in {"ImagePullBackOff", "ErrImagePull"}:
                detected = True
                detected_container = name or detected_container
                reasons.append(f"pod_status_waiting_reason:{w_reason}")

        # Also inspect events for ImagePullBackOff-like messages.
        field_selector = f"involvedObject.name={pod}"
        ev = core_v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)
        items = list(getattr(ev, "items", None) or [])
        for e in items:
            msg_l = (str(getattr(e, "reason", "") or "") + " " + str(getattr(e, "message", "") or "")).lower()
            if "imagepullbackoff" in msg_l or "errimagepull" in msg_l or "failed to pull image" in msg_l:
                detected = True
                reasons.append("event_mentions_imagepull")

        res = {
            "ok": True,
            "namespace": namespace,
            "pod": pod,
            "imagepull_detected": detected,
            "container": detected_container or (container or ""),
            "reasons": sorted(list(set(reasons))),
        }
        logger.info(
            "tool=check_imagepullbackoff ok=true ns=%s pod=%s detected=%s container=%s",
            namespace,
            pod,
            detected,
            res.get("container", ""),
        )
        return res
    except Exception as e:
        logger.exception("tool=check_imagepullbackoff ok=false error=%s", e)
        return {"ok": False, "error": str(e)}


def tool_check_oom(*, namespace: str, pod: str, container: str = "") -> Dict[str, Any]:
    """
    Tool: detect OOM-related failures via pod container status and events.
    Returns a boolean and best-effort affected container name.
    """
    if not namespace or not pod:
        logger.warning("tool=check_oom ok=false error=missing_required_params")
        return {"ok": False, "error": "missing_required_params"}

    try:
        config.load_incluster_config()
        core_v1 = client.CoreV1Api()
        p = core_v1.read_namespaced_pod(name=pod, namespace=namespace)

        detected = False
        detected_container = ""
        reasons: List[str] = []

        statuses = list(getattr(getattr(p, "status", None), "container_statuses", None) or [])
        for cs in statuses:
            name = str(getattr(cs, "name", "") or "")
            if container and name != container:
                continue
            st = getattr(cs, "state", None)
            last = getattr(cs, "last_state", None)

            term = getattr(st, "terminated", None) if st else None
            last_term = getattr(last, "terminated", None) if last else None

            for t in (term, last_term):
                r = str(getattr(t, "reason", "") or "") if t else ""
                if r == "OOMKilled":
                    detected = True
                    detected_container = name or detected_container
                    reasons.append("pod_status_terminated_reason:OOMKilled")

        # Also inspect events for OOM-like messages (reuse patterns from get_pod_events).
        field_selector = f"involvedObject.name={pod}"
        ev = core_v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)
        items = list(getattr(ev, "items", None) or [])
        for e in items:
            msg_l = (str(getattr(e, "reason", "") or "") + " " + str(getattr(e, "message", "") or "")).lower()
            if (
                re.search(r"\boom[- ]?killed\b", msg_l)
                or "out of memory" in msg_l
                or "memory limit too low" in msg_l
            ):
                detected = True
                reasons.append("event_mentions_oom")

        res = {
            "ok": True,
            "namespace": namespace,
            "pod": pod,
            "oom_detected": detected,
            "container": detected_container or (container or ""),
            "reasons": sorted(list(set(reasons))),
        }
        logger.info("tool=check_oom ok=true ns=%s pod=%s detected=%s container=%s", namespace, pod, detected, res.get("container", ""))
        return res
    except Exception as e:
        logger.exception("tool=check_oom ok=false error=%s", e)
        return {"ok": False, "error": str(e)}

