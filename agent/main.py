from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.llm import decide_next_tool_call, decide_workflow_tool_call
from agent.tools import (
    tool_check_imagepullbackoff,
    tool_check_oom,
    tool_cordon_node,
    tool_delete_pod,
    tool_drain_node,
    tool_fix_imagepullbackoff,
    tool_get_node_conditions,
    tool_get_node_ready,
    tool_get_pod_events,
    tool_get_runbook,
    tool_increase_memory_limit,
    tool_uncordon_node,
)

RunbookId = Literal[
    "RB_IMAGEPULL",
    "RB_OOM",
    "RB_CONTAINERCREATING",
    "RB_CRASHLOOP",
    "RB_NODE_UNSCHEDULABLE",
    "RB_NODE_NOTREADY",
    "RB_UNKNOWN",
]
RB_IMAGEPULL = "RB_IMAGEPULL"
RB_OOM = "RB_OOM"
RB_CONTAINERCREATING = "RB_CONTAINERCREATING"
RB_CRASHLOOP = "RB_CRASHLOOP"
RB_NODE_UNSCHEDULABLE = "RB_NODE_UNSCHEDULABLE"
RB_NODE_NOTREADY = "RB_NODE_NOTREADY"
MAX_TOOL_STEPS = 3

logger = logging.getLogger("agentic_sre.graph")


class AgentState(TypedDict, total=False):
    alert_labels: Dict[str, Any]
    agent_mode: str
    runbook_id: RunbookId
    action_taken: str
    action_recommended: str
    action_source: str
    action_error: str
    rb_steps: List[Dict[str, Any]]
    llm_trace: Dict[str, Any]

def _step(state: AgentState, action_id: str, status: str, *, evidence: Any = None, error: Optional[str] = None) -> None:
    steps = state.setdefault("rb_steps", [])
    rec: Dict[str, Any] = {"action_id": action_id, "status": status}
    if evidence is not None:
        rec["evidence"] = evidence
    if error is not None:
        rec["error"] = error
    steps.append(rec)


def _get_label(labels: Dict[str, Any], key: str, default: str = "") -> str:
    v = labels.get(key)
    return default if v is None else str(v)


def route(state: AgentState) -> AgentState:
    labels = state.get("alert_labels", {}) or {}
    rb = (labels.get("runbook_id") or "").strip()
    if rb in {RB_IMAGEPULL, RB_OOM, RB_CONTAINERCREATING, RB_CRASHLOOP, RB_NODE_UNSCHEDULABLE, RB_NODE_NOTREADY}:
        state["runbook_id"] = rb  # type: ignore[assignment]
        logger.info("node=route runbook_id=%s (from_label) alertname=%s", state["runbook_id"], labels.get("alertname"))
        return state

    alertname = (labels.get("alertname") or "").lower()
    if "imagepullbackoff" in alertname:
        state["runbook_id"] = RB_IMAGEPULL
    elif "oomkilled" in alertname:
        state["runbook_id"] = RB_OOM
    elif "containercreating" in alertname:
        state["runbook_id"] = RB_CONTAINERCREATING
    elif "crashloop" in alertname:
        state["runbook_id"] = RB_CRASHLOOP
    elif "nodeunschedulable" in alertname or "unschedulable" in alertname:
        state["runbook_id"] = RB_NODE_UNSCHEDULABLE
    elif "nodenotready" in alertname or "notready" in alertname:
        state["runbook_id"] = RB_NODE_NOTREADY
    else:
        state["runbook_id"] = "RB_UNKNOWN"
    logger.info("node=route runbook_id=%s alertname=%s", state["runbook_id"], labels.get("alertname"))
    return state


def _execute_tool(
    *,
    tool: str,
    args: Dict[str, Any],
    namespace: str,
    pod: str,
    container: str,
    node: str,
    agent_mode: str,
    tool_results: Dict[str, Any],
    state: AgentState,
) -> bool:
    """
    Execute one tool call.

    Returns:
      True  -> we are done (either fixed, noop, or hard error)
      False -> continue loop (e.g. after fetching runbook)
    """
    if tool in (None, "", "noop"):
        logger.info("tool=noop reason=%s", args.get("reason", "noop"))
        _step(state, "noop", "ok", evidence={"reason": args.get("reason", "noop")})
        return True

    if tool == "get_runbook":
        rb_id = args.get("runbook_id") or RB_IMAGEPULL
        logger.info("tool=get_runbook runbook_id=%s", rb_id)
        res = tool_get_runbook(runbook_id=rb_id)
        tool_results["runbook"] = res
        _step(state, "tool:get_runbook", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "get_runbook_failed")
            logger.error("tool=get_runbook failed error=%s", state["action_error"])
            return True
        return False

    if tool == "get_pod_events":
        logger.info(
            "tool=get_pod_events namespace=%s pod=%s limit=%s",
            args.get("namespace") or namespace,
            args.get("pod") or pod,
            args.get("limit"),
        )
        res = tool_get_pod_events(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            limit=int(args.get("limit") or 25),
        )
        tool_results["pod_events"] = res
        _step(state, "tool:get_pod_events", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "get_pod_events_failed")
            logger.error("tool=get_pod_events failed error=%s", state["action_error"])
            return True
        return False

    if tool == "check_imagepullbackoff":
        res = tool_check_imagepullbackoff(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            container=args.get("container") or container,
        )
        tool_results["imagepull"] = res
        _step(state, "tool:check_imagepullbackoff", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "check_imagepullbackoff_failed")
            return True
        return False

    if tool == "check_oom":
        res = tool_check_oom(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            container=args.get("container") or container,
        )
        tool_results["oom"] = res
        _step(state, "tool:check_oom", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "check_oom_failed")
            return True
        return False

    if tool == "get_node_ready":
        n = args.get("node") or node
        logger.info("tool=get_node_ready node=%s", n)
        res = tool_get_node_ready(node=n)
        tool_results["node_ready"] = res
        _step(state, "tool:get_node_ready", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "get_node_ready_failed")
            logger.error("tool=get_node_ready failed error=%s", state["action_error"])
            return True
        return False

    if tool == "get_node_conditions":
        n = args.get("node") or node
        logger.info("tool=get_node_conditions node=%s", n)
        res = tool_get_node_conditions(node=n)
        tool_results["node_conditions"] = res
        _step(state, "tool:get_node_conditions", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "get_node_conditions_failed")
            logger.error("tool=get_node_conditions failed error=%s", state["action_error"])
            return True
        return False

    if tool == "uncordon_node":
        n = args.get("node") or node
        logger.info("tool=uncordon_node node=%s mode=%s", n, args.get("mode") or agent_mode)
        res = tool_uncordon_node(node=n, mode=args.get("mode") or agent_mode)
        _step(state, "tool:uncordon_node", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "uncordon_node_failed")
            logger.error("tool=uncordon_node failed error=%s", state["action_error"])
            return True

        state["action_source"] = "llm"
        if res.get("mode") == "auto":
            state["action_taken"] = res.get("action", "")
        else:
            state["action_recommended"] = res.get("action", "")
        logger.info(
            "tool=uncordon_node ok action_taken=%s action_recommended=%s",
            state.get("action_taken", ""),
            state.get("action_recommended", ""),
        )
        return True

    if tool == "cordon_node":
        n = args.get("node") or node
        logger.info("tool=cordon_node node=%s mode=%s", n, args.get("mode") or agent_mode)
        res = tool_cordon_node(node=n, mode=args.get("mode") or agent_mode)
        tool_results["cordon"] = res
        _step(state, "tool:cordon_node", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "cordon_node_failed")
            return True
        return False

    if tool == "drain_node":
        n = args.get("node") or node
        logger.info("tool=drain_node node=%s mode=%s", n, args.get("mode") or agent_mode)
        res = tool_drain_node(
            node=n,
            mode=args.get("mode") or agent_mode,
            timeout_seconds=int(args.get("timeout_seconds") or 300),
        )
        tool_results["drain"] = res
        _step(state, "tool:drain_node", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "drain_node_failed")
            return True
        return False

    if tool == "delete_pod":
        logger.info(
            "tool=delete_pod namespace=%s pod=%s mode=%s",
            args.get("namespace") or namespace,
            args.get("pod") or pod,
            args.get("mode") or agent_mode,
        )
        res = tool_delete_pod(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            mode=args.get("mode") or agent_mode,
        )
        _step(state, "tool:delete_pod", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "delete_pod_failed")
            logger.error("tool=delete_pod failed error=%s", state["action_error"])
            return True

        state["action_source"] = "llm"
        if res.get("mode") == "auto":
            state["action_taken"] = res.get("action", "")
        else:
            state["action_recommended"] = res.get("action", "")
        logger.info(
            "tool=delete_pod ok action_taken=%s action_recommended=%s",
            state.get("action_taken", ""),
            state.get("action_recommended", ""),
        )
        return True

    if tool == "fix_imagepullbackoff":
        rb = tool_results.get("runbook") or {}
        fallback_image = args.get("fallback_image") or rb.get("fallback_image")
        logger.info(
            "tool=fix_imagepullbackoff namespace=%s pod=%s container=%s mode=%s",
            args.get("namespace") or namespace,
            args.get("pod") or pod,
            args.get("container") or container,
            args.get("mode") or agent_mode,
        )
        res = tool_fix_imagepullbackoff(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            container=args.get("container") or container,
            fallback_image=fallback_image,
            mode=args.get("mode") or agent_mode,
        )
        _step(state, "tool:fix_imagepullbackoff", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "fix_failed")
            logger.error("tool=fix_imagepullbackoff failed error=%s", state["action_error"])
            return True

        state["action_source"] = "llm"
        if res.get("mode") == "auto":
            state["action_taken"] = res.get("action", "")
        else:
            state["action_recommended"] = res.get("action", "")
        logger.info(
            "tool=fix_imagepullbackoff ok action_taken=%s action_recommended=%s",
            state.get("action_taken", ""),
            state.get("action_recommended", ""),
        )
        return True

    if tool == "increase_memory_limit":
        logger.info(
            "tool=increase_memory_limit namespace=%s pod=%s container=%s mode=%s",
            args.get("namespace") or namespace,
            args.get("pod") or pod,
            args.get("container") or container,
            args.get("mode") or agent_mode,
        )
        res = tool_increase_memory_limit(
            namespace=args.get("namespace") or namespace,
            pod=args.get("pod") or pod,
            container=args.get("container") or container,
            mode=args.get("mode") or agent_mode,
        )
        _step(state, "tool:increase_memory_limit", "ok" if res.get("ok") else "failed", evidence=res)
        if not res.get("ok"):
            state["action_error"] = res.get("error", "increase_memory_limit_failed")
            logger.error("tool=increase_memory_limit failed error=%s", state["action_error"])
            return True

        state["action_source"] = "llm"
        if res.get("mode") == "auto" and not res.get("noop"):
            state["action_taken"] = res.get("action", "")
        else:
            # If noop (already at max) or recommend mode, record as recommendation.
            state["action_recommended"] = res.get("action", res.get("reason", "noop"))
        logger.info(
            "tool=increase_memory_limit ok action_taken=%s action_recommended=%s",
            state.get("action_taken", ""),
            state.get("action_recommended", ""),
        )
        return True

    state["action_error"] = f"unknown_tool:{tool}"
    logger.error("unknown_tool=%s", tool)
    _step(state, "execute", "failed", error=f"unknown_tool:{tool}")
    return True


def imagepull_llm_patch(state: AgentState) -> AgentState:
    """
    Runbook-driven workflow with LLM tool-calls for ImagePullBackOff:
    - Read the ordered workflow from the runbook frontmatter (RB_IMAGEPULL.md)
    - For each workflow step, ask the LLM to call the expected tool
    - Validate LLM tool choice against the workflow before executing
    """
    labels = state.get("alert_labels", {}) or {}
    namespace = _get_label(labels, "namespace", "default")
    pod = _get_label(labels, "pod", "")
    container = _get_label(labels, "container", "app") or "app"
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info(
        "node=imagepull_llm_patch start namespace=%s pod=%s container=%s mode=%s",
        namespace,
        pod,
        container,
        agent_mode,
    )

    from agent.runbook_loader import load_runbook

    rb = load_runbook(RB_IMAGEPULL)
    if not rb:
        state["action_error"] = "runbook_not_found"
        _step(state, "load_runbook", "failed", error="runbook_not_found")
        return state

    tool_results: Dict[str, Any] = {}
    workflow = (rb.metadata or {}).get("workflow") or []
    if not isinstance(workflow, list) or not workflow:
        state["action_error"] = "missing_workflow_in_runbook"
        _step(state, "load_workflow", "failed", error="missing_workflow_in_runbook")
        return state

    # Seed runbook-derived config into tool_results so the executor can stay deterministic.
    rb_action = rb.get_action("patch_image")
    fallback_image = str((rb_action.extra or {}).get("fallback_image", "")).strip() if rb_action else ""
    if fallback_image:
        tool_results["runbook"] = {"ok": True, "runbook_id": RB_IMAGEPULL, "fallback_image": fallback_image}

    runbook_text = rb.content
    alert_context = {"namespace": namespace, "pod": pod, "container": container, "node": node, "mode": agent_mode}

    step_idx = 0
    while step_idx < len(workflow):
        step = workflow[step_idx]
        action_id = (step or {}).get("action_id") if isinstance(step, dict) else None
        action_id = "" if action_id is None else str(action_id).strip()
        if not action_id:
            step_idx += 1
            continue

        # Map runbook action_id -> the expected tool name.
        expected_tool = ""
        if action_id == "get_pod_events":
            expected_tool = "get_pod_events"
        elif action_id == "patch_image":
            expected_tool = "fix_imagepullbackoff"
        else:
            state["action_error"] = f"unsupported_runbook_action:{action_id}"
            _step(state, f"execute:{action_id}", "failed", error=f"unsupported_runbook_action:{action_id}")
            return state

        try:
            decision = decide_workflow_tool_call(
                runbook_id=RB_IMAGEPULL,
                step_action_id=action_id,
                allowed_tool=expected_tool,
                runbook_text=runbook_text,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in {expected_tool, "noop"}:
            state["action_error"] = f"llm_invalid_tool_for_step:{action_id}:{tool}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state
        if tool == "noop":
            # The model is only allowed to noop when required context is missing; enforce that.
            state["action_error"] = f"llm_noop_for_required_step:{action_id}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state

        done = _execute_tool(
            tool=tool,
            args=args,
            namespace=namespace,
            pod=pod,
            container=container,
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )

        # get_pod_events returns done=False (continue), patch_image returns done=True (we're done).
        if done:
            state["action_source"] = "runbook_workflow_llm"
            return state

        step_idx += 1

    # If workflow had no executable actions (shouldn't happen), noop.
    _step(state, "noop", "ok", evidence={"reason": "workflow_completed_without_action"})
    return state


def oom_llm_patch(state: AgentState) -> AgentState:
    """
    Tool-using LLM agent for OOMKilled:
    - LLM must call increase_memory_limit (or noop)
    - We execute the tool call and record results into rb_steps
    """
    labels = state.get("alert_labels", {}) or {}
    namespace = _get_label(labels, "namespace", "default")
    pod = _get_label(labels, "pod", "")
    container = _get_label(labels, "container", "app") or "app"
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info(
        "node=oom_llm_patch start namespace=%s pod=%s container=%s mode=%s",
        namespace,
        pod,
        container,
        agent_mode,
    )

    tool_results: Dict[str, Any] = {}
    alert_context = {"namespace": namespace, "pod": pod, "container": container, "node": node, "mode": agent_mode}

    for _ in range(MAX_TOOL_STEPS):
        try:
            decision = decide_next_tool_call(
                runbook_id=RB_OOM,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
            logger.info("llm_decision tool=%s args=%s", decision.get("tool"), decision.get("args"))
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        done = _execute_tool(
            tool=tool,
            args=args,
            namespace=namespace,
            pod=pod,
            container=container,
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )
        if done:
            logger.info(
                "node=oom_llm_patch done action_taken=%s action_recommended=%s action_error=%s",
                state.get("action_taken"),
                state.get("action_recommended"),
                state.get("action_error"),
            )
            return state

    state["action_error"] = "max_tool_steps_exceeded"
    _step(state, "execute", "failed", error="max_tool_steps_exceeded")
    logger.error("node=oom_llm_patch max_tool_steps_exceeded")
    return state


def containercreating_llm_patch(state: AgentState) -> AgentState:
    """
    Runbook workflow + LLM tool-calls for ContainerCreating stuck:
    - Follow RB_CONTAINERCREATING.workflow
    - LLM tool-calls the expected tool per step
    - Steps can be conditionally skipped via `when` gates on prior tool_results
    """
    labels = state.get("alert_labels", {}) or {}
    namespace = _get_label(labels, "namespace", "default")
    pod = _get_label(labels, "pod", "")
    container = _get_label(labels, "container", "app") or "app"
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info(
        "node=containercreating_llm_patch start namespace=%s pod=%s container=%s mode=%s",
        namespace,
        pod,
        container,
        agent_mode,
    )

    from agent.runbook_loader import load_runbook

    def _when_true(path: str, tr: Dict[str, Any]) -> bool:
        cur: Any = tr
        for part in (path or "").split("."):
            if not part:
                continue
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur.get(part)
        return cur is True

    rb = load_runbook(RB_CONTAINERCREATING)
    if not rb:
        state["action_error"] = "runbook_not_found"
        _step(state, "load_runbook", "failed", error="runbook_not_found")
        return state

    workflow = (rb.metadata or {}).get("workflow") or []
    if not isinstance(workflow, list) or not workflow:
        state["action_error"] = "missing_workflow_in_runbook"
        _step(state, "load_workflow", "failed", error="missing_workflow_in_runbook")
        return state

    tool_results: Dict[str, Any] = {}
    rb_action = rb.get_action("patch_image")
    fallback_image = str((rb_action.extra or {}).get("fallback_image", "")).strip() if rb_action else ""
    if fallback_image:
        tool_results["runbook"] = {"ok": True, "runbook_id": RB_CONTAINERCREATING, "fallback_image": fallback_image}

    runbook_text = rb.content
    alert_context = {"namespace": namespace, "pod": pod, "container": container, "node": node, "mode": agent_mode}

    for step in workflow:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get("action_id") or "").strip()
        if not action_id:
            continue
        when = str(step.get("when") or "").strip()
        if when and not _when_true(when, tool_results):
            _step(state, f"skip:{action_id}", "ok", evidence={"reason": f"when_false:{when}"})
            continue

        expected_tool = {
            "get_pod_events": "get_pod_events",
            "check_imagepullbackoff": "check_imagepullbackoff",
            "patch_image": "fix_imagepullbackoff",
            "check_oom": "check_oom",
            "increase_memory_limit": "increase_memory_limit",
        }.get(action_id, "")
        if not expected_tool:
            state["action_error"] = f"unsupported_runbook_action:{action_id}"
            _step(state, f"execute:{action_id}", "failed", error=state["action_error"])
            return state

        try:
            decision = decide_workflow_tool_call(
                runbook_id=RB_CONTAINERCREATING,
                step_action_id=action_id,
                allowed_tool=expected_tool,
                runbook_text=runbook_text,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in {expected_tool, "noop"} or tool == "noop":
            state["action_error"] = f"llm_invalid_tool_for_step:{action_id}:{tool}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state

        done = _execute_tool(
            tool=tool,
            args=args,
            namespace=namespace,
            pod=pod,
            container=container,
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )
        if done:
            state["action_source"] = "runbook_workflow_llm"
            return state

    _step(state, "noop", "ok", evidence={"reason": "workflow_completed_without_terminal_action"})
    return state


def crashloop_llm_patch(state: AgentState) -> AgentState:
    """
    Runbook workflow + LLM tool-calls for CrashLoopBackOff:
    - Follow RB_CRASHLOOP.workflow
    - Check ImagePullBackOff first, then OOM, then remediate accordingly
    """
    labels = state.get("alert_labels", {}) or {}
    namespace = _get_label(labels, "namespace", "default")
    pod = _get_label(labels, "pod", "")
    container = _get_label(labels, "container", "app") or "app"
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info(
        "node=crashloop_llm_patch start namespace=%s pod=%s container=%s mode=%s",
        namespace,
        pod,
        container,
        agent_mode,
    )

    from agent.runbook_loader import load_runbook

    def _when_true(path: str, tr: Dict[str, Any]) -> bool:
        cur: Any = tr
        for part in (path or "").split("."):
            if not part:
                continue
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur.get(part)
        return cur is True

    rb = load_runbook(RB_CRASHLOOP)
    if not rb:
        state["action_error"] = "runbook_not_found"
        _step(state, "load_runbook", "failed", error="runbook_not_found")
        return state

    workflow = (rb.metadata or {}).get("workflow") or []
    if not isinstance(workflow, list) or not workflow:
        state["action_error"] = "missing_workflow_in_runbook"
        _step(state, "load_workflow", "failed", error="missing_workflow_in_runbook")
        return state

    tool_results: Dict[str, Any] = {}
    rb_action = rb.get_action("patch_image")
    fallback_image = str((rb_action.extra or {}).get("fallback_image", "")).strip() if rb_action else ""
    if fallback_image:
        tool_results["runbook"] = {"ok": True, "runbook_id": RB_CRASHLOOP, "fallback_image": fallback_image}

    runbook_text = rb.content
    alert_context = {"namespace": namespace, "pod": pod, "container": container, "node": node, "mode": agent_mode}

    for step in workflow:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get("action_id") or "").strip()
        if not action_id:
            continue
        when = str(step.get("when") or "").strip()
        if when and not _when_true(when, tool_results):
            _step(state, f"skip:{action_id}", "ok", evidence={"reason": f"when_false:{when}"})
            continue

        expected_tool = {
            "get_pod_events": "get_pod_events",
            "check_imagepullbackoff": "check_imagepullbackoff",
            "patch_image": "fix_imagepullbackoff",
            "check_oom": "check_oom",
            "increase_memory_limit": "increase_memory_limit",
        }.get(action_id, "")
        if not expected_tool:
            # Future actions (rollback_deployment, increase_resources) are not yet wired.
            _step(state, f"skip:{action_id}", "ok", evidence={"reason": "unsupported_action_not_wired_yet"})
            continue

        try:
            decision = decide_workflow_tool_call(
                runbook_id=RB_CRASHLOOP,
                step_action_id=action_id,
                allowed_tool=expected_tool,
                runbook_text=runbook_text,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in {expected_tool, "noop"} or tool == "noop":
            state["action_error"] = f"llm_invalid_tool_for_step:{action_id}:{tool}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state

        done = _execute_tool(
            tool=tool,
            args=args,
            namespace=namespace,
            pod=pod,
            container=container,
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )
        if done:
            state["action_source"] = "runbook_workflow_llm"
            return state

    _step(state, "noop", "ok", evidence={"reason": "workflow_completed_without_terminal_action"})
    return state


def node_unschedulable_llm_patch(state: AgentState) -> AgentState:
    """
    Runbook workflow + LLM tool-calls for unschedulable (cordoned) nodes:
    - Follow RB_NODE_UNSCHEDULABLE.workflow
    - LLM tool-calls the expected tool per step (validated)
    - Uncordon is gated by when_all conditions in the runbook
    """
    labels = state.get("alert_labels", {}) or {}
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info("node=node_unschedulable_llm_patch start node=%s mode=%s", node, agent_mode)

    from agent.runbook_loader import load_runbook

    def _when_path_true(path: str, tr: Dict[str, Any]) -> bool:
        cur: Any = tr
        for part in (path or "").split("."):
            if not part:
                continue
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur.get(part)
        return cur is True

    rb = load_runbook(RB_NODE_UNSCHEDULABLE)
    if not rb:
        state["action_error"] = "runbook_not_found"
        _step(state, "load_runbook", "failed", error="runbook_not_found")
        return state

    workflow = (rb.metadata or {}).get("workflow") or []
    if not isinstance(workflow, list) or not workflow:
        state["action_error"] = "missing_workflow_in_runbook"
        _step(state, "load_workflow", "failed", error="missing_workflow_in_runbook")
        return state

    tool_results: Dict[str, Any] = {}
    runbook_text = rb.content
    alert_context = {"node": node, "mode": agent_mode}

    for step in workflow:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get("action_id") or "").strip()
        if not action_id:
            continue

        when_all = step.get("when_all")
        if isinstance(when_all, list) and when_all:
            if not all(_when_path_true(str(p), tool_results) for p in when_all):
                _step(state, f"skip:{action_id}", "ok", evidence={"reason": "when_all_false", "when_all": when_all})
                continue

        expected_tool = {
            "get_node_ready": "get_node_ready",
            "get_node_conditions": "get_node_conditions",
            "uncordon_node": "uncordon_node",
        }.get(action_id, "")
        if not expected_tool:
            state["action_error"] = f"unsupported_runbook_action:{action_id}"
            _step(state, f"execute:{action_id}", "failed", error=state["action_error"])
            return state

        try:
            decision = decide_workflow_tool_call(
                runbook_id=RB_NODE_UNSCHEDULABLE,
                step_action_id=action_id,
                allowed_tool=expected_tool,
                runbook_text=runbook_text,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in {expected_tool, "noop"} or tool == "noop":
            state["action_error"] = f"llm_invalid_tool_for_step:{action_id}:{tool}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state

        done = _execute_tool(
            tool=tool,
            args=args,
            namespace="",
            pod="",
            container="",
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )
        if done:
            state["action_source"] = "runbook_workflow_llm"
            return state

    _step(state, "noop", "ok", evidence={"reason": "workflow_completed_without_terminal_action"})
    return state


def node_notready_llm_patch(state: AgentState) -> AgentState:
    """
    Runbook workflow + LLM tool-calls for NotReady nodes:
    - Follow RB_NODE_NOTREADY.workflow
    - LLM tool-calls the expected tool per step (validated)
    - Steps are gated by when_all conditions in the runbook
    """
    labels = state.get("alert_labels", {}) or {}
    node = _get_label(labels, "node", "")
    agent_mode = state.get("agent_mode", "recommend")

    logger.info("node=node_notready_llm_patch start node=%s mode=%s", node, agent_mode)

    from agent.runbook_loader import load_runbook

    def _when_path_true(path: str, tr: Dict[str, Any]) -> bool:
        cur: Any = tr
        for part in (path or "").split("."):
            if not part:
                continue
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur.get(part)
        return cur is True

    rb = load_runbook(RB_NODE_NOTREADY)
    if not rb:
        state["action_error"] = "runbook_not_found"
        _step(state, "load_runbook", "failed", error="runbook_not_found")
        return state

    workflow = (rb.metadata or {}).get("workflow") or []
    if not isinstance(workflow, list) or not workflow:
        state["action_error"] = "missing_workflow_in_runbook"
        _step(state, "load_workflow", "failed", error="missing_workflow_in_runbook")
        return state

    tool_results: Dict[str, Any] = {}
    runbook_text = rb.content
    alert_context = {"node": node, "mode": agent_mode}

    for step in workflow:
        if not isinstance(step, dict):
            continue
        action_id = str(step.get("action_id") or "").strip()
        if not action_id:
            continue

        when_all = step.get("when_all")
        if isinstance(when_all, list) and when_all:
            if not all(_when_path_true(str(p), tool_results) for p in when_all):
                _step(state, f"skip:{action_id}", "ok", evidence={"reason": "when_all_false", "when_all": when_all})
                continue

        expected_tool = {
            "get_node_ready": "get_node_ready",
            "get_node_conditions": "get_node_conditions",
            "cordon_node": "cordon_node",
            "drain_node": "drain_node",
        }.get(action_id, "")
        if not expected_tool:
            state["action_error"] = f"unsupported_runbook_action:{action_id}"
            _step(state, f"execute:{action_id}", "failed", error=state["action_error"])
            return state

        try:
            decision = decide_workflow_tool_call(
                runbook_id=RB_NODE_NOTREADY,
                step_action_id=action_id,
                allowed_tool=expected_tool,
                runbook_text=runbook_text,
                alert_context=alert_context,
                tool_results=tool_results,
            )
            state["llm_trace"] = {"decision": decision}
            _step(state, "llm_decide", "ok", evidence=decision)
        except Exception as e:
            state["action_error"] = f"llm_failed:{e}"
            _step(state, "llm_decide", "failed", error=str(e))
            logger.exception("llm_failed error=%s", e)
            return state

        tool = decision.get("tool")
        args = decision.get("args") or {}
        if tool not in {expected_tool, "noop"} or tool == "noop":
            state["action_error"] = f"llm_invalid_tool_for_step:{action_id}:{tool}"
            _step(state, "llm_decide", "failed", error=state["action_error"])
            return state

        done = _execute_tool(
            tool=tool,
            args=args,
            namespace="",
            pod="",
            container="",
            node=node,
            agent_mode=agent_mode,
            tool_results=tool_results,
            state=state,
        )
        if done:
            state["action_source"] = "runbook_workflow_llm"
            return state

    _step(state, "noop", "ok", evidence={"reason": "workflow_completed_without_terminal_action"})
    return state


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("route", route)
    graph.add_node("imagepull_llm_patch", imagepull_llm_patch)
    graph.add_node("oom_llm_patch", oom_llm_patch)
    graph.add_node("containercreating_llm_patch", containercreating_llm_patch)
    graph.add_node("crashloop_llm_patch", crashloop_llm_patch)
    graph.add_node("node_unschedulable_llm_patch", node_unschedulable_llm_patch)
    graph.add_node("node_notready_llm_patch", node_notready_llm_patch)

    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        lambda s: s.get("runbook_id", "RB_UNKNOWN"),
        {
            "RB_IMAGEPULL": "imagepull_llm_patch",
            "RB_OOM": "oom_llm_patch",
            "RB_CONTAINERCREATING": "containercreating_llm_patch",
            "RB_CRASHLOOP": "crashloop_llm_patch",
            "RB_NODE_UNSCHEDULABLE": "node_unschedulable_llm_patch",
            "RB_NODE_NOTREADY": "node_notready_llm_patch",
            "RB_UNKNOWN": END,
        },
    )
    graph.add_edge("imagepull_llm_patch", END)
    graph.add_edge("oom_llm_patch", END)
    graph.add_edge("containercreating_llm_patch", END)
    graph.add_edge("crashloop_llm_patch", END)
    graph.add_edge("node_unschedulable_llm_patch", END)
    graph.add_edge("node_notready_llm_patch", END)
    return graph.compile()
