"""Browser import and immutable approval snapshots; deployment applies this SQL."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_fb_threads (
 id TEXT PRIMARY KEY, channel TEXT NOT NULL CHECK(channel IN ('fb_message','ig_message','fb_comment','ig_comment')),
 key TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'waiting', version BIGINT NOT NULL DEFAULT 0,
 last_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), draft JSONB, drafted_version BIGINT,
 draft_token UUID, draft_started_at TIMESTAMPTZ, note TEXT NOT NULL DEFAULT '',
 pending_instruction TEXT NOT NULL DEFAULT '', pending_original TEXT NOT NULL DEFAULT '', pending_rule BIGINT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(channel,key));
CREATE INDEX IF NOT EXISTS desk_fb_threads_wait ON desk_fb_threads(status,last_at);
CREATE TABLE IF NOT EXISTS desk_fb_events (
 id BIGSERIAL PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES desk_fb_threads(id),
 fingerprint TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','page')),
 content TEXT NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 event_at TIMESTAMPTZ, origin TEXT NOT NULL DEFAULT 'inbox',
 source_version BIGINT, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(thread_id,fingerprint));
CREATE INDEX IF NOT EXISTS desk_fb_events_thread ON desk_fb_events(thread_id,id);
CREATE TABLE IF NOT EXISTS desk_fb_attempts (
 retry_key UUID PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES desk_fb_threads(id),
 staff_id BIGINT NOT NULL REFERENCES desk_users(id), source_version BIGINT NOT NULL,
 content TEXT NOT NULL, public_confirmed BOOLEAN NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('approved','sent','failed','expired','cancelled')),
 reason TEXT NOT NULL DEFAULT '', approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
ALTER TABLE desk_fb_attempts ADD COLUMN IF NOT EXISTS question TEXT NOT NULL DEFAULT '';
ALTER TABLE desk_fb_attempts ADD COLUMN IF NOT EXISTS ai_draft TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS desk_fb_one_approval ON desk_fb_attempts(thread_id) WHERE status='approved';
CREATE INDEX IF NOT EXISTS desk_fb_attempts_thread ON desk_fb_attempts(thread_id,approved_at DESC);
CREATE TABLE IF NOT EXISTS desk_fb_media (
 id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'), thread_id TEXT NOT NULL REFERENCES desk_fb_threads(id),
 attempt_key UUID REFERENCES desk_fb_attempts(retry_key), data BYTEA,
 content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg','image/png','video/mp4')),
 size BIGINT NOT NULL CHECK (size > 0), filename TEXT NOT NULL,
 uploaded_by BIGINT NOT NULL REFERENCES desk_users(id),
 uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), removed_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS desk_fb_media_thread ON desk_fb_media(thread_id,uploaded_at);
CREATE TABLE IF NOT EXISTS desk_edit_rules (
 id BIGSERIAL PRIMARY KEY, instruction TEXT NOT NULL, channel TEXT NOT NULL,
 conversation_summary TEXT NOT NULL, original_draft TEXT NOT NULL,
 revised_draft TEXT NOT NULL DEFAULT '', created_by BIGINT NOT NULL REFERENCES desk_users(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE INDEX IF NOT EXISTS desk_edit_rules_active ON desk_edit_rules(active,id DESC);
"""
