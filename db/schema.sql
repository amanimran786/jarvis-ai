-- Jarvis OS — Production PostgreSQL Schema
-- Requires: pgvector extension for vector search
-- Run: psql -U jarvis -d jarvis -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-------------------------------------------------------------------------------
-- ENUMS
-------------------------------------------------------------------------------

CREATE TYPE memory_layer AS ENUM ('working', 'project', 'personal', 'knowledge');
CREATE TYPE memory_status AS ENUM ('active', 'archived', 'superseded');
CREATE TYPE task_status   AS ENUM ('pending', 'assigned', 'running', 'review', 'done', 'failed', 'cancelled');
CREATE TYPE review_verdict AS ENUM ('approved', 'rejected', 'needs_revision');
CREATE TYPE agent_role    AS ENUM ('human', 'manager', 'agent', 'readonly');
CREATE TYPE event_type    AS ENUM (
    'task.created', 'task.assigned', 'task.started', 'task.completed',
    'task.failed', 'task.cancelled',
    'review.requested', 'review.completed',
    'agent.started', 'agent.stopped',
    'memory.written', 'memory.recalled',
    'approval.requested', 'approval.granted', 'approval.denied'
);

-------------------------------------------------------------------------------
-- MEMORY SYSTEM
-- Four layers: working (ephemeral), project (scoped), personal (user), knowledge (global)
-------------------------------------------------------------------------------

CREATE TABLE memory_entries (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    layer           memory_layer NOT NULL,
    agent_id        TEXT        NOT NULL,         -- who wrote it
    session_id      UUID,                         -- working memory only
    project_id      TEXT,                         -- project memory only
    key             TEXT        NOT NULL,         -- addressable name
    content         TEXT        NOT NULL,
    content_vector  vector(768),                  -- nomic-embed-text dim
    metadata        JSONB       NOT NULL DEFAULT '{}',
    status          memory_status NOT NULL DEFAULT 'active',
    importance      SMALLINT    NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
    access_count    INTEGER     NOT NULL DEFAULT 0,
    last_accessed   TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,                  -- NULL = no expiry (knowledge layer)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vector similarity search (IVFFlat — tune lists based on row count)
CREATE INDEX idx_memory_vector    ON memory_entries USING ivfflat (content_vector vector_cosine_ops) WITH (lists = 100);
-- Layer + status filter (most queries filter these first)
CREATE INDEX idx_memory_layer_status ON memory_entries (layer, status);
-- Agent lookups
CREATE INDEX idx_memory_agent     ON memory_entries (agent_id, layer, status);
-- Session scope (working memory cleanup)
CREATE INDEX idx_memory_session   ON memory_entries (session_id) WHERE session_id IS NOT NULL;
-- Project scope
CREATE INDEX idx_memory_project   ON memory_entries (project_id, layer) WHERE project_id IS NOT NULL;
-- JSONB metadata queries
CREATE INDEX idx_memory_metadata  ON memory_entries USING gin (metadata);
-- Full-text search fallback
CREATE INDEX idx_memory_content_fts ON memory_entries USING gin (to_tsvector('english', content));

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE TRIGGER trg_memory_updated_at
    BEFORE UPDATE ON memory_entries
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-------------------------------------------------------------------------------
-- TASKS
-- Master task registry. SQLite mirrors a subset for local-only fast ops.
-- Authoritative source for audit, cross-agent visibility, and review routing.
-------------------------------------------------------------------------------

CREATE TABLE tasks (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    parent_id       UUID        REFERENCES tasks(id) ON DELETE SET NULL,  -- subtask chain
    title           TEXT        NOT NULL,
    description     TEXT,
    requester_id    TEXT        NOT NULL,   -- human uid or agent_id
    assigned_to     TEXT,                  -- agent_id
    status          task_status NOT NULL DEFAULT 'pending',
    priority        SMALLINT    NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    model_used      TEXT,                  -- which LLM handled this
    context         JSONB       NOT NULL DEFAULT '{}',   -- routing hints, tool list, etc.
    result          JSONB,                 -- structured output when done
    error_detail    TEXT,                  -- error message when failed
    retry_count     SMALLINT    NOT NULL DEFAULT 0,
    max_retries     SMALLINT    NOT NULL DEFAULT 3,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    deadline        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_status      ON tasks (status, priority DESC);
CREATE INDEX idx_tasks_assigned    ON tasks (assigned_to, status);
CREATE INDEX idx_tasks_requester   ON tasks (requester_id);
CREATE INDEX idx_tasks_parent      ON tasks (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX idx_tasks_context     ON tasks USING gin (context);
CREATE INDEX idx_tasks_created     ON tasks (created_at DESC);

CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-------------------------------------------------------------------------------
-- REVIEW CHAINS
-- DAG of reviewer stages. Each task gets a chain; agents advance through stages.
-------------------------------------------------------------------------------

CREATE TABLE review_chains (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage           SMALLINT    NOT NULL DEFAULT 0,   -- 0-indexed stage in DAG
    reviewer_id     TEXT        NOT NULL,              -- agent_id or 'human'
    verdict         review_verdict,
    notes           TEXT,
    artifacts       JSONB       NOT NULL DEFAULT '{}', -- attached output/diffs
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    UNIQUE (task_id, stage)
);

CREATE INDEX idx_review_task       ON review_chains (task_id, stage);
CREATE INDEX idx_review_pending    ON review_chains (reviewer_id) WHERE verdict IS NULL;

-------------------------------------------------------------------------------
-- AUDIT LOG
-- Append-only event log. Never UPDATE or DELETE rows here.
-------------------------------------------------------------------------------

CREATE TABLE audit_log (
    id              BIGSERIAL   PRIMARY KEY,
    event_type      event_type  NOT NULL,
    actor_id        TEXT        NOT NULL,
    target_id       TEXT,                  -- task_id, memory_id, agent_id
    target_type     TEXT,                  -- 'task', 'memory', 'agent', etc.
    payload         JSONB       NOT NULL DEFAULT '{}',
    session_id      UUID,
    ip_address      INET,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partitioning hint: partition by month in production
-- CREATE TABLE audit_log_2026_06 PARTITION OF audit_log FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

CREATE INDEX idx_audit_ts          ON audit_log (ts DESC);
CREATE INDEX idx_audit_actor       ON audit_log (actor_id, ts DESC);
CREATE INDEX idx_audit_target      ON audit_log (target_id, target_type, ts DESC);
CREATE INDEX idx_audit_event       ON audit_log (event_type, ts DESC);
CREATE INDEX idx_audit_payload     ON audit_log USING gin (payload);

-------------------------------------------------------------------------------
-- AGENT PERMISSIONS (RBAC)
-- Maps agent_id → role → allowed capabilities
-------------------------------------------------------------------------------

CREATE TABLE agent_permissions (
    agent_id        TEXT        PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    role            agent_role  NOT NULL DEFAULT 'agent',
    allowed_tools   TEXT[]      NOT NULL DEFAULT '{}',
    allowed_agents  TEXT[]      NOT NULL DEFAULT '{}',  -- can dispatch to these
    rate_limit_rpm  SMALLINT    NOT NULL DEFAULT 60,
    is_active       BOOLEAN     NOT NULL DEFAULT true,
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_perms_role        ON agent_permissions (role) WHERE is_active = true;

CREATE TRIGGER trg_perms_updated_at
    BEFORE UPDATE ON agent_permissions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-------------------------------------------------------------------------------
-- SESSIONS
-- Tracks a single conversation/context window per channel
-------------------------------------------------------------------------------

CREATE TABLE sessions (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         TEXT        NOT NULL,
    channel         TEXT        NOT NULL,   -- 'desktop', 'voice', 'imessage', 'telegram', 'cli'
    context_tokens  INTEGER     NOT NULL DEFAULT 0,
    state           JSONB       NOT NULL DEFAULT '{}',
    is_active       BOOLEAN     NOT NULL DEFAULT true,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX idx_sessions_user     ON sessions (user_id, is_active);
CREATE INDEX idx_sessions_channel  ON sessions (channel, is_active);

-------------------------------------------------------------------------------
-- SEED: default RBAC entries
-------------------------------------------------------------------------------

INSERT INTO agent_permissions (agent_id, display_name, role, allowed_tools, allowed_agents, rate_limit_rpm) VALUES
    ('human',             'Aman (Human Operator)',    'human',    ARRAY['*'],          ARRAY['*'], 9999),
    ('jarvis_manager',    'Jarvis Manager',           'manager',  ARRAY['*'],          ARRAY['*'], 300),
    ('backend_engineer',  'Backend Engineer',         'agent',    ARRAY['code','shell','file_read','file_write'], ARRAY[]::TEXT[], 120),
    ('frontend_designer', 'Frontend Designer',        'agent',    ARRAY['code','file_read','file_write'],         ARRAY[]::TEXT[], 120),
    ('ux_researcher',     'UX Researcher',            'agent',    ARRAY['web_search','file_read'],                ARRAY[]::TEXT[], 60),
    ('security_reviewer', 'Security Reviewer',        'agent',    ARRAY['code','file_read','shell'],              ARRAY[]::TEXT[], 60),
    ('qa_tester',         'QA Tester',                'agent',    ARRAY['code','shell','file_read'],              ARRAY[]::TEXT[], 120),
    ('researcher',        'Research Agent',           'agent',    ARRAY['web_search','file_read'],                ARRAY[]::TEXT[], 60),
    ('devops_release',    'DevOps & Release',         'agent',    ARRAY['shell','file_read','file_write'],        ARRAY[]::TEXT[], 60),
    ('memory_librarian',  'Memory Librarian',         'agent',    ARRAY['file_read','file_write','memory'],       ARRAY[]::TEXT[], 60)
ON CONFLICT (agent_id) DO NOTHING;
