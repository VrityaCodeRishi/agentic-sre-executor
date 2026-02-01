from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from agent.db import (
    add_event,
    advisory_unlock,
    get_incident,
    get_latest_event_by_type,
    list_incident_events,
    list_incidents,
    try_advisory_lock,
    update_incident_runbook,
    upsert_incident,
)

from agent.main import build_graph
from agent.llm import generate_incident_analysis

app = FastAPI(title="agentic-sre-agent", version="0.1.0")
graph = build_graph()

AGENT_MODE = os.getenv("AGENT_MODE", "recommend")
CLUSTER_NAME = os.getenv("CLUSTER_NAME", "unknown")

logger = logging.getLogger("agentic_sre.webhook")
if not logger.handlers:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")


class Alert(BaseModel):
    status: str
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerWebhook(BaseModel):
    receiver: Optional[str] = None
    status: str
    alerts: List[Alert] = Field(default_factory=list)
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = None
    version: Optional[str] = None
    groupKey: Optional[str] = None
    truncatedAlerts: Optional[int] = None


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}


@app.get("/", response_class=HTMLResponse)
def ui_index() -> str:
    # Simple no-auth UI (MVP)
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Agentic SRE - Incidents</title>
    <style>
      body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; margin:24px;}
      table{border-collapse:collapse; width:100%;}
      th,td{border-bottom:1px solid #e5e7eb; padding:10px; text-align:left; font-size:14px;}
      th{color:#111827; font-weight:600;}
      .muted{color:#6b7280}
      a{color:#2563eb; text-decoration:none}
      a:hover{text-decoration:underline}
      .pill{display:inline-block; padding:2px 8px; border-radius:999px; background:#f3f4f6; font-size:12px;}
    </style>
  </head>
  <body>
    <h2>Incidents</h2>
    <div class="muted">Latest incidents handled by the agent. Click an incident to view analysis and steps.</div>
    <div style="height:16px"></div>
    <table id="t">
      <thead>
        <tr>
          <th>ID</th>
          <th>Alert</th>
          <th>Namespace/Pod</th>
          <th>Node</th>
          <th>Runbook</th>
          <th>Severity</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <script>
      async function load() {
        const res = await fetch('/api/incidents?limit=50');
        const data = await res.json();
        const tbody = document.querySelector('#t tbody');
        tbody.innerHTML = '';
        for (const inc of data.incidents) {
          const tr = document.createElement('tr');
          const nsPod = (inc.namespace||'-') + '/' + (inc.pod||'-');
          tr.innerHTML = `
            <td><a href="/incident/${inc.id}">${inc.id}</a></td>
            <td>${inc.alertname||'-'}</td>
            <td>${nsPod}</td>
            <td>${inc.node||'-'}</td>
            <td><span class="pill">${inc.runbook_id||'-'}</span></td>
            <td>${inc.severity||'-'}</td>
            <td class="muted">${inc.updated_at||'-'}</td>
          `;
          tbody.appendChild(tr);
        }
      }
      load();
    </script>
  </body>
</html>
""".strip()


@app.get("/incident/{incident_id}", response_class=HTMLResponse)
def ui_incident(incident_id: int) -> str:
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Incident {incident_id}</title>
    <style>
      body{{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; margin:24px;}}
      a{{color:#2563eb; text-decoration:none}}
      a:hover{{text-decoration:underline}}
      pre{{background:#0b1020; color:#e5e7eb; padding:12px; border-radius:8px; overflow:auto;}}
      .card{{border:1px solid #e5e7eb; border-radius:12px; padding:16px; margin:16px 0;}}
      .muted{{color:#6b7280}}
      .pill{{display:inline-block; padding:2px 8px; border-radius:999px; background:#f3f4f6; font-size:12px;}}
    </style>
  </head>
  <body>
    <div><a href="/">← Back</a></div>
    <h2>Incident {incident_id}</h2>
    <div id="meta" class="muted">Loading…</div>

    <div class="card">
      <h3>Analysis</h3>
      <div id="analysis" class="muted">Generating/Loading…</div>
    </div>

    <div class="card">
      <h3>Timeline</h3>
      <pre id="events">Loading…</pre>
    </div>

    <script>
      async function load() {{
        const res = await fetch('/api/incidents/{incident_id}');
        const data = await res.json();
        const inc = data.incident;
        document.getElementById('meta').innerHTML =
          `<span class="pill">${{inc.runbook_id||'-'}}</span> ` +
          ` Alert: <b>${{inc.alertname||'-'}}</b> ` +
          ` NS/Pod: ${{inc.namespace||'-'}}/${{inc.pod||'-'}} ` +
          ` Severity: ${{inc.severity||'-'}} ` +
          ` Updated: ${{inc.updated_at||'-'}}`;

        document.getElementById('analysis').innerText = data.analysis_markdown || 'No analysis yet.';
        document.getElementById('events').innerText = JSON.stringify(data.events, null, 2);
      }}
      load();
    </script>
  </body>
</html>
""".strip()


@app.get("/api/incidents")
def api_list_incidents(limit: int = 50, offset: int = 0) -> JSONResponse:
    rows = list_incidents(limit=limit, offset=offset)
    # Enrich with "node" from most recent webhook_received labels, if present.
    out = []
    for r in rows:
        inc_id = int(r["id"])
        latest_webhook = get_latest_event_by_type(incident_id=inc_id, event_type="webhook_received") or {}
        labels = ((latest_webhook.get("payload") or {}).get("labels") or {}) if latest_webhook else {}
        out.append({**r, "node": labels.get("node")})
    return JSONResponse(content=jsonable_encoder({"incidents": out}))


@app.get("/api/incidents/{incident_id}")
def api_get_incident(incident_id: int) -> JSONResponse:
    inc = get_incident(incident_id=incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")

    events = list_incident_events(incident_id=incident_id, limit=200)
    analysis_evt = get_latest_event_by_type(incident_id=incident_id, event_type="analysis") or {}
    analysis_md = ((analysis_evt.get("payload") or {}).get("analysis_markdown") or "") if analysis_evt else ""
    return JSONResponse(content=jsonable_encoder({"incident": inc, "events": list(reversed(events)), "analysis_markdown": analysis_md}))


def _fingerprint_for(webhook: AlertmanagerWebhook, alert: Alert, labels: Dict[str, str]) -> str:
    if alert.fingerprint:
        return alert.fingerprint
    if webhook.groupKey and webhook.groupKey not in {"{}/{}", "{}"}:
        return webhook.groupKey

    alertname = labels.get("alertname", "unknown")
    namespace = labels.get("namespace", "")
    pod = labels.get("pod", "")
    container = labels.get("container", "")
    return f"{alertname}:{namespace}:{pod}:{container}"


@app.post("/alertmanager")
def alertmanager(webhook: AlertmanagerWebhook, request: Request) -> Dict[str, Any]:
    logger.info(
        "webhook_received receiver=%s status=%s alerts=%d remote=%s",
        webhook.receiver,
        webhook.status,
        len(webhook.alerts),
        request.client.host if request.client else "unknown",
    )

    if not webhook.alerts:
        return {"received": 0, "results": []}

    results: List[Dict[str, Any]] = []

    try:
        for a in webhook.alerts:
            labels = dict(webhook.commonLabels or {})
            labels.update(a.labels or {})

            fp = _fingerprint_for(webhook, a, labels)

            incident = upsert_incident(
                fingerprint=fp,
                alertname=labels.get("alertname"),
                namespace=labels.get("namespace"),
                pod=labels.get("pod"),
                severity=labels.get("severity"),
                agent_mode=AGENT_MODE,
            )

            add_event(
                incident_id=int(incident["id"]),
                event_type="webhook_received",
                payload={
                    "cluster": CLUSTER_NAME,
                    "alert_status": a.status,
                    "webhook_status": webhook.status,
                    "labels": labels,
                    "annotations": a.annotations or {},
                    "startsAt": a.startsAt,
                    "endsAt": a.endsAt,
                    "fingerprint": fp,
                },
            )

            if not try_advisory_lock(fp):
                add_event(
                    incident_id=int(incident["id"]),
                    event_type="suppressed",
                    payload={"reason": "dedupe_lock_busy", "fingerprint": fp},
                )
                results.append({"fingerprint": fp, "status": "suppressed"})
                continue

            try:
                state = {
                    "alert_labels": labels,
                    "agent_mode": AGENT_MODE,
                    "cluster": CLUSTER_NAME,
                    "fingerprint": fp,
                    "incident_id": int(incident["id"]),
                }

                out = graph.invoke(state)

                runbook_id = out.get("runbook_id")

                update_incident_runbook(int(incident["id"]), runbook_id)

                add_event(
                    incident_id=int(incident["id"]),
                    event_type="final",
                    payload={"runbook_id": runbook_id, "state": out},
                )

                # Generate and persist analysis (best-effort).
                try:
                    analysis_md = generate_incident_analysis(
                        runbook_id=str(runbook_id or "RB_UNKNOWN"),
                        cluster=CLUSTER_NAME,
                        alert_labels=labels,
                        alert_annotations=a.annotations or {},
                        final_state=out,
                    )
                    if analysis_md:
                        add_event(
                            incident_id=int(incident["id"]),
                            event_type="analysis",
                            payload={"analysis_markdown": analysis_md, "runbook_id": runbook_id},
                        )
                except Exception as e:
                    logger.warning("analysis_generation_failed incident_id=%s error=%s", incident["id"], e)

                results.append(
                    {
                        "fingerprint": fp,
                        "status": "handled",
                        "runbook_id": runbook_id,
                    }
                )
            finally:
                advisory_unlock(fp)

        return {"received": len(webhook.alerts), "results": results}
    except Exception as e:
        logger.exception("webhook_processing_failed error=%s", e)
        try:
            body = json.dumps(webhook.model_dump(), default=str)[:4000]
            logger.error("webhook_payload_preview=%s", body)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="webhook processing failed") from e
