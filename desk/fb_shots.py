"""The conversation exactly as Meta's inbox showed it: one screenshot per conversation, the latest one wins.

Wayne (11/12) wants to compare the desk's transcription with the real screen before approving.
Only the conversation column is captured (not the list of other customers); shots are deleted after 30 days.
"""
from .media import image_info

LIMIT = 4 * 1024 * 1024


def sniff(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    return None


def save(store, tid, data):
    """Returns True when stored; a bad or oversized file is skipped, never fatal for the run."""
    mime = sniff(data or b'')
    if not mime or len(data) > LIMIT:
        return False
    try:
        image_info(data, mime)
    except Exception:
        return False
    with store.cursor() as cur:
        cur.execute("DELETE FROM desk_fb_shots WHERE taken_at < NOW() - INTERVAL '30 days'")
        cur.execute('SELECT 1 FROM desk_fb_threads WHERE id=%s', (tid,))
        if not cur.fetchone():
            return False
        cur.execute("""INSERT INTO desk_fb_shots(thread_id,data,content_type,size,taken_at) VALUES(%s,%s,%s,%s,NOW())
          ON CONFLICT(thread_id) DO UPDATE SET data=EXCLUDED.data,content_type=EXCLUDED.content_type,size=EXCLUDED.size,taken_at=NOW()""",
                    (tid, data, mime, len(data)))
    return True


def read(store, tid):
    if not isinstance(tid, str):
        return None
    return store.one("SELECT data,content_type FROM desk_fb_shots WHERE thread_id=%s AND taken_at >= NOW() - INTERVAL '30 days'", (tid,))
