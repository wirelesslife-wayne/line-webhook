"""Photos LINE customers sent, shown as thumbnails on the desk (Wayne 11/17, preview v2 approved).

The LINE run reads the back office in the AI Chrome. The page shrinks each photo and posts it straight here with a
short-lived ticket, so the pictures never pass through the AI. Every photo is re-encoded (which also drops the
phone's location data), only signed-in staff can open it, and it is deleted after 30 days.
"""
import base64
import hashlib
import re
import secrets
from io import BytesIO
from PIL import Image, ImageOps

KEEP_DAYS = 30
TICKET_MINUTES = 40
RAW_LIMIT = 6_000_000           # one photo as the page sends it (already shrunk there)
THUMB_SIDE, FULL_SIDE = 400, 1600
PIC = re.compile(r'^[0-9]{6,32}$')  # LINE message id
ORIGIN = 'https://chat.line.biz'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_fb_pics (
 thread_id TEXT NOT NULL REFERENCES desk_fb_threads(id), pic TEXT NOT NULL,
 token TEXT NOT NULL UNIQUE CHECK (token ~ '^[0-9a-f]{32}$'),
 thumb BYTEA NOT NULL, big BYTEA NOT NULL, saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (thread_id, pic));
ALTER TABLE desk_fb_events ADD COLUMN IF NOT EXISTS pic TEXT;
'''


def clean_pic(value):
    """The run writes the id in groups (「1234-5678-…」) so the browser tool does not mistake it for a password."""
    value = str(value or '').replace('-', '')
    return value if PIC.match(value) else ''


def ticket(store):
    """A one-off upload pass for this run (40 minutes); only its hash is kept."""
    raw = secrets.token_hex(16)
    with store.cursor() as cur:
        cur.execute("DELETE FROM desk_marks WHERE name LIKE 'pic_ticket:%%' AND at < NOW() - INTERVAL '1 day'")
        cur.execute("INSERT INTO desk_marks(name,value,at) VALUES(%s,'',NOW())",
                    ('pic_ticket:' + hashlib.sha256(raw.encode()).hexdigest(),))
    return raw


def ticket_ok(store, raw):
    if not isinstance(raw, str) or not re.match(r'^[0-9a-f]{32}$', raw):
        return False
    return bool(store.one(f"SELECT 1 AS ok FROM desk_marks WHERE name=%s AND at > NOW() - INTERVAL '{TICKET_MINUTES} minutes'",
                          ('pic_ticket:' + hashlib.sha256(raw.encode()).hexdigest(),)))


def _jpeg(data, side):
    with Image.open(BytesIO(data)) as im:
        if im.format not in ('JPEG', 'PNG', 'WEBP'):
            raise ValueError('not a photo')
        if im.width * im.height > 40_000_000:
            raise ValueError('too large')
        im = ImageOps.exif_transpose(im).convert('RGB')
        im.thumbnail((side, side))
        out = BytesIO()
        im.save(out, 'JPEG', quality=82, optimize=True)  # a fresh file: no EXIF, no GPS
        return out.getvalue()


def save(store, key, pic, data_b64):
    """key: the LINE chat id; pic: the LINE message id; data: the photo (base64). Returns 'saved', 'have' or why not."""
    from .fb import thread_id
    key, pic = str(key or '').replace('-', ''), clean_pic(pic)
    if not re.match(r'^U[0-9a-f]{32}$', key) or not pic:
        return 'bad id'
    try:
        raw = base64.b64decode(data_b64 or '', validate=True)
    except Exception:
        return 'bad data'
    if not raw or len(raw) > RAW_LIMIT:
        return 'bad size'
    tid = thread_id('line_message', key)
    if store.one('SELECT 1 AS ok FROM desk_fb_pics WHERE thread_id=%s AND pic=%s', (tid, pic)):
        return 'have'
    try:
        thumb, full = _jpeg(raw, THUMB_SIDE), _jpeg(raw, FULL_SIDE)
    except Exception:
        return 'not a photo'
    with store.cursor() as cur:
        cur.execute('SELECT 1 FROM desk_fb_threads WHERE id=%s', (tid,))
        if not cur.fetchone():
            return 'no conversation'
        cur.execute("""INSERT INTO desk_fb_pics(thread_id,pic,token,thumb,big) VALUES(%s,%s,%s,%s,%s)
          ON CONFLICT(thread_id,pic) DO NOTHING""", (tid, pic, secrets.token_hex(16), thumb, full))
    return 'saved'


def todo(store, limit=40):
    """Photos the desk still lacks: LINE customers' photos from the last 30 days, newest conversations first."""
    rows = store.all(f"""SELECT t.key,e.pic FROM desk_fb_events e JOIN desk_fb_threads t ON t.id=e.thread_id
      WHERE t.channel='line_message' AND e.pic IS NOT NULL AND e.role='user'
        AND COALESCE(e.event_at,e.received_at) > NOW() - INTERVAL '{KEEP_DAYS} days'
        AND NOT EXISTS(SELECT 1 FROM desk_fb_pics p WHERE p.thread_id=e.thread_id AND p.pic=e.pic)
      ORDER BY t.last_at DESC, e.id DESC LIMIT %s""", (limit,))
    return [dict(key=r['key'], pic='-'.join(re.findall('.{1,6}', r['pic']))) for r in rows]


def cleanup(store):
    store.execute(f"DELETE FROM desk_fb_pics WHERE saved_at < NOW() - INTERVAL '{KEEP_DAYS} days'")


def tokens(store, thread_ids):
    """{(thread_id, pic): token} for the photos still kept."""
    if not thread_ids:
        return {}
    return {(r['thread_id'], r['pic']): r['token'] for r in store.all(
        f"""SELECT thread_id,pic,token FROM desk_fb_pics WHERE thread_id=ANY(%s)
          AND saved_at >= NOW() - INTERVAL '{KEEP_DAYS} days'""", (list(thread_ids),))}


def read(store, token, size):
    if not isinstance(token, str) or not re.match(r'^[0-9a-f]{32}$', token) or size not in ('thumb', 'full'):
        return None
    row = store.one(f"SELECT {dict(thumb='thumb', full='big')[size]} AS data FROM desk_fb_pics WHERE token=%s AND saved_at >= NOW() - INTERVAL '{KEEP_DAYS} days'", (token,))
    return bytes(row['data']) if row else None
