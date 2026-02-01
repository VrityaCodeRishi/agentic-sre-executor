#!/bin/bash
# Quick script to check which runbooks were assigned to incidents

PGPASSWORD="sre_agent_password" psql -h 127.0.0.1 -p 5432 -U sre_agent -d sre_incidents <<EOF
SELECT 
  id,
  alertname,
  namespace,
  pod,
  runbook_id,
  status,
  created_at
FROM incidents
ORDER BY created_at DESC;
EOF

echo ""
echo "--- Final Events (with runbook_id) ---"
PGPASSWORD="sre_agent_password" psql -h 127.0.0.1 -p 5432 -U sre_agent -d sre_incidents <<EOF
SELECT 
  i.id as incident_id,
  i.alertname,
  e.payload->>'runbook_id' as runbook_id,
  e.ts
FROM incidents i
JOIN incident_events e ON e.incident_id = i.id
WHERE e.event_type = 'final'
ORDER BY e.ts DESC;
EOF

