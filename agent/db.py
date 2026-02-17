from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def upsert_incident(
    *,
    fingerprint: str,
    alertname: Optional[str],
    namespace: Optional[str],
    pod: Optional[str],
    severity: Optional[str],
    agent_mode: str,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Upsert incident record (MVP).
    Stores a lightweight summary and basic identity fields. No embeddings/learning.
    """

    if not summary:
        parts = []
        if alertname:
            parts.append(f"Alert: {alertname}")
        if namespace:
            parts.append(f"Namespace: {namespace}")
        if pod:
            parts.append(f"Pod: {pod}")
        summary = " | ".join(parts)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into incidents (fingerprint, alertname, namespace, pod, severity, agent_mode, summary)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (fingerprint) do update set
              updated_at = now(),
              alertname = coalesce(excluded.alertname, incidents.alertname),
              namespace = coalesce(excluded.namespace, incidents.namespace),
              pod = coalesce(excluded.pod, incidents.pod),
              severity = coalesce(excluded.severity, incidents.severity),
              agent_mode = coalesce(excluded.agent_mode, incidents.agent_mode),
              summary = coalesce(excluded.summary, incidents.summary)
            returning *;
            """,
            (fingerprint, alertname, namespace, pod, severity, agent_mode, summary),
        )
        row = cur.fetchone()
        assert row is not None
        return row


def update_incident_runbook(incident_id: int, runbook_id: Optional[str]) -> None:
    """Update the runbook_id field for an existing incident."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "update incidents set runbook_id = %s, updated_at = now() where id = %s",
            (runbook_id, incident_id),
        )


def add_event(incident_id: int, event_type: str, payload: Dict[str, Any]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into incident_events (incident_id, event_type, payload) values (%s, %s, %s)",
            (incident_id, event_type, Json(payload)),
        )


def list_incidents(*, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select *
            from incidents
            order by updated_at desc
            limit %s offset %s
            """,
            (int(limit), int(offset)),
        )
        return list(cur.fetchall() or [])


def get_incident(*, incident_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select * from incidents where id = %s", (int(incident_id),))
        return cur.fetchone()


def list_incident_events(*, incident_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select *
            from incident_events
            where incident_id = %s
            order by ts desc
            limit %s
            """,
            (int(incident_id), int(limit)),
        )
        return list(cur.fetchall() or [])


def get_latest_event_by_type(*, incident_id: int, event_type: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select *
            from incident_events
            where incident_id = %s and event_type = %s
            order by ts desc
            limit 1
            """,
            (int(incident_id), str(event_type)),
        )
        return cur.fetchone()


def get_similar_past_incidents(
    *,
    current_incident_id: int,
    alertname: Optional[str],
    namespace: Optional[str],
    pod: Optional[str],
    node: Optional[str],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return all past incidents similar to the current one, across all time.

    Similarity is defined as matching any of:
      - same alertname
      - same namespace + pod
      - same node (when node is non-empty)

    Only incidents that have a 'final' event (i.e. the agent completed a run)
    are included. The action outcome is extracted from that event's JSONB payload.
    Full history is returned (no time window) so the analysis LLM can detect
    long-running repeat patterns.
    """
    filters = []
    params: List[Any] = [current_incident_id]

    if alertname:
        filters.append("i.alertname = %s")
        params.append(alertname)
    if namespace and pod:
        filters.append("(i.namespace = %s AND i.pod = %s)")
        params.extend([namespace, pod])
    if node:
        filters.append(
            "EXISTS ("
            "  SELECT 1 FROM incident_events we"
            "  WHERE we.incident_id = i.id AND we.event_type = 'webhook_received'"
            "  AND we.payload->'labels'->>'node' = %s"
            ")"
        )
        params.append(node)

    if not filters:
        return []

    where_clause = " OR ".join(filters)

    sql = f"""
        SELECT
            i.id,
            i.alertname,
            i.namespace,
            i.pod,
            i.runbook_id,
            i.severity,
            to_char(i.created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
            to_char(i.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at,
            fe.payload->'state'->>'action_taken'      AS action_taken,
            fe.payload->'state'->>'action_recommended' AS action_recommended,
            fe.payload->'state'->>'action_error'       AS action_error,
            fe.payload->>'runbook_id'                  AS final_runbook_id
        FROM incidents i
        JOIN LATERAL (
            SELECT payload
            FROM incident_events
            WHERE incident_id = i.id AND event_type = 'final'
            ORDER BY ts DESC
            LIMIT 1
        ) fe ON true
        WHERE i.id != %s
          AND ({where_clause})
        ORDER BY i.updated_at DESC
        LIMIT {int(limit)}
    """

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def advisory_lock_key(s: str) -> int:
    h = hashlib.sha256(s.encode("utf-8")).digest()
    key_u64 = int.from_bytes(h[:8], "big", signed=False)
    return key_u64 % (2**63)


def try_advisory_lock(fingerprint: str) -> bool:
    key = advisory_lock_key(fingerprint)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(%s) as locked", (key,))
        row = cur.fetchone()
        return bool(row and row["locked"])


def advisory_unlock(fingerprint: str) -> None:
    key = advisory_lock_key(fingerprint)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("select pg_advisory_unlock(%s)", (key,))

