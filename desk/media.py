"""Employee uploads; only opaque read tokens are public. No outbound requests."""
from io import BytesIO
import os
import re
import secrets
import warnings
from urllib.parse import urlsplit
from PIL import Image, ImageOps, UnidentifiedImageError
from .errors import ActionError

# Use decimal MB conservatively, so byte counts never exceed either MB convention.
MB = 1_000_000
IMAGE_LIMIT = 10 * MB
VIDEO_LIMIT = 50 * MB
PREVIEW_LIMIT = MB
CAPACITY = 500 * MB
TOKEN = re.compile(r'^[0-9a-f]{32}$')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS desk_media (
 id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
 preview_token TEXT UNIQUE NOT NULL CHECK (preview_token ~ '^[0-9a-f]{32}$' AND preview_token <> id),
 data BYTEA, preview_data BYTEA, content_type TEXT NOT NULL
   CHECK (content_type IN ('image/jpeg','image/png','video/mp4')),
 size BIGINT NOT NULL CHECK (size > 0), preview_size INT NOT NULL CHECK (preview_size > 0),
 filename TEXT NOT NULL, uploaded_by BIGINT NOT NULL REFERENCES desk_users(id),
 user_id TEXT NOT NULL REFERENCES desk_conversations(user_id),
 uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
 retry_key UUID REFERENCES desk_send_attempts(retry_key), removed_at TIMESTAMPTZ,
 cleared_at TIMESTAMPTZ,
 CHECK ((data IS NULL) = (preview_data IS NULL)));
CREATE INDEX IF NOT EXISTS desk_media_uploaded ON desk_media(uploaded_at) WHERE data IS NOT NULL;
CREATE INDEX IF NOT EXISTS desk_media_owner ON desk_media(user_id,uploaded_by);
'''


def image_info(data, mime, *, preview=False):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != {'image/jpeg':'JPEG','image/png':'PNG'}.get(mime):
                    raise ValueError()
                if image.width * image.height > 40_000_000 or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError()
                if preview and max(image.size) > 240:
                    raise ValueError()
                image.verify()
            with Image.open(BytesIO(data)) as decoded:
                decoded.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ActionError('圖片內容不正確、尺寸過大或格式不符，請改用 JPEG／PNG', 400)


def mp4_valid(data):
    # Check bounded ISO BMFF top-level boxes, not merely a caller-supplied MIME.
    pos, boxes, compatible = 0, set(), False
    while pos < len(data):
        if len(data) - pos < 8:
            return False
        size = int.from_bytes(data[pos:pos+4], 'big')
        kind, header = data[pos+4:pos+8], 8
        if size == 1:
            if len(data) - pos < 16:
                return False
            size, header = int.from_bytes(data[pos+8:pos+16], 'big'), 16
        elif size == 0:
            size = len(data) - pos
        if size < header or pos + size > len(data):
            return False
        if kind == b'ftyp':
            payload = data[pos+header:pos+size]
            brands = [payload[:4]] + [payload[i:i+4] for i in range(8, len(payload), 4)]
            compatible = len(payload) >= 8 and any(b in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'M4V ') for b in brands)
        if size > header:
            boxes.add(kind)
        pos += size
    return compatible and {b'ftyp', b'moov', b'mdat'} <= boxes


def validate_upload(file, preview):
    mime = file.mimetype
    if mime not in ('image/jpeg', 'image/png', 'video/mp4'):
        raise ActionError('只接受 JPEG、PNG 圖片或 MP4 影片', 400)
    limit = VIDEO_LIMIT if mime == 'video/mp4' else IMAGE_LIMIT
    data = file.stream.read(limit + 1)
    if len(data) > limit:
        raise ActionError('影片太大，請改傳 YouTube 連結' if mime == 'video/mp4'
                          else '圖片超過 10MB，請先壓縮或改選較小圖片', 400)
    if not data:
        raise ActionError('檔案內容是空的', 400)
    if mime == 'video/mp4':
        if not mp4_valid(data):
            raise ActionError('影片不是有效的 MP4 檔案', 400)
    else:
        image_info(data, mime)
    if preview is None or preview.mimetype != 'image/jpeg':
        raise ActionError('請重新選取檔案，產生 JPEG 預覽圖', 400)
    thumb = preview.stream.read(PREVIEW_LIMIT + 1)
    if not thumb or len(thumb) > PREVIEW_LIMIT:
        raise ActionError('預覽圖大小不正確', 400)
    image_info(thumb, 'image/jpeg', preview=True)
    # Re-encode thumbnail to strip metadata and untrusted trailing payloads.
    with Image.open(BytesIO(thumb)) as image:
        output = BytesIO()
        ImageOps.exif_transpose(image).convert('RGB').save(output, 'JPEG', quality=85)
        thumb = output.getvalue()
    filename = (file.filename or '附件').replace('\\', '/').rsplit('/', 1)[-1]
    filename = ''.join(c for c in filename if c.isprintable())[:180] or '附件'
    return data, thumb, mime, filename


def descriptor(row, base=''):
    return {'id':row['id'], 'type':'video' if row['content_type']=='video/mp4' else 'image',
            'url':base+'/desk/media/'+row['id'],
            'preview_url':base+'/desk/media/'+row['preview_token'],
            'name':row['filename']}


def public_base():
    value = os.environ.get('DESK_MEDIA_BASE_URL', '').rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or len(value) > 1500):
        raise ActionError('附件公開網址尚未設定，請通知管理者設定 HTTPS 網址', 503)
    return value


def media_lock(cur):
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('desk-media-storage'))")


def cleanup_cursor(cur, incoming=0):
    cur.execute('''UPDATE desk_media SET data=NULL,preview_data=NULL,cleared_at=NOW()
      WHERE data IS NOT NULL AND uploaded_at <= NOW()-INTERVAL '30 days' ''')
    cur.execute('SELECT COALESCE(SUM(size+preview_size),0) AS total FROM desk_media WHERE data IS NOT NULL')
    total = cur.fetchone()['total']
    if total + incoming <= CAPACITY:
        return
    cur.execute('''SELECT id,size+preview_size AS size FROM desk_media
      WHERE data IS NOT NULL AND sent_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM desk_send_attempts a WHERE a.retry_key=desk_media.retry_key
                      AND a.status IN ('sending','unknown'))
      ORDER BY sent_at,uploaded_at,id FOR UPDATE''')
    for row in cur.fetchall():
        if total + incoming <= CAPACITY:
            break
        cur.execute('UPDATE desk_media SET data=NULL,preview_data=NULL,cleared_at=NOW() WHERE id=%s', (row['id'],))
        total -= row['size']
    if total + incoming > CAPACITY:
        raise ActionError('附件空間已滿，尚未送出的檔案會保留；請移除不用的附件或改傳連結', 507)


def cleanup(store):
    with store.cursor() as cur:
        media_lock(cur)
        cleanup_cursor(cur)


def upload(store, staff, uid, file, preview):
    if not isinstance(uid, str) or not store.conversation(uid):
        raise ActionError('找不到對話', 404)
    data, thumb, mime, filename = validate_upload(file, preview)
    with store.cursor() as cur:
        media_lock(cur)
        cleanup_cursor(cur, len(data)+len(thumb))
        cur.execute('''SELECT COUNT(*) AS n FROM desk_media m WHERE user_id=%s AND uploaded_by=%s
          AND data IS NOT NULL AND removed_at IS NULL AND sent_at IS NULL
          AND (retry_key IS NULL OR EXISTS (SELECT 1 FROM desk_send_attempts a
               WHERE a.retry_key=m.retry_key AND a.status='failed'))''', (uid, staff['id']))
        if cur.fetchone()['n'] >= 4:
            raise ActionError('最多只能加入 4 個檔案，請先移除不用的附件', 400)
        token, preview_token = secrets.token_hex(16), secrets.token_hex(16)
        cur.execute('''INSERT INTO desk_media
          (id,preview_token,data,preview_data,content_type,size,preview_size,filename,uploaded_by,user_id)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,preview_token,content_type,filename''',
          (token,preview_token,data,thumb,mime,len(data),len(thumb),filename,staff['id'],uid))
        return descriptor(cur.fetchone())


def available(store, staff, uid):
    rows = store.all('''SELECT id,preview_token,content_type,filename FROM desk_media m
      WHERE user_id=%s AND uploaded_by=%s AND data IS NOT NULL AND removed_at IS NULL AND sent_at IS NULL
      AND uploaded_at > NOW()-INTERVAL '30 days'
      AND (retry_key IS NULL OR EXISTS (SELECT 1 FROM desk_send_attempts a WHERE a.retry_key=m.retry_key AND a.status='failed'))
      ORDER BY uploaded_at,id LIMIT 4''', (uid,staff['id']))
    return [descriptor(row) for row in rows]


def remove(store, staff, token, uid):
    with store.cursor() as cur:
        media_lock(cur)
        cur.execute('''UPDATE desk_media m SET removed_at=NOW(),data=NULL,preview_data=NULL,cleared_at=NOW()
          WHERE id=%s AND uploaded_by=%s AND user_id=%s AND sent_at IS NULL
          AND (retry_key IS NULL OR EXISTS (SELECT 1 FROM desk_send_attempts a WHERE a.retry_key=m.retry_key AND a.status='failed'))''',
          (token,staff['id'],uid))
        if cur.rowcount != 1:
            raise ActionError('附件已使用、待查核或不屬於此對話，無法移除', 409)


def resolve(cur, staff, uid, tokens):
    media_lock(cur)
    items = []
    base = public_base() if tokens else ''
    for token in tokens:
        cur.execute('''SELECT id,preview_token,content_type,filename FROM desk_media m
          WHERE id=%s AND uploaded_by=%s AND user_id=%s AND data IS NOT NULL
          AND removed_at IS NULL AND sent_at IS NULL AND uploaded_at > NOW()-INTERVAL '30 days'
          AND (retry_key IS NULL OR EXISTS (SELECT 1 FROM desk_send_attempts a WHERE a.retry_key=m.retry_key AND a.status='failed'))
          FOR UPDATE''', (token,staff['id'],uid))
        row = cur.fetchone()
        if not row:
            raise ActionError('附件已失效、已使用或不屬於此對話，請重新選取', 400)
        items.append(descriptor(row,base))
    return items
