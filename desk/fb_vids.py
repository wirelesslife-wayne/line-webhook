"""Videos LINE customers sent, played on the desk (Wayne 11/17; ChatGPT and Gemini reviewed the preview).

Like fb_pics: the LINE run's page posts the cover and the video with its short-lived ticket. Kept in their own table
so the conversation list never reads them. Each video up to 20 MB, all videos together up to 300 MB (the oldest
are cleared first, keeping the cover), everything deleted after 30 days. Only signed-in staff can play them.
"""
import base64
import re
import secrets
from io import BytesIO
from PIL import Image, ImageOps
from .fb_pics import KEEP_DAYS, clean_pic

VIDEO_LIMIT = 20_000_000
TOTAL_LIMIT = 300_000_000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_fb_vids (
 thread_id TEXT NOT NULL REFERENCES desk_fb_threads(id), pic TEXT NOT NULL,
 token TEXT NOT NULL UNIQUE CHECK (token ~ '^[0-9a-f]{32}$'),
 cover BYTEA NOT NULL, video BYTEA, size BIGINT NOT NULL DEFAULT 0, duration_ms INT NOT NULL DEFAULT 0,
 state TEXT NOT NULL CHECK (state IN ('wait','ok','big','cleared')), saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (thread_id, pic));
'''


def _tid(key):
    from .fb import thread_id
    key = str(key or '').replace('-', '')
    return thread_id('line_message', key) if re.match(r'^U[0-9a-f]{32}$', key) else None


def save_cover(store, key, pic, data_b64, size, duration_ms):
    """Step 1 of a video: its cover, length and size. Over 20 MB only the cover is kept ('big')."""
    tid, pic = _tid(key), clean_pic(pic)
    if not tid or not pic:
        return 'bad id'
    try:
        raw = base64.b64decode(data_b64 or '', validate=True)
        with Image.open(BytesIO(raw)) as im:
            if im.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError
            im = ImageOps.exif_transpose(im).convert('RGB')
            im.thumbnail((640, 640))
            out = BytesIO()
            im.save(out, 'JPEG', quality=82)
        size, duration_ms = int(size or 0), int(duration_ms or 0)
    except Exception:
        return 'not a photo'
    with store.cursor() as cur:
        cur.execute('SELECT 1 FROM desk_fb_threads WHERE id=%s', (tid,))
        if not cur.fetchone():
            return 'no conversation'
        cur.execute("""INSERT INTO desk_fb_vids(thread_id,pic,token,cover,size,duration_ms,state) VALUES(%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(thread_id,pic) DO NOTHING RETURNING state""",
                    (tid, pic, secrets.token_hex(16), out.getvalue(), max(0, size), max(0, duration_ms),
                     'big' if size > VIDEO_LIMIT else 'wait'))
        row = cur.fetchone()
    return 'have' if not row else 'big' if row['state'] == 'big' else 'saved'


def save_video(store, key, pic, data):
    """Step 2: the video itself (an MP4 up to 20 MB). Makes room by clearing the oldest videos first."""
    tid, pic = _tid(key), clean_pic(pic)
    if not tid or not pic:
        return 'bad id'
    if not data or len(data) > VIDEO_LIMIT or data[4:8] != b'ftyp':
        return 'not a video'
    with store.cursor() as cur:
        cur.execute("SELECT state FROM desk_fb_vids WHERE thread_id=%s AND pic=%s FOR UPDATE", (tid, pic))
        row = cur.fetchone()
        if not row:
            return 'no cover'
        if row['state'] != 'wait':
            return 'have'
        cur.execute("SELECT thread_id,pic,size FROM desk_fb_vids WHERE state='ok' ORDER BY saved_at")
        kept = cur.fetchall()
        total = sum(r['size'] for r in kept)
        for old in kept:
            if total + len(data) <= TOTAL_LIMIT:
                break
            cur.execute("UPDATE desk_fb_vids SET video=NULL,state='cleared' WHERE thread_id=%s AND pic=%s", (old['thread_id'], old['pic']))
            total -= old['size']
        cur.execute("UPDATE desk_fb_vids SET video=%s,size=%s,state='ok' WHERE thread_id=%s AND pic=%s",
                    (data, len(data), tid, pic))
    return 'saved'


def todo(store, limit=10):
    """Videos the desk still lacks (no cover yet, or the video not uploaded), last 30 days."""
    rows = store.all(f"""SELECT t.key,e.pic FROM desk_fb_events e JOIN desk_fb_threads t ON t.id=e.thread_id
      WHERE t.channel='line_message' AND e.pic IS NOT NULL AND e.role='user' AND e.content LIKE '[影片]%%'
        AND COALESCE(e.event_at,e.received_at) > NOW() - INTERVAL '{KEEP_DAYS} days'
        AND NOT EXISTS(SELECT 1 FROM desk_fb_vids v WHERE v.thread_id=e.thread_id AND v.pic=e.pic AND v.state<>'wait')
      ORDER BY t.last_at DESC, e.id DESC LIMIT %s""", (limit,))
    return [dict(key=r['key'], pic='-'.join(re.findall('.{1,6}', r['pic'])), kind='video') for r in rows]


def cleanup(store):
    store.execute(f"DELETE FROM desk_fb_vids WHERE saved_at < NOW() - INTERVAL '{KEEP_DAYS} days'")


def info(store, thread_ids):
    """{(thread_id, pic): {token, state, dur}} for the videos still kept (cover at least)."""
    if not thread_ids:
        return {}
    return {(r['thread_id'], r['pic']): dict(token=r['token'], state=r['state'], dur=r['duration_ms']) for r in store.all(
        f"""SELECT thread_id,pic,token,state,duration_ms FROM desk_fb_vids WHERE thread_id=ANY(%s)
          AND saved_at >= NOW() - INTERVAL '{KEEP_DAYS} days'""", (list(thread_ids),))}


_CACHE = {}  # phones ask for a video in many small pieces; keep the last few in memory


def read(store, token, part):
    if not isinstance(token, str) or not re.match(r'^[0-9a-f]{32}$', token) or part not in ('cover', 'video'):
        return None
    if part == 'video' and token in _CACHE:
        return _CACHE[token]
    row = store.one(f"SELECT {part} AS data FROM desk_fb_vids WHERE token=%s AND saved_at >= NOW() - INTERVAL '{KEEP_DAYS} days'", (token,))
    data = bytes(row['data']) if row and row['data'] is not None else None
    if data and part == 'video':
        while len(_CACHE) >= 3:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[token] = data
    return data
