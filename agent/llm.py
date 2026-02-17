from __future__ import annotations

import json
import os
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("agentic_sre.llm")


def _openai_client():
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError(f"openai_import_failed: {e}") from e

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY_not_set")
    return OpenAI(api_key=api_key)


def _json_load_loose(text: str) -> Dict[str, Any]:
    """
    Parse a JSON object from a model response.
    - First try full-string json.loads
    - Then fall back to the first {...} substring
    """
    text = (text or "").strip()
    data = json.loads(text)
    if isinstance(data, dict):
        return data
    raise ValueError("not_a_json_object")


def _json_load_loose_fallback(text: str) -> Dict[str, Any]:
    try:
        return _json_load_loose(text)
    except Exception:
        start = (text or "").find("{")
        end = (text or "").rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def decide_imagepull_action(
    *,
    runbook_text: str,
    context: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ask an LLM to choose the next action for ImagePullBackOff.

    MVP: We only support patch_image via the runbook fallback_image.
    The LLM must output strict JSON:
      {"action_id":"patch_image","reason":"...","params":{"namespace":"...","deployment":"...","container":"...","image":"..."}}
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-5.2")

    system = (
        "You are an SRE automation agent. You must follow the runbook instructions exactly.\n"
        "You can only choose actions that exist in the runbook.\n"
        "Return STRICT JSON only. No markdown, no prose.\n"
        "If required fields are missing, return: "
        '{"action_id":"noop","reason":"missing_required_context","params":{}}'
    )

    user = {
        "incident_type": "ImagePullBackOff",
        "runbook": runbook_text,
        "context": context,
        "allowed_actions": ["patch_image"],
        "required_params_for_patch_image": ["namespace", "deployment", "container", "image"],
    }

    client = _openai_client()

    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )

    content = (resp.choices[0].message.content or "").strip()
    return _json_load_loose_fallback(content)


def decide_runbook_action(
    *,
    runbook_id: str,
    runbook_text: str,
    actions: list[dict[str, Any]],
    context: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ask an LLM to choose the next runbook action to execute.

    The LLM must output STRICT JSON:
      {"action_id":"<one of allowed_actions>","reason":"...","params":{...}}

    If required context is missing, it should choose noop:
      {"action_id":"noop","reason":"missing_required_context","params":{}}
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-5.2")

    allowed_actions = [a.get("action_id") for a in (actions or []) if a.get("action_id")]

    # Deterministic shortcut: if there is exactly one possible action in the runbook,
    # choose it directly (avoids LLM picking noop despite sufficient context).
    unique_actions = sorted({a for a in allowed_actions if a and a != "noop"})
    if len(unique_actions) == 1:
        return {
            "action_id": unique_actions[0],
            "reason": "single_runbook_action",
            "params": {},
        }

    system = (
        "You are an SRE automation agent. You must follow the runbook instructions.\n"
        "You can only choose an action_id that exists in allowed_actions.\n"
        "Return STRICT JSON only. No markdown, no prose.\n"
        "If information is missing to safely act, choose action_id=noop.\n"
    )

    user = {
        "runbook_id": runbook_id,
        "runbook": runbook_text,
        "actions": actions,
        "allowed_actions": allowed_actions + ["noop"],
        "context": context,
    }

    client = _openai_client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )

    content = (resp.choices[0].message.content or "").strip()
    return _json_load_loose_fallback(content)


def decide_workflow_tool_call(
    *,
    runbook_id: str,
    step_action_id: str,
    allowed_tool: str,
    runbook_text: str,
    alert_context: Dict[str, Any],
    tool_results: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Decide the next tool call for a specific deterministic runbook workflow step.

    The model MUST call exactly `allowed_tool` (or `noop` only if required context is missing).
    We still validate on the executor side.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-5.2")
    t0 = time.time()

    system = (
        "You are an SRE automation agent.\n"
        "You MUST use tool-calls; do not output plain text.\n"
        "You are executing a deterministic runbook workflow step.\n"
        "Rules:\n"
        f"- runbook_id: {runbook_id}\n"
        f"- step_action_id: {step_action_id}\n"
        f"- You MUST call tool: {allowed_tool}\n"
        "- You may only call noop if required context is missing.\n"
        "- Do not invent values; use alert_context.\n"
    )

    user = {
        "runbook_id": runbook_id,
        "step_action_id": step_action_id,
        "allowed_tool": allowed_tool,
        "runbook": runbook_text,
        "alert_context": alert_context,
        "tool_results": tool_results,
    }

    out = _call_openai_required_tool(model=model, system=system, user=user)
    dt_ms = int((time.time() - t0) * 1000)
    args = out.get("args") or {}
    logger.info(
        "workflow_model=%s latency_ms=%s runbook_id=%s step_action_id=%s tool=%s arg_keys=%s",
        model,
        dt_ms,
        runbook_id,
        step_action_id,
        out.get("tool"),
        sorted(list(args.keys())),
    )
    return out


def generate_incident_analysis(
    *,
    runbook_id: str,
    cluster: str,
    alert_labels: Dict[str, Any],
    alert_annotations: Dict[str, Any],
    final_state: Dict[str, Any],
    past_incidents: Optional[list] = None,
    model: Optional[str] = None,
) -> str:
    """
    Generate a human-readable incident analysis for UI display.
    Returns markdown text (no tool-calls).

    When past_incidents is provided, the analysis includes a history-aware
    section that flags repeat patterns and gives the SRE team better
    long-term remediation recommendations.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-5.2")

    has_history = bool(past_incidents)

    history_instruction = (
        "## Historical pattern & SRE recommendation\n"
        "  - Based on past_incidents, identify if this is a repeat occurrence.\n"
        "  - If the same action was taken before and the alert recurred, flag it as a short-term fix.\n"
        "  - Recommend a more permanent resolution for the SRE team (e.g. root cause investigation, "
        "resource right-sizing, image pipeline fix, node replacement).\n"
        "  - If no past incidents exist, state 'No prior history found for this resource/alert.'\n"
    ) if has_history else (
        "## Historical pattern & SRE recommendation\n"
        "  - No past incident history was available for this alert.\n"
    )

    system = (
        "You are an SRE incident analyst.\n"
        "Write a clear, factual incident analysis based ONLY on the provided data.\n"
        "Do not invent logs/metrics.\n"
        "Output Markdown with these sections:\n"
        "## Summary\n"
        "## What happened (evidence)\n"
        "## Root cause hypothesis\n"
        "## Action taken / recommended\n"
        "## Why that action\n"
        + history_instruction +
        "## Follow-ups\n"
    )

    user: Dict[str, Any] = {
        "cluster": cluster,
        "runbook_id": runbook_id,
        "alert_labels": alert_labels,
        "alert_annotations": alert_annotations,
        "agent_state": final_state,
    }
    if has_history:
        user["past_incidents"] = past_incidents

    client = _openai_client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_runbook",
            "description": "Fetch runbook configuration needed to remediate ImagePullBackOff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "runbook_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["runbook_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fix_imagepullbackoff",
            "description": "Patch the owning Deployment image to the fallback image (or recommend).",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "container": {"type": "string"},
                    "fallback_image": {"type": "string"},
                    "mode": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod", "container"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "increase_memory_limit",
            "description": "Increase the owning Deployment container memory limit by 50% (capped), or recommend.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "container": {"type": "string"},
                    "mode": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod", "container"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_events",
            "description": "Fetch recent Kubernetes events for a pod (used to diagnose ContainerCreating and similar states).",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "limit": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_imagepullbackoff",
            "description": "Detect ImagePullBackOff/ErrImagePull for a pod (via status + events).",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "container": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_oom",
            "description": "Detect OOMKilled for a pod/container (via status + events).",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "container": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_pod",
            "description": "Delete a pod to force recreation (safe restart for Deployments).",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                    "mode": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["namespace", "pod"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_ready",
            "description": "Check whether a node is Ready and whether it is currently unschedulable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_conditions",
            "description": "Check node conditions (pressure/unavailable) excluding the Ready gate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uncordon_node",
            "description": "Make a node schedulable (clear spec.unschedulable).",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "mode": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cordon_node",
            "description": "Cordon a node (set spec.unschedulable=true).",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "mode": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drain_node",
            "description": "Drain a node (best-effort eviction of non-daemonset pods).",
            "parameters": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "mode": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["node"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "Take no action (used when information is missing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
]


def _call_openai_required_tool(*, model: str, system: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call OpenAI with tool-calling required and return the first tool call normalized.
    """
    client = _openai_client()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
        tools=_TOOLS_SPEC,
        tool_choice="required",
    )

    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("llm_did_not_call_tool")

    call = tool_calls[0]
    name = call.function.name
    args_raw = call.function.arguments or "{}"
    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    return {"tool": name, "args": args, "reason": args.get("reason", "")}


def decide_next_tool_call(
    *,
    runbook_id: str,
    alert_context: Dict[str, Any],
    tool_results: Dict[str, Any],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Decide the next tool call for the remediation flow.

    Expected STRICT JSON:
      {"tool":"get_runbook","args":{"runbook_id":"RB_IMAGEPULL"},"reason":"..."}
    or
      {"tool":"fix_imagepullbackoff","args":{"namespace":"...","pod":"...","container":"...","fallback_image":"...","mode":"auto"},"reason":"..."}
    or
      {"tool":"increase_memory_limit","args":{"namespace":"...","pod":"...","container":"...","mode":"auto"},"reason":"..."}
    or
      {"tool":"get_pod_events","args":{"namespace":"...","pod":"...","limit":25},"reason":"..."}
    or
      {"tool":"delete_pod","args":{"namespace":"...","pod":"...","mode":"auto"},"reason":"..."}
    or
      {"tool":"get_node_ready","args":{"node":"..."},"reason":"..."}
    or
      {"tool":"get_node_conditions","args":{"node":"..."},"reason":"..."}
    or
      {"tool":"uncordon_node","args":{"node":"...","mode":"auto"},"reason":"..."}
    or
      {"tool":"noop","args":{},"reason":"..."}
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-5.2")
    t0 = time.time()

    system = (
        "You are an SRE automation agent.\n"
        "You MUST use tool-calls; do not output plain text.\n"
        "Rules:\n"
        "- For runbook_id RB_IMAGEPULL: if runbook not yet loaded in tool_results, call get_runbook; "
        "otherwise call fix_imagepullbackoff using fallback_image from get_runbook.\n"
        "- For runbook_id RB_OOM: call increase_memory_limit.\n"
        "- For runbook_id RB_CONTAINERCREATING: first call get_pod_events if tool_results does not contain pod_events; "
        "then if pod_events.oom_detected is true call increase_memory_limit; "
        "else if pod_events.sandbox_failure_detected is true call delete_pod; "
        "otherwise call noop.\n"
        "- For runbook_id RB_NODE_UNSCHEDULABLE: first call get_node_ready if tool_results does not contain node_ready; "
        "then call get_node_conditions if tool_results does not contain node_conditions; "
        "then if node_ready.ready is true AND node_conditions.healthy is true AND node_ready.unschedulable is true, call uncordon_node; "
        "otherwise call noop.\n"
        "- Never invent images or container names; use alert_context.\n"
    )

    user = {
        "runbook_id": runbook_id,
        "alert_context": alert_context,
        "tool_results": tool_results,
    }
    out = _call_openai_required_tool(model=model, system=system, user=user)
    dt_ms = int((time.time() - t0) * 1000)
    args = out.get("args") or {}
    logger.info("model=%s latency_ms=%s tool=%s arg_keys=%s", model, dt_ms, out.get("tool"), sorted(list(args.keys())))
    return out
