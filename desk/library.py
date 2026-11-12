"""Wayne's common pictures (Google Drive「常用圖片」): only the customer-facing cards from his index are imported.

The AI may suggest up to two of them for a private reply; Wayne attaches one with a click and still approves the send.
Internal cards (dealer prices, stamps, bank details for non-buyers…) are never imported, so they can never be suggested.
"""
from .errors import ActionError
from .fb_media import add
from .knowledge import require_reviewer
from .media import TOKEN

MAX_SUGGEST = 2


def listing(store):
    return [dict(id=r['id'], name=r['filename'], topic=r['topic'], use_when=r['use_when'])
            for r in store.all('SELECT id,filename,topic,use_when FROM desk_media_library WHERE active ORDER BY topic,filename')]


def catalog(store):
    """Numbered list for the drafting prompt; numbers are easier for the model than file ids."""
    rows = store.all('SELECT id,filename,topic,summary,use_when FROM desk_media_library WHERE active ORDER BY topic,filename')
    return [(n, r) for n, r in enumerate(rows, 1)]


def suggestions(numbers, numbered):
    """Turn the model's picks into library cards; unknown numbers are dropped, never guessed."""
    by = {n: r for n, r in numbered}
    out = []
    for x in numbers if isinstance(numbers, list) else []:
        try:
            r = by.get(int(x))
        except (TypeError, ValueError):
            continue
        if r and all(o['id'] != r['id'] for o in out):
            out.append(dict(id=r['id'], name=r['filename']))
    return out[:MAX_SUGGEST]


def read(store, lib_id):
    if not isinstance(lib_id, str) or not TOKEN.fullmatch(lib_id):
        return None
    return store.one('SELECT data,content_type FROM desk_media_library WHERE id=%s AND active', (lib_id,))


def attach(store, staff, tid, lib_id):
    require_reviewer(staff)
    if not isinstance(lib_id, str) or not TOKEN.fullmatch(lib_id):
        raise ActionError('圖片編號不正確', 400)
    row = store.one('SELECT filename,content_type,data FROM desk_media_library WHERE id=%s AND active', (lib_id,))
    if not row:
        raise ActionError('找不到這張常用圖片', 404)
    if store.one("""SELECT 1 FROM desk_fb_media WHERE thread_id=%s AND filename=%s AND removed_at IS NULL AND attempt_key IS NULL""",
                 (tid if isinstance(tid, str) else '', row['filename'])):
        raise ActionError('這張已經附上了', 409)
    return add(store, staff, tid, bytes(row['data']), row['content_type'], row['filename'])
