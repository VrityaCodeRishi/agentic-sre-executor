-- Enable pgvector extension (requires superuser or extension exists)
create extension if not exists vector;

create table if not exists incidents (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  fingerprint text not null unique,
  alertname text,
  namespace text,
  pod text,
  severity text,

  runbook_id text,
  status text not null default 'open', -- open|resolved|suppressed
  agent_mode text not null default 'recommend',

      summary text,
      -- Vector embedding for semantic similarity search (1536 dimensions for OpenAI text-embedding-3-small)
      summary_embedding vector(1536)
);

create table if not exists incident_events (
  id bigserial primary key,
  incident_id bigint not null references incidents(id) on delete cascade,
  ts timestamptz not null default now(),
  event_type text not null,
  payload jsonb not null
);

create index if not exists incident_events_incident_id_ts_idx
  on incident_events (incident_id, ts desc);

-- Vector similarity index for semantic search (IVFFlat for fast approximate search)
create index if not exists incidents_summary_embedding_idx
  on incidents using ivfflat (summary_embedding vector_cosine_ops)
  with (lists = 100);