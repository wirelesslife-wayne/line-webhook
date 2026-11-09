"""Postgres storage. Source messages are strictly SELECT-only."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import psycopg2
from psycopg2.extras import RealDictCursor, Json

UTC = timezone.utc


def now():
    return datetime.now(UTC)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_users (
 id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS desk_login_attempts (
 bucket TEXT PRIMARY KEY, failures INT NOT NULL DEFAULT 0, locked_until TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS desk_sessions (
 sid TEXT PRIMARY KEY, user_id BIGINT REFERENCES desk_users(id), csrf TEXT NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS desk_meta (key TEXT PRIMARY KEY, value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS desk_kb (
 name TEXT PRIMARY KEY, content JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS desk_qa (
 source_id BIGINT PRIMARY KEY, asked_at TIMESTAMPTZ NOT NULL,
 question TEXT NOT NULL, answer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS desk_conversations (
 user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, last_source_id BIGINT NOT NULL,
 handled_upto BIGINT NOT NULL DEFAULT 0, last_at TIMESTAMPTZ NOT NULL,
 first_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
 draft JSONB, drafted_upto BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS desk_conversations_wait ON desk_conversations(status,first_at);
CREATE TABLE IF NOT EXISTS desk_send_attempts (
 retry_key UUID PRIMARY KEY, user_id TEXT NOT NULL, staff_id BIGINT REFERENCES desk_users(id),
 content TEXT NOT NULL, images JSONB NOT NULL, source_upto BIGINT NOT NULL,
 status TEXT NOT NULL, http_status INT, reason TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS desk_attempts_user ON desk_send_attempts(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS desk_outgoing (
 id BIGSERIAL PRIMARY KEY, retry_key UUID UNIQUE REFERENCES desk_send_attempts(retry_key),
 user_id TEXT NOT NULL, staff_id BIGINT REFERENCES desk_users(id), content TEXT NOT NULL,
 images JSONB NOT NULL, http_status INT NOT NULL, sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS desk_actions (
 id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, staff_id BIGINT REFERENCES desk_users(id),
 action TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS desk_ai_usage (
 id UUID PRIMARY KEY, month TEXT NOT NULL, model TEXT NOT NULL,
 usd NUMERIC(18,9) NOT NULL CHECK (usd >= 0), status TEXT NOT NULL,
 input_tokens BIGINT, cached_tokens BIGINT, output_tokens BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS desk_ai_usage_month ON desk_ai_usage(month);
'''


class Store:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def connect(self):
        conn = psycopg2.connect(self.url, connect_timeout=10, application_name='cs-desk')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def cursor(self):
        with self.connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur

    def initialize(self):
        with self.cursor() as cur:
            cur.execute(SCHEMA)

    def one(self, sql, args=()):
        with self.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def all(self, sql, args=()):
        with self.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def execute(self, sql, args=()):
        with self.cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount

    @contextmanager
    def conversation_transaction(self, uid):
        """All state decisions and writes share the same locked row/transaction."""
        with self.cursor() as cur:
            cur.execute('SELECT * FROM desk_conversations WHERE user_id=%s FOR UPDATE', (uid,))
            yield cur, cur.fetchone()

    def latest_source_cursor(self, cur, uid):
        cur.execute('SELECT COALESCE(MAX(id),0) AS id FROM messages WHERE oa IS NULL AND user_id=%s', (uid,))
        return cur.fetchone()['id']

    def kb(self,names=None):
        rows = self.all('SELECT name,content FROM desk_kb WHERE name=ANY(%s)',(names,)) if names else self.all('SELECT name,content FROM desk_kb')
        return {r['name']: r['content'] for r in rows}

    def put_kb(self, name, value):
        self.execute('''INSERT INTO desk_kb(name,content) VALUES(%s,%s)
          ON CONFLICT(name) DO UPDATE SET content=EXCLUDED.content,updated_at=NOW()''', (name, Json(value)))

    def meta(self, key, default=None):
        r = self.one('SELECT value FROM desk_meta WHERE key=%s', (key,))
        return r['value'] if r else default

    def set_meta(self, key, value):
        self.execute('''INSERT INTO desk_meta VALUES(%s,%s)
          ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value''', (key, Json(value)))

    @contextmanager
    def lock(self, name):
        # Session lock survives transaction commits, is released on connection close.
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(hashtext(%s))', (name,))
                acquired = cur.fetchone()[0]
            try:
                yield acquired
            finally:
                if acquired:
                    with conn.cursor() as cur:
                        cur.execute('SELECT pg_advisory_unlock(hashtext(%s))', (name,))

    def conversation(self, uid):
        return self.one('SELECT * FROM desk_conversations WHERE user_id=%s', (uid,))

    def incoming(self, uid, after=0):
        return self.all('''SELECT id,time_str,message_type,content FROM messages
            WHERE oa IS NULL AND user_id=%s AND id>%s ORDER BY id''', (uid, after))

    def latest_source(self, uid):
        r = self.one('SELECT COALESCE(MAX(id),0) AS id FROM messages WHERE oa IS NULL AND user_id=%s', (uid,))
        return r['id']

    def ingest(self):
        from .worker import utc_time
        with self.lock('desk-ingest') as acquired:
            if not acquired:
                return 0
            watermark = self.meta('source_cursor')
            if watermark is None:
                # First activation adopts recent 3 hours, never drafts the whole archive.
                from datetime import timedelta
                r = self.one('''SELECT COALESCE(MIN(id)-1,(SELECT COALESCE(MAX(id),0) FROM messages)) AS id
                  FROM messages WHERE oa IS NULL AND time_str >= %s''',
                  ((now()-timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S'),))
                watermark = r['id']
            count = 0
            while True:
                rows = self.all('''SELECT id,user_id,display_name,time_str FROM messages
                  WHERE id>%s AND oa IS NULL ORDER BY id LIMIT 500''', (watermark,))
                if not rows:
                    break
                with self.cursor() as cur:
                    for r in rows:
                        if not r['user_id']:
                            continue
                        t = utc_time(r['time_str'])
                        # Same row lock used by mark_done/send/redraft. The upsert also
                        # locks new rows, including concurrent first ingestion.
                        cur.execute('SELECT user_id FROM desk_conversations WHERE user_id=%s FOR UPDATE', (r['user_id'],))
                        cur.execute('''INSERT INTO desk_conversations
                          (user_id,display_name,last_source_id,last_at,first_at) VALUES(%s,%s,%s,%s,%s)
                          ON CONFLICT(user_id) DO UPDATE SET display_name=EXCLUDED.display_name,
                          last_source_id=EXCLUDED.last_source_id,last_at=EXCLUDED.last_at,
                          first_at=CASE WHEN desk_conversations.status IN ('done','skip')
                            THEN EXCLUDED.first_at ELSE desk_conversations.first_at END,
                          status='waiting', updated_at=NOW()
                          WHERE desk_conversations.last_source_id < EXCLUDED.last_source_id''',
                          (r['user_id'], r['display_name'] or '未提供名稱', r['id'], t, t))
                    watermark = rows[-1]['id']
                    cur.execute('''INSERT INTO desk_meta VALUES('source_cursor',%s)
                      ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value''', (Json(watermark),))
                count += len(rows)
            return count
