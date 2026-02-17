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
    get_similar_past_incidents,
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
      body{{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; margin:24px; color:#111827;}}
      a{{color:#2563eb; text-decoration:none}}
      a:hover{{text-decoration:underline}}
      pre{{background:#0b1020; color:#e5e7eb; padding:12px; border-radius:8px; overflow:auto; font-size:13px;}}
      code{{background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:13px;}}
      pre code{{background:none; padding:0;}}
      .card{{border:1px solid #e5e7eb; border-radius:12px; padding:20px; margin:16px 0;}}
      .card-header{{display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;}}
      .card-header h3{{margin:0; font-size:16px; color:#374151;}}
      .muted{{color:#6b7280}}
      .pill{{display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:500;}}
      .pill-blue{{background:#dbeafe; color:#1d4ed8;}}
      .pill-gray{{background:#f3f4f6; color:#374151;}}
      .pill-red{{background:#fee2e2; color:#b91c1c;}}
      .pill-green{{background:#dcfce7; color:#15803d;}}
      .btn{{display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:6px; font-size:13px; font-weight:500; cursor:pointer; border:1px solid #d1d5db; background:#fff; color:#374151; transition:background 0.15s;}}
      .btn:hover{{background:#f9fafb;}}
      .btn:disabled{{opacity:0.5; cursor:not-allowed;}}
      .btn-primary{{background:#2563eb; color:#fff; border-color:#2563eb;}}
      .btn-primary:hover{{background:#1d4ed8;}}
      /* markdown rendered content */
      .md-body h2{{font-size:15px; font-weight:600; margin:18px 0 6px; color:#1f2937; border-bottom:1px solid #f3f4f6; padding-bottom:4px;}}
      .md-body h3{{font-size:14px; font-weight:600; margin:14px 0 4px; color:#374151;}}
      .md-body p{{margin:4px 0 8px; line-height:1.6; font-size:14px;}}
      .md-body ul,.md-body ol{{margin:4px 0 8px; padding-left:20px; font-size:14px; line-height:1.7;}}
      .md-body li{{margin:2px 0;}}
      .md-body strong{{color:#111827;}}
      /* history table */
      .hist-table{{width:100%; border-collapse:collapse; font-size:13px; margin-top:8px;}}
      .hist-table th{{background:#f9fafb; text-align:left; padding:8px 10px; font-weight:600; color:#374151; border-bottom:2px solid #e5e7eb;}}
      .hist-table td{{padding:7px 10px; border-bottom:1px solid #f3f4f6; vertical-align:top;}}
      .hist-table tr:hover td{{background:#fafafa;}}
      .action-taken{{color:#15803d; font-weight:500;}}
      .action-rec{{color:#1d4ed8;}}
      .action-err{{color:#b91c1c;}}
      .no-history{{color:#6b7280; font-size:14px; font-style:italic;}}
      .spinner{{display:inline-block; width:14px; height:14px; border:2px solid #d1d5db; border-top-color:#2563eb; border-radius:50%; animation:spin 0.6s linear infinite;}}
      @keyframes spin{{to{{transform:rotate(360deg)}}}}
    </style>
  </head>
  <body>
    <div><a href="/">← Back to incidents</a></div>
    <h2 style="margin:16px 0 4px">Incident {incident_id}</h2>
    <div id="meta" class="muted" style="font-size:14px; margin-bottom:8px">Loading…</div>

    <div class="card">
      <div class="card-header">
        <h3>Analysis</h3>
        <button class="btn btn-primary" id="regenBtn" onclick="regenAnalysis()">↻ Re-generate Analysis</button>
      </div>
      <div id="regenStatus" style="font-size:13px; margin-bottom:8px; display:none"></div>
      <div id="analysis" class="md-body muted">Loading…</div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>Past Similar Incidents</h3>
      </div>
      <div id="history">Loading…</div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>Agent Timeline</h3>
      </div>
      <pre id="events">Loading…</pre>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
      function renderMd(md) {{
        return (typeof marked !== 'undefined') ? marked.parse(md) : '<pre>' + md + '</pre>';
      }}

      function renderHistory(past) {{
        const histEl = document.getElementById('history');
        if (!past || !past.length) {{
          histEl.innerHTML = '<p class="no-history">No prior similar incidents found in the database.</p>';
          return;
        }}
        let rows = past.map(p => {{
          const actionCell = p.action_taken
            ? `<span class="action-taken">✓ ${{p.action_taken}}</span>`
            : p.action_recommended
            ? `<span class="action-rec">→ ${{p.action_recommended}}</span>`
            : p.action_error
            ? `<span class="action-err">✗ ${{p.action_error}}</span>`
            : '<span class="muted">—</span>';
          return `<tr>
            <td><a href="/incident/${{p.id}}">#${{p.id}}</a></td>
            <td>${{p.alertname||'-'}}</td>
            <td>${{p.namespace||'-'}}/${{p.pod||'-'}}</td>
            <td><span class="pill pill-gray">${{p.runbook_id||'-'}}</span></td>
            <td>${{actionCell}}</td>
            <td class="muted">${{(p.created_at||'').slice(0,19).replace('T',' ')}}</td>
          </tr>`;
        }}).join('');
        histEl.innerHTML = `
          <p class="muted" style="font-size:13px; margin:0 0 8px">${{past.length}} past incident(s) found for the same alert / resource.</p>
          <table class="hist-table">
            <thead><tr>
              <th>#</th><th>Alert</th><th>NS / Pod</th><th>Runbook</th><th>Action outcome</th><th>When</th>
            </tr></thead>
            <tbody>${{rows}}</tbody>
          </table>`;
      }}

      async function load() {{
        const res = await fetch('/api/incidents/{incident_id}');
        const data = await res.json();
        const inc = data.incident;

        const severityClass = (inc.severity||'').toLowerCase() === 'critical' ? 'pill-red'
                            : (inc.severity||'').toLowerCase() === 'warning'  ? 'pill-blue'
                            : 'pill-gray';
        document.getElementById('meta').innerHTML =
          `<span class="pill pill-gray">${{inc.runbook_id||'-'}}</span> &nbsp;` +
          `Alert: <b>${{inc.alertname||'-'}}</b> &nbsp;` +
          `NS/Pod: <b>${{inc.namespace||'-'}}/${{inc.pod||'-'}}</b> &nbsp;` +
          `<span class="pill ${{severityClass}}">${{inc.severity||'unknown'}}</span> &nbsp;` +
          `Updated: ${{inc.updated_at||'-'}}`;

        const md = data.analysis_markdown || '';
        document.getElementById('analysis').innerHTML = md
          ? renderMd(md)
          : '<span class="muted">No analysis yet. Click Re-generate Analysis to create one.</span>';

        renderHistory(data.past_incidents);

        document.getElementById('events').innerText = JSON.stringify(data.events, null, 2);
      }}

      async function regenAnalysis() {{
        const btn = document.getElementById('regenBtn');
        const statusEl = document.getElementById('regenStatus');
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Generating…';
        statusEl.style.display = 'block';
        statusEl.innerHTML = '<span class="muted">Querying past incidents and re-generating analysis with history context…</span>';

        try {{
          const res = await fetch('/api/incidents/{incident_id}/regenerate-analysis', {{method: 'POST'}});
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          const md = data.analysis_markdown || '';
          document.getElementById('analysis').innerHTML = md
            ? renderMd(md)
            : '<span class="muted">Generation returned empty.</span>';
          statusEl.innerHTML = '<span style="color:#15803d">✓ Analysis regenerated with full history context.</span>';
        }} catch(e) {{
          statusEl.innerHTML = `<span style="color:#b91c1c">✗ Failed: ${{e.message}}</span>`;
        }} finally {{
          btn.disabled = false;
          btn.innerHTML = '↻ Re-generate Analysis';
        }}
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

    # Fetch past similar incidents so the UI can render the history table directly.
    webhook_evt = get_latest_event_by_type(incident_id=incident_id, event_type="webhook_received") or {}
    webhook_labels = ((webhook_evt.get("payload") or {}).get("labels") or {}) if webhook_evt else {}
    past = get_similar_past_incidents(
        current_incident_id=incident_id,
        alertname=inc.get("alertname"),
        namespace=inc.get("namespace"),
        pod=inc.get("pod"),
        node=webhook_labels.get("node"),
    )

    return JSONResponse(content=jsonable_encoder({
        "incident": inc,
        "events": list(reversed(events)),
        "analysis_markdown": analysis_md,
        "past_incidents": past,
    }))


@app.post("/api/incidents/{incident_id}/regenerate-analysis")
def api_regenerate_analysis(incident_id: int) -> JSONResponse:
    """
    Re-generate the incident analysis on demand, incorporating full past-incident
    history context. Overwrites the stored 'analysis' event with the new result.
    """
    inc = get_incident(incident_id=incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")

    # Reconstruct the final state and alert context from stored events.
    final_evt = get_latest_event_by_type(incident_id=incident_id, event_type="final") or {}
    final_state = ((final_evt.get("payload") or {}).get("state") or {}) if final_evt else {}
    runbook_id = (final_evt.get("payload") or {}).get("runbook_id") or inc.get("runbook_id") or "RB_UNKNOWN"

    webhook_evt = get_latest_event_by_type(incident_id=incident_id, event_type="webhook_received") or {}
    webhook_payload = (webhook_evt.get("payload") or {}) if webhook_evt else {}
    alert_labels = webhook_payload.get("labels") or {}
    alert_annotations = webhook_payload.get("annotations") or {}
    cluster = webhook_payload.get("cluster") or CLUSTER_NAME

    # Fetch full history for this alert / resource.
    past = get_similar_past_incidents(
        current_incident_id=incident_id,
        alertname=inc.get("alertname"),
        namespace=inc.get("namespace"),
        pod=inc.get("pod"),
        node=alert_labels.get("node"),
    )

    try:
        analysis_md = generate_incident_analysis(
            runbook_id=str(runbook_id),
            cluster=cluster,
            alert_labels=alert_labels,
            alert_annotations=alert_annotations,
            final_state=final_state,
            past_incidents=past or None,
        )
    except Exception as e:
        logger.exception("regenerate_analysis_failed incident_id=%s error=%s", incident_id, e)
        raise HTTPException(status_code=500, detail=f"analysis generation failed: {e}") from e

    if analysis_md:
        add_event(
            incident_id=incident_id,
            event_type="analysis",
            payload={"analysis_markdown": analysis_md, "runbook_id": runbook_id, "regenerated": True},
        )

    return JSONResponse(content={"analysis_markdown": analysis_md, "past_incidents_count": len(past)})


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
                    past = get_similar_past_incidents(
                        current_incident_id=int(incident["id"]),
                        alertname=labels.get("alertname"),
                        namespace=labels.get("namespace"),
                        pod=labels.get("pod"),
                        node=labels.get("node"),
                    )
                    analysis_md = generate_incident_analysis(
                        runbook_id=str(runbook_id or "RB_UNKNOWN"),
                        cluster=CLUSTER_NAME,
                        alert_labels=labels,
                        alert_annotations=a.annotations or {},
                        final_state=out,
                        past_incidents=past or None,
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
