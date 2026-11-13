"""Videos on our YouTube channel, so a draft can link the one that shows the customer's own car.

Wayne 11/13: his replies link the channel's video for that car (the Corolla Cross tailgate short); the website's
install-case pages are not to be linked for now (a customer has to click through several levels to see anything).
The full list is loaded from the office machine (desk.tools.upload_videos); the server adds new uploads from the
channel's public feed about once a day.
"""
import html
import re
import requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_videos (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""
FEED = 'https://www.youtube.com/feeds/videos.xml?channel_id=UCcOUUK9vW7Z_fOrgLM0QtRw'
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')


def link(video_id):
    return 'https://youtu.be/' + video_id


def save(store, rows):
    """rows: (id, title) pairs. Titles follow the channel; nothing is deleted here."""
    n = 0
    for vid, title in rows:
        title = (title or '').strip()[:300]
        if VIDEO_ID.fullmatch(vid or '') and title:
            n += store.execute("""INSERT INTO desk_videos(id,title) VALUES(%s,%s)
              ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title""", (vid, title))
    return n


def refresh_if_due(store, get=None):
    """New uploads from the public feed (latest 15), about once a day. A failed fetch changes nothing."""
    if store.one("SELECT 1 AS x FROM desk_marks WHERE name='videos_feed' AND at > NOW()-INTERVAL '1 day'"):
        return 0
    store.execute("""INSERT INTO desk_marks(name,value) VALUES('videos_feed','') ON CONFLICT(name) DO UPDATE SET at=NOW()""")
    get = get or (lambda u: requests.get(u, headers={'User-Agent': 'Mozilla/5.0'}, timeout=(4, 8)))
    try:
        r = get(FEED)
        if r.status_code != 200:
            return 0
        entries = re.findall(r'<yt:videoId>([^<]+)</yt:videoId>\s*<yt:channelId>[^<]*</yt:channelId>\s*<title>([^<]*)</title>', r.text)
        return save(store, [(vid, html.unescape(title)) for vid, title in entries])
    except Exception:
        return 0


def squash(s):
    """「CX-5(三代)」 and 「cx5」 are the same car name."""
    return re.sub(r'[\s\-_.／/·]', '', re.sub(r'[（(].*', '', s or '')).upper()


def cars_in(title, names):
    """Car models a title names, most specific only: 「Corolla Cross」 is not also a 「Corolla」 video.

    A name ending in a digit must not run on into more digits (CX-5 is not CX-50); one ending in a letter may be
    followed by digits (CRV5, NX350h) but not by more letters. Short all-letter names (IS, FIT, CITY, WISH) count only
    when the title writes them in capitals, so ordinary English words in a title are not cars.
    """
    title = title or ''
    found = []
    for name in names:
        key = squash(name)
        if len(key) < 2:
            continue
        pattern = r'(?<![A-Z0-9])' + r'[\s\-_.]*'.join(map(re.escape, key)) + (r'(?![A-Z0-9])' if key[-1].isdigit() else r'(?![A-Z])')
        if re.search(pattern, title if key.isalpha() and len(key) <= 4 else title.upper()):
            found.append(name)
    return [n for n in found if not any(squash(n) != squash(m) and squash(n) in squash(m) for m in found)]


def for_car(store, ex, names):
    """Channel videos whose title names this customer's car; the ones about the product they asked come first."""
    model = (ex or {}).get('model') or ''
    if not squash(model):
        return []
    names = list(set(names) | {model})
    wanted = [squash(p) for p in (ex.get('products') or []) if isinstance(p, str) and len(squash(p)) >= 2]
    # The short link Wayne already uses for a video, when there is one: the reply then looks like his own.
    short = {}
    for r in store.all("SELECT url,target FROM desk_links WHERE state='ok' AND target ~ 'youtu' ORDER BY checked_at"):
        m = re.search(r'(?:youtu\.be/|shorts/|v=)(' + VIDEO_ID.pattern + ')', r['target'])
        if m:
            short[m.group(1)] = r['url']
    rows = [r for r in store.all('SELECT id,title FROM desk_videos ORDER BY added_at DESC,id')
            if any(squash(n) == squash(model) for n in cars_in(r['title'], names))]  # exact: a Corolla is not a Corolla Cross

    def about(r):  # 「電動尾門」 and 「尾門」 are the same product in a title
        t = squash(r['title'])
        return any(w in t or (w.endswith('尾門') and '尾門' in t) for w in wanted)
    rows.sort(key=lambda r: not about(r))
    return [dict(標題=r['title'], 網址=short.get(r['id']) or link(r['id'])) for r in rows[:6]]


def titles(store):
    """url -> title for every channel video, in the form drafts link them."""
    return {link(r['id']): 'YouTube：' + r['title'] for r in store.all('SELECT id,title FROM desk_videos')}
