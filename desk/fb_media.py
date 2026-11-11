"""Attachments Wayne adds to an FB/IG private reply. The browser run uploads them with the approved text.

Bytes stay in the desk database until exported to the local machine that operates the inbox;
nothing here contacts Meta.
"""
import secrets
from pathlib import Path
from .errors import ActionError
from .knowledge import require_reviewer
from .media import MB, TOKEN, image_info, mp4_valid

IMAGE_LIMIT = 10 * MB
VIDEO_LIMIT = 25 * MB   # Messenger's own attachment limit
PER_THREAD = 3
CAPACITY = 300 * MB
TYPES = {'image/jpeg': '.jpg', 'image/png': '.png', 'video/mp4': '.mp4'}
# Free to use: not removed, bytes present, and not tied to a reply that is approved or already sent.
FREE = """m.removed_at IS NULL AND m.data IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM desk_fb_attempts a WHERE a.retry_key=m.attempt_key AND a.status IN ('approved','sent'))"""


def describe(row):
    return dict(id=row['id'], name=row['filename'], type='video' if row['content_type'] == 'video/mp4' else 'image')


def listing(store, tid):
    return [describe(r) for r in store.all(
        f'SELECT id,filename,content_type FROM desk_fb_media m WHERE thread_id=%s AND {FREE} ORDER BY uploaded_at,id', (tid,))]


def attached(store, key):
    return [describe(r) for r in store.all(
        'SELECT id,filename,content_type FROM desk_fb_media WHERE attempt_key=%s AND removed_at IS NULL ORDER BY uploaded_at,id', (key,))]


def upload(store, staff, tid, file):
    require_reviewer(staff)
    mime = getattr(file, 'mimetype', '')
    if mime not in TYPES:
        raise ActionError('只接受 JPEG、PNG 圖片或 MP4 影片', 400)
    limit = VIDEO_LIMIT if mime == 'video/mp4' else IMAGE_LIMIT
    data = file.stream.read(limit + 1)
    if not data:
        raise ActionError('檔案內容是空的', 400)
    if len(data) > limit:
        raise ActionError('影片超過 25MB，請改貼 YouTube 連結' if mime == 'video/mp4' else '圖片超過 10MB，請先壓縮', 400)
    if mime == 'video/mp4':
        if not mp4_valid(data):
            raise ActionError('影片不是有效的 MP4 檔案', 400)
    else:
        image_info(data, mime)
    name = (file.filename or '附件').replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if c.isprintable())[:120] or '附件'
    with store.cursor() as cur:
        cur.execute('SELECT channel,status FROM desk_fb_threads WHERE id=%s FOR UPDATE', (tid if isinstance(tid, str) else '',))
        c = cur.fetchone()
        if not c:
            raise ActionError('找不到對話', 404)
        if c['channel'].endswith('_comment'):
            raise ActionError('公開留言不能附檔，請改貼連結', 400)
        if c['status'] not in ('waiting', 'drafting', 'ready', 'blocked', 'error'):
            raise ActionError('這則已核准或已處理，請重新載入', 409)
        cur.execute("UPDATE desk_fb_media SET data=NULL WHERE data IS NOT NULL AND uploaded_at<NOW()-INTERVAL '30 days'")
        cur.execute(f'SELECT COUNT(*) AS n FROM desk_fb_media m WHERE thread_id=%s AND {FREE}', (tid,))
        if cur.fetchone()['n'] >= PER_THREAD:
            raise ActionError('一則回覆最多附 3 個檔案，請先移除不用的', 400)
        cur.execute('SELECT COALESCE(SUM(size),0) AS total FROM desk_fb_media WHERE data IS NOT NULL')
        if cur.fetchone()['total'] + len(data) > CAPACITY:
            raise ActionError('附件空間已滿，請改貼連結或通知管理者', 507)
        token = secrets.token_hex(16)
        cur.execute("""INSERT INTO desk_fb_media(id,thread_id,data,content_type,size,filename,uploaded_by)
          VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,filename,content_type""", (token, tid, data, mime, len(data), name, staff['id']))
        return describe(cur.fetchone())


def remove(store, staff, media_id, tid):
    require_reviewer(staff)
    if not isinstance(media_id, str) or not TOKEN.fullmatch(media_id):
        raise ActionError('附件編號不正確', 400)
    done = store.execute(f'UPDATE desk_fb_media m SET removed_at=NOW(),data=NULL WHERE id=%s AND thread_id=%s AND {FREE}', (media_id, tid))
    if not done:
        raise ActionError('附件已核准送出或不屬於這則對話，無法移除', 409)


def bind(cur, tid, key, ids, public):
    """Inside the approval transaction: tie exactly the files Wayne saw to this approval."""
    if ids in (None, []):
        return
    if (not isinstance(ids, list) or len(ids) > PER_THREAD or len(set(ids)) != len(ids)
            or not all(isinstance(i, str) and TOKEN.fullmatch(i) for i in ids)):
        raise ActionError('附件清單不正確，請重新載入', 400)
    if public:
        raise ActionError('公開留言不能附檔', 400)
    cur.execute(f'UPDATE desk_fb_media m SET attempt_key=%s WHERE thread_id=%s AND id=ANY(%s) AND {FREE}', (key, tid, ids))
    if cur.rowcount != len(ids):
        raise ActionError('附件已變動，請重新載入後再核准', 409)


def read(store, media_id):
    if not isinstance(media_id, str) or not TOKEN.fullmatch(media_id):
        return None
    return store.one('SELECT data,content_type FROM desk_fb_media WHERE id=%s AND data IS NOT NULL AND removed_at IS NULL', (media_id,))


def export(store, attempt_id, folder):
    """Write an approved reply's files next to the browser so it can attach them; returns their paths in order."""
    rows = store.all("""SELECT id,data,content_type FROM desk_fb_media
      WHERE attempt_key=%s AND removed_at IS NULL AND data IS NOT NULL ORDER BY uploaded_at,id""", (attempt_id,))
    paths = []
    if rows:
        target = Path(folder) / attempt_id
        target.mkdir(parents=True, exist_ok=True)
        for n, r in enumerate(rows, 1):
            path = target / f"{n}_{r['id'][:8]}{TYPES[r['content_type']]}"
            path.write_bytes(bytes(r['data']))
            paths.append(str(path))
    return paths
