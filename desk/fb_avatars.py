"""The customer's profile picture next to the name on the review page (Wayne 11/14: it looks more real).

The browser run gives either the picture's public address (LINE: profile.line-scdn.net) or a small screenshot of the
picture it saved on this computer (Meta signs its picture addresses, which the browser tool may not read out).
Pictures are shrunk to 96 px JPEG here, so nothing but the picture itself is kept, and only logged-in staff see them.
"""
from io import BytesIO
import re
import warnings
from PIL import Image, ImageOps

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_fb_avatars (
 thread_id TEXT PRIMARY KEY REFERENCES desk_fb_threads(id), data BYTEA NOT NULL,
 fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""
HOSTS = re.compile(r'^https://([a-z0-9-]+\.)*(line-scdn\.net|fbcdn\.net|cdninstagram\.com)/')
LIMIT = 2 * 1024 * 1024
SIZE = 96


def shrink(data):
    """96 px square JPEG, or None when it is not a plain picture."""
    if not data or len(data) > LIMIT:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 16_000_000:
                    return None
                square = ImageOps.fit(ImageOps.exif_transpose(image).convert('RGB'), (SIZE, SIZE))
                out = BytesIO()
                square.save(out, 'JPEG', quality=85)
                return out.getvalue()
    except Exception:
        return None


def fresh(store, tid, days=7):
    return bool(store.one(f"SELECT 1 FROM desk_fb_avatars WHERE thread_id=%s AND fetched_at > NOW()-INTERVAL '{int(days)} days'", (tid,)))


def save(store, tid, data):
    small = shrink(data)
    if not small:
        return False
    with store.cursor() as cur:
        cur.execute('SELECT 1 FROM desk_fb_threads WHERE id=%s', (tid,))
        if not cur.fetchone():
            return False
        cur.execute("""INSERT INTO desk_fb_avatars(thread_id,data,fetched_at) VALUES(%s,%s,NOW())
          ON CONFLICT(thread_id) DO UPDATE SET data=EXCLUDED.data,fetched_at=NOW()""", (tid, small))
    return True


def collect(store, rows, get=None):
    """For a browser run's rows: a saved screenshot wins; an address is downloaded once a week. Never fatal."""
    from pathlib import Path
    import requests
    get = get or (lambda u: requests.get(u, timeout=(4, 6), headers={'User-Agent': 'Mozilla/5.0'}))
    saved = 0
    for row in rows:
        value = row.get('avatar')
        line = row.get('avatar_line')
        if isinstance(line, str) and re.fullmatch(r'[A-Za-z0-9_~-]{10,400}', line):
            # The LINE run writes the picture id with a 「~」 every few letters (the browser tool hides long codes).
            value = 'https://profile.line-scdn.net/' + line.replace('~', '') + '/preview'
        if not isinstance(value, str) or not value or len(value) > 4000:
            continue
        try:
            if HOSTS.match(value):
                if fresh(store, row['id']):
                    continue
                r = get(value)
                data = r.content if r.status_code == 200 else b''
            elif not value.startswith(('http:', 'https:')):
                data = Path(value).read_bytes()
            else:
                continue  # only the platforms' own picture servers
            saved += save(store, row['id'], data)
        except Exception:
            pass
    return saved


def read(store, tid):
    return store.one('SELECT data FROM desk_fb_avatars WHERE thread_id=%s', (tid,)) if isinstance(tid, str) else None


def have(store, line=False):
    """Conversation keys that already have a picture, so the browser run does not capture them again."""
    return [r['key'] for r in store.all("""SELECT t.key FROM desk_fb_threads t JOIN desk_fb_avatars a ON a.thread_id=t.id
      WHERE (t.channel='line_message')=%s ORDER BY t.key""", (line,))]
