-- ============================================================
-- AIXING v0.1 — PostgreSQL 18
--
-- Core loop:
--   Strategy -> External Events -> Topics -> Decisions -> Content
--   -> Production -> Publication -> Performance -> Feedback -> next decision
--
-- UUID: UUIDv7 (native in PG18)
-- Status: CHECK constraints
-- Multi-tenancy: workspace_id redundancy + RLS
--
-- 与原始草稿的两处差异（均为执行所需，非设计变更）：
--   1. 移除 BEGIN/COMMIT —— 本文件由 Alembic 在单个事务中执行
--   2. publications 的唯一约束改为部分唯一索引，原因见该表定义处注释
-- ============================================================


-- ============================================================
-- 0. Extensions / Schemas
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS app;


-- ============================================================
-- 1. Users
-- ============================================================

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),

    email           text NOT NULL,
    display_name    text NOT NULL,

    avatar_url      text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT users_email_unique UNIQUE (email)
);


-- ============================================================
-- 2. Workspaces
-- ============================================================

CREATE TABLE workspaces (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),

    name            text NOT NULL,

    status          text NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN (
                        'ACTIVE',
                        'ARCHIVED'
                    )),

    created_by      uuid NOT NULL
                    REFERENCES users(id)
                    ON DELETE RESTRICT,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 3. Workspace Members
-- ============================================================

CREATE TABLE workspace_members (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id    uuid NOT NULL
                    REFERENCES workspaces(id)
                    ON DELETE CASCADE,

    user_id         uuid NOT NULL
                    REFERENCES users(id)
                    ON DELETE CASCADE,

    role            text NOT NULL
                    CHECK (role IN (
                        'OWNER',
                        'ADMIN',
                        'EDITOR',
                        'VIEWER'
                    )),

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT workspace_members_unique
        UNIQUE (workspace_id, user_id)
);


-- ============================================================
-- 4. Strategies
--
-- Main table intentionally slim.
-- Actual strategy content lives in strategy_versions.
-- ============================================================

CREATE TABLE strategies (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id            uuid NOT NULL
                            REFERENCES workspaces(id)
                            ON DELETE CASCADE,

    name                    text NOT NULL,

    status                  text NOT NULL DEFAULT 'DRAFT'
                            CHECK (status IN (
                                'DRAFT',
                                'ACTIVE',
                                'PAUSED',
                                'ARCHIVED'
                            )),

    current_version_number  integer NOT NULL DEFAULT 1
                            CHECK (current_version_number > 0),

    lock_version             bigint NOT NULL DEFAULT 0
                            CHECK (lock_version >= 0),

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 5. Strategy Versions
--
-- Immutable snapshots.
--
-- strategy_config example:
-- {
--   "positioning": "...",
--   "target_audience": [...],
--   "platforms": [...],
--   "content_directions": [...],
--   "tone": "...",
--   "goals": {...},
--   "boundaries": [...]
-- }
-- ============================================================

CREATE TABLE strategy_versions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    strategy_id         uuid NOT NULL
                        REFERENCES strategies(id)
                        ON DELETE CASCADE,

    version_number      integer NOT NULL
                        CHECK (version_number > 0),

    strategy_config     jsonb NOT NULL,

    change_summary      text,

    created_by          uuid NOT NULL
                        REFERENCES users(id)
                        ON DELETE RESTRICT,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT strategy_versions_unique
        UNIQUE (strategy_id, version_number),

    CONSTRAINT strategy_versions_config_object
        CHECK (jsonb_typeof(strategy_config) = 'object')
);


-- ============================================================
-- 6. External Events
--
-- What happened in the outside world. NOT a Topic.
-- ============================================================

CREATE TABLE external_events (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    source_type         text NOT NULL
                        CHECK (source_type IN (
                            'NEWS',
                            'SEARCH',
                            'SOCIAL',
                            'RSS',
                            'API',
                            'MANUAL',
                            'OTHER'
                        )),

    external_id         text NOT NULL,

    title               text NOT NULL,

    summary             text,

    url                 text,

    source_published_at timestamptz,

    discovered_at       timestamptz NOT NULL DEFAULT now(),

    raw_payload         jsonb,

    search_vector       tsvector
                        GENERATED ALWAYS AS (
                            to_tsvector(
                                'simple',
                                coalesce(title, '') || ' ' ||
                                coalesce(summary, '')
                            )
                        ) STORED,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT external_events_source_unique
        UNIQUE (workspace_id, source_type, external_id)
);


-- ============================================================
-- 7. Topics
--
-- One Topic can be supported by many External Events.
-- ============================================================

CREATE TABLE topics (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    primary_event_id    uuid
                        REFERENCES external_events(id)
                        ON DELETE SET NULL,

    title               text NOT NULL,

    description         text,

    status              text NOT NULL DEFAULT 'CANDIDATE'
                        CHECK (status IN (
                            'CANDIDATE',
                            'RECOMMENDED',
                            'SELECTED',
                            'REJECTED',
                            'EXPIRED',
                            'USED'
                        )),

    trend_score         numeric(8,4),
    relevance_score     numeric(8,4),
    potential_score     numeric(8,4),
    risk_score          numeric(8,4),

    discovered_at       timestamptz NOT NULL DEFAULT now(),

    selected_at         timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 8. Topic <-> External Event
-- ============================================================

CREATE TABLE topic_events (
    topic_id            uuid NOT NULL
                        REFERENCES topics(id)
                        ON DELETE CASCADE,

    event_id            uuid NOT NULL
                        REFERENCES external_events(id)
                        ON DELETE CASCADE,

    role                text NOT NULL DEFAULT 'SUPPORTING'
                        CHECK (role IN (
                            'PRIMARY',
                            'SUPPORTING',
                            'CONTRADICTING',
                            'BACKGROUND'
                        )),

    weight              numeric(8,4) NOT NULL DEFAULT 1.0
                        CHECK (weight >= 0),

    created_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (topic_id, event_id)
);


-- ============================================================
-- 9. Decisions
--
-- decision -> exact strategy_version
-- ============================================================

CREATE TABLE decisions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    strategy_version_id uuid NOT NULL
                        REFERENCES strategy_versions(id)
                        ON DELETE RESTRICT,

    topic_id            uuid
                        REFERENCES topics(id)
                        ON DELETE SET NULL,

    decision_type       text NOT NULL
                        CHECK (decision_type IN (
                            'TOPIC_SELECTION',
                            'ANGLE_SELECTION',
                            'CONTENT_DIRECTION',
                            'PUBLISHING',
                            'STRATEGY'
                        )),

    status              text NOT NULL DEFAULT 'PROPOSED'
                        CHECK (status IN (
                            'PROPOSED',
                            'APPROVED',
                            'REJECTED',
                            'EXECUTED',
                            'SUPERSEDED'
                        )),

    selected_option     text,

    rationale           text,

    score               numeric(8,4),

    decided_by          uuid
                        REFERENCES users(id)
                        ON DELETE SET NULL,

    decided_at          timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 10. Decision Traces
--
-- Structured evidence. NOT chain-of-thought.
-- ============================================================

CREATE TABLE decision_traces (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    decision_id         uuid NOT NULL
                        REFERENCES decisions(id)
                        ON DELETE CASCADE,

    trace_type          text NOT NULL
                        CHECK (trace_type IN (
                            'EVIDENCE',
                            'SCORE',
                            'RULE',
                            'ALTERNATIVE',
                            'CONSTRAINT'
                        )),

    source_type         text,

    source_id           uuid,

    data                jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 11. Contents
-- ============================================================

CREATE TABLE contents (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id            uuid NOT NULL
                            REFERENCES workspaces(id)
                            ON DELETE CASCADE,

    topic_id                uuid
                            REFERENCES topics(id)
                            ON DELETE SET NULL,

    decision_id             uuid
                            REFERENCES decisions(id)
                            ON DELETE SET NULL,

    title                   text NOT NULL,

    content_type             text NOT NULL DEFAULT 'VIDEO_SCRIPT'
                            CHECK (content_type IN (
                                'VIDEO_SCRIPT',
                                'ARTICLE',
                                'POST',
                                'AUDIO',
                                'OTHER'
                            )),

    status                  text NOT NULL DEFAULT 'DRAFT'
                            CHECK (status IN (
                                'DRAFT',
                                'REVIEW',
                                'APPROVED',
                                'PRODUCING',
                                'READY',
                                'PUBLISHED',
                                'ARCHIVED'
                            )),

    current_version_number  integer NOT NULL DEFAULT 1
                            CHECK (current_version_number > 0),

    lock_version             bigint NOT NULL DEFAULT 0
                            CHECK (lock_version >= 0),

    created_by              uuid NOT NULL
                            REFERENCES users(id)
                            ON DELETE RESTRICT,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 12. Content Versions
--
-- User edits are valuable feedback signals.
-- ============================================================

CREATE TABLE content_versions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    content_id          uuid NOT NULL
                        REFERENCES contents(id)
                        ON DELETE CASCADE,

    version_number      integer NOT NULL
                        CHECK (version_number > 0),

    title               text NOT NULL,

    body                text NOT NULL,

    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    change_summary      text,

    created_by          uuid
                        REFERENCES users(id)
                        ON DELETE SET NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT content_versions_unique
        UNIQUE (content_id, version_number)
);


-- ============================================================
-- 13. Tasks
--
-- Business/app task record, NOT the queue/outbox implementation.
-- Gives agent_runs a real parent entity from Day 1.
-- ============================================================

CREATE TABLE tasks (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    task_type           text NOT NULL,

    status              text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING',
                            'RUNNING',
                            'WAITING',
                            'SUCCEEDED',
                            'FAILED',
                            'CANCELLED'
                        )),

    priority            smallint NOT NULL DEFAULT 0,

    input               jsonb NOT NULL DEFAULT '{}'::jsonb,

    output              jsonb,

    error               jsonb,

    started_at          timestamptz,

    completed_at        timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 14. Productions
-- ============================================================

CREATE TABLE productions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    content_id          uuid NOT NULL
                        REFERENCES contents(id)
                        ON DELETE RESTRICT,

    content_version_number integer NOT NULL
                            CHECK (content_version_number > 0),

    status              text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING',
                            'RUNNING',
                            'SUCCEEDED',
                            'FAILED',
                            'CANCELLED'
                        )),

    production_type     text NOT NULL DEFAULT 'VIDEO'
                        CHECK (production_type IN (
                            'VIDEO',
                            'AUDIO',
                            'OTHER'
                        )),

    output_uri          text,

    duration_ms         bigint
                        CHECK (duration_ms IS NULL OR duration_ms >= 0),

    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    started_at          timestamptz,

    completed_at        timestamptz,

    error               jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
-- 15. Publications
--
-- One production can have multiple publications.
--
-- 原设计写的是 UNIQUE NULLS NOT DISTINCT (platform, external_post_id)。
-- PG 里 NULLS NOT DISTINCT 的含义是"把 NULL 当作相等值"，
-- 于是同一平台下所有 external_post_id 为空的行都会互相冲突：
-- 一个 workspace 只能存在一条"尚未发布"的记录，第二条 PENDING 直接报错。
-- 这里改为部分唯一索引，只在 external_post_id 有值时才约束。
-- ============================================================

CREATE TABLE publications (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    production_id       uuid NOT NULL
                        REFERENCES productions(id)
                        ON DELETE RESTRICT,

    platform             text NOT NULL,

    platform_account_ref text,

    status              text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING',
                            'PUBLISHING',
                            'PUBLISHED',
                            'FAILED',
                            'DELETED'
                        )),

    external_post_id    text,

    publish_url         text,

    scheduled_at        timestamptz,

    published_at        timestamptz,

    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    error               jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX publications_external_post_unique
    ON publications (platform, external_post_id)
    WHERE external_post_id IS NOT NULL;


-- ============================================================
-- 16. Performance Snapshots
--
-- publication + age_bucket -> one canonical snapshot.
-- ============================================================

CREATE TABLE performance_snapshots (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    publication_id      uuid NOT NULL
                        REFERENCES publications(id)
                        ON DELETE CASCADE,

    age_bucket          text NOT NULL
                        CHECK (age_bucket IN (
                            '1H',
                            '6H',
                            '24H',
                            '7D',
                            '30D'
                        )),

    captured_at         timestamptz NOT NULL DEFAULT now(),

    views               bigint NOT NULL DEFAULT 0
                        CHECK (views >= 0),

    likes               bigint NOT NULL DEFAULT 0
                        CHECK (likes >= 0),

    comments            bigint NOT NULL DEFAULT 0
                        CHECK (comments >= 0),

    shares              bigint NOT NULL DEFAULT 0
                        CHECK (shares >= 0),

    saves               bigint NOT NULL DEFAULT 0
                        CHECK (saves >= 0),

    followers_gained    bigint NOT NULL DEFAULT 0,

    watch_time_ms       bigint NOT NULL DEFAULT 0
                        CHECK (watch_time_ms >= 0),

    completion_rate     numeric(8,4),

    engagement_rate     numeric(8,4),

    raw_metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT performance_snapshot_unique
        UNIQUE (publication_id, age_bucket)
);


-- ============================================================
-- 17. Feedback
--
-- Exactly one business target: content OR publication.
-- ============================================================

CREATE TABLE feedback (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    content_id          uuid
                        REFERENCES contents(id)
                        ON DELETE CASCADE,

    publication_id      uuid
                        REFERENCES publications(id)
                        ON DELETE CASCADE,

    feedback_type       text NOT NULL
                        CHECK (feedback_type IN (
                            'HUMAN_REVIEW',
                            'USER_EDIT',
                            'PERFORMANCE',
                            'SYSTEM',
                            'OTHER'
                        )),

    source              text,

    rating              numeric(8,4),

    comment             text,

    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_by          uuid
                        REFERENCES users(id)
                        ON DELETE SET NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT feedback_exactly_one_target
        CHECK (
            (content_id IS NOT NULL)::int +
            (publication_id IS NOT NULL)::int = 1
        )
);


-- ============================================================
-- 18. Approvals
--
-- Business state transition, NOT generic feedback.
-- ============================================================

CREATE TABLE approvals (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    content_id          uuid NOT NULL
                        REFERENCES contents(id)
                        ON DELETE CASCADE,

    stage               text NOT NULL
                        CHECK (stage IN (
                            'CONTENT',
                            'PRODUCTION',
                            'PUBLICATION'
                        )),

    status              text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING',
                            'APPROVED',
                            'REJECTED',
                            'CANCELLED'
                        )),

    reviewer_id         uuid
                        REFERENCES users(id)
                        ON DELETE SET NULL,

    comment             text,

    decided_at          timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT approvals_unique_stage
        UNIQUE (content_id, stage)
);


-- ============================================================
-- 19. Agent Runs
--
-- Minimal AI runtime accounting. NO chain-of-thought.
-- ============================================================

CREATE TABLE agent_runs (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL
                        REFERENCES workspaces(id)
                        ON DELETE CASCADE,

    task_id             uuid
                        REFERENCES tasks(id)
                        ON DELETE SET NULL,

    agent_name          text NOT NULL,

    model_provider      text NOT NULL,

    model_name          text NOT NULL,

    input_tokens        bigint NOT NULL DEFAULT 0
                        CHECK (input_tokens >= 0),

    output_tokens       bigint NOT NULL DEFAULT 0
                        CHECK (output_tokens >= 0),

    cost                numeric(18,8) NOT NULL DEFAULT 0
                        CHECK (cost >= 0),

    latency_ms          bigint
                        CHECK (latency_ms IS NULL OR latency_ms >= 0),

    status              text NOT NULL DEFAULT 'RUNNING'
                        CHECK (status IN (
                            'RUNNING',
                            'SUCCEEDED',
                            'FAILED',
                            'CANCELLED'
                        )),

    started_at          timestamptz NOT NULL DEFAULT now(),

    completed_at        timestamptz,

    error               jsonb
);


-- ============================================================
-- 20. Indexes
-- ============================================================

CREATE INDEX idx_users_created_at
    ON users(created_at);

CREATE INDEX idx_workspace_members_user
    ON workspace_members(user_id);

CREATE INDEX idx_workspace_members_workspace_role
    ON workspace_members(workspace_id, role);

CREATE INDEX idx_strategies_workspace_status
    ON strategies(workspace_id, status);

CREATE INDEX idx_strategy_versions_workspace_strategy
    ON strategy_versions(workspace_id, strategy_id);

CREATE INDEX idx_strategy_versions_created_at
    ON strategy_versions(strategy_id, created_at DESC);

CREATE INDEX idx_external_events_workspace_discovered
    ON external_events(workspace_id, discovered_at DESC);

CREATE INDEX idx_external_events_source_published
    ON external_events(
        workspace_id,
        source_type,
        source_published_at DESC
    );

CREATE INDEX idx_external_events_search
    ON external_events
    USING GIN(search_vector);

CREATE INDEX idx_external_events_title_trgm
    ON external_events
    USING GIN(title gin_trgm_ops);

CREATE INDEX idx_topics_workspace_status
    ON topics(workspace_id, status);

CREATE INDEX idx_topics_workspace_created
    ON topics(workspace_id, created_at DESC);

CREATE INDEX idx_topics_primary_event
    ON topics(primary_event_id);

CREATE INDEX idx_topic_events_event
    ON topic_events(event_id);

CREATE INDEX idx_decisions_workspace_created
    ON decisions(workspace_id, created_at DESC);

CREATE INDEX idx_decisions_topic
    ON decisions(topic_id);

CREATE INDEX idx_decisions_strategy_version
    ON decisions(strategy_version_id);

CREATE INDEX idx_decision_traces_decision
    ON decision_traces(decision_id);

CREATE INDEX idx_contents_workspace_status
    ON contents(workspace_id, status);

CREATE INDEX idx_contents_workspace_updated
    ON contents(workspace_id, updated_at DESC);

CREATE INDEX idx_contents_topic
    ON contents(topic_id);

CREATE INDEX idx_contents_decision
    ON contents(decision_id);

CREATE INDEX idx_content_versions_content
    ON content_versions(content_id, version_number DESC);

CREATE INDEX idx_tasks_workspace_status
    ON tasks(workspace_id, status);

CREATE INDEX idx_tasks_pending
    ON tasks(workspace_id, priority DESC, created_at)
    WHERE status = 'PENDING';

CREATE INDEX idx_productions_workspace_status
    ON productions(workspace_id, status);

CREATE INDEX idx_productions_content
    ON productions(content_id);

CREATE INDEX idx_productions_pending
    ON productions(workspace_id, created_at)
    WHERE status = 'PENDING';

CREATE INDEX idx_publications_workspace_status
    ON publications(workspace_id, status);

CREATE INDEX idx_publications_production
    ON publications(production_id);

CREATE INDEX idx_publications_platform
    ON publications(workspace_id, platform, published_at DESC);

CREATE INDEX idx_performance_publication
    ON performance_snapshots(publication_id);

CREATE INDEX idx_performance_workspace_bucket
    ON performance_snapshots(
        workspace_id,
        age_bucket,
        captured_at DESC
    );

CREATE INDEX idx_feedback_workspace_created
    ON feedback(workspace_id, created_at DESC);

CREATE INDEX idx_feedback_content
    ON feedback(content_id);

CREATE INDEX idx_feedback_publication
    ON feedback(publication_id);

CREATE INDEX idx_approvals_workspace_status
    ON approvals(workspace_id, status);

CREATE INDEX idx_approvals_content
    ON approvals(content_id);

CREATE INDEX idx_agent_runs_workspace_started
    ON agent_runs(workspace_id, started_at DESC);

CREATE INDEX idx_agent_runs_task
    ON agent_runs(task_id);

CREATE INDEX idx_agent_runs_model
    ON agent_runs(
        workspace_id,
        model_provider,
        model_name,
        started_at DESC
    );


-- ============================================================
-- 21. updated_at Trigger
-- ============================================================

CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_workspaces_updated_at
BEFORE UPDATE ON workspaces
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_strategies_updated_at
BEFORE UPDATE ON strategies
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_topics_updated_at
BEFORE UPDATE ON topics
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_contents_updated_at
BEFORE UPDATE ON contents
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_tasks_updated_at
BEFORE UPDATE ON tasks
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_productions_updated_at
BEFORE UPDATE ON productions
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();

CREATE TRIGGER trg_publications_updated_at
BEFORE UPDATE ON publications
FOR EACH ROW
EXECUTE FUNCTION app.set_updated_at();


-- ============================================================
-- 22. Application Context
--
--   SET LOCAL app.user_id = '...';
--   SET LOCAL app.workspace_id = '...';
-- ============================================================

CREATE OR REPLACE FUNCTION app.current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(
        pg_catalog.current_setting('app.user_id', true),
        ''
    )::uuid;
$$;

CREATE OR REPLACE FUNCTION app.current_workspace_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(
        pg_catalog.current_setting('app.workspace_id', true),
        ''
    )::uuid;
$$;


-- ============================================================
-- 23. Workspace Membership Security Function
-- ============================================================

CREATE OR REPLACE FUNCTION app.is_workspace_member(target_workspace_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.workspace_members wm
        WHERE wm.workspace_id = target_workspace_id
          AND wm.user_id = app.current_user_id()
    );
$$;

REVOKE ALL ON FUNCTION app.is_workspace_member(uuid) FROM PUBLIC;


-- ============================================================
-- 24. RLS
-- ============================================================

ALTER TABLE strategies ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE topic_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE contents ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE productions ENABLE ROW LEVEL SECURITY;
ALTER TABLE publications ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- 25. RLS Policies
-- ============================================================

CREATE POLICY strategies_workspace_policy
ON strategies
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY strategy_versions_workspace_policy
ON strategy_versions
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY external_events_workspace_policy
ON external_events
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY topics_workspace_policy
ON topics
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY topic_events_workspace_policy
ON topic_events
FOR ALL
USING (
    EXISTS (
        SELECT 1
        FROM topics t
        WHERE t.id = topic_events.topic_id
          AND t.workspace_id = app.current_workspace_id()
          AND app.is_workspace_member(t.workspace_id)
    )
)
WITH CHECK (
    EXISTS (
        SELECT 1
        FROM topics t
        WHERE t.id = topic_events.topic_id
          AND t.workspace_id = app.current_workspace_id()
          AND app.is_workspace_member(t.workspace_id)
    )
);

CREATE POLICY decisions_workspace_policy
ON decisions
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY decision_traces_workspace_policy
ON decision_traces
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY contents_workspace_policy
ON contents
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY content_versions_workspace_policy
ON content_versions
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY tasks_workspace_policy
ON tasks
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY productions_workspace_policy
ON productions
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY publications_workspace_policy
ON publications
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY performance_workspace_policy
ON performance_snapshots
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY feedback_workspace_policy
ON feedback
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY approvals_workspace_policy
ON approvals
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);

CREATE POLICY agent_runs_workspace_policy
ON agent_runs
FOR ALL
USING (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
)
WITH CHECK (
    workspace_id = app.current_workspace_id()
    AND app.is_workspace_member(workspace_id)
);


-- ============================================================
-- 26. Comments
-- ============================================================

COMMENT ON TABLE external_events IS
'External world events collected from news/search/social/etc.';

COMMENT ON TABLE topics IS
'Content topics selected or evaluated by AIXING. Distinct from external events.';

COMMENT ON TABLE decisions IS
'Business decisions made by human or AI, bound to an exact strategy version.';

COMMENT ON TABLE decision_traces IS
'Structured evidence and scoring records for decisions. Never store chain-of-thought.';

COMMENT ON TABLE contents IS
'Durable content assets. Production is a separate lifecycle.';

COMMENT ON TABLE content_versions IS
'Immutable content snapshots; user edits become explicit versions.';

COMMENT ON TABLE productions IS
'Media production jobs and resulting assets.';

COMMENT ON TABLE publications IS
'Publishing instances of a production to external platforms.';

COMMENT ON TABLE performance_snapshots IS
'Canonical age-aligned performance snapshots for cross-content comparison.';

COMMENT ON TABLE feedback IS
'Human/system feedback. Quantitative repeatable evaluations belong in a future evaluations table.';

COMMENT ON TABLE approvals IS
'Business approval state machine. Not generic feedback.';

COMMENT ON TABLE agent_runs IS
'Minimal AI execution accounting: model, tokens, cost, latency, status. No chain-of-thought.';


-- ============================================================
-- 27. 应用角色授权（幂等）
--
-- 关键前提：RLS 对超级用户和表 owner 都不生效。
-- 若应用沿用 superuser 账号连接，上面所有 ENABLE ROW LEVEL SECURITY
-- 等于没打开。生产必须使用独立的非超级用户、非 owner 角色。
--
-- 角色由运维预先创建（见 scripts/app_role.sql），此处只在它已存在时补齐授权。
-- 必须包含 is_workspace_member 的 EXECUTE —— 该函数已对 PUBLIC 撤销，
-- 缺这一条会让所有带 RLS 的查询直接报 permission denied。
-- ============================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_app') THEN
        GRANT USAGE ON SCHEMA public, app TO xing_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO xing_app;
        GRANT EXECUTE ON FUNCTION app.is_workspace_member(uuid) TO xing_app;
        GRANT EXECUTE ON FUNCTION app.current_user_id() TO xing_app;
        GRANT EXECUTE ON FUNCTION app.current_workspace_id() TO xing_app;
    END IF;
END
$$;
