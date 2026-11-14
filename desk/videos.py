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
    rows = [r for r in store.all('SELECT id,title FROM desk_videos ORDER BY added_at DESC,id')
            if any(squash(n) == squash(model) for n in cars_in(r['title'], names))]  # exact: a Corolla is not a Corolla Cross

    def about(r):  # 「電動尾門」 and 「尾門」 are the same product in a title
        t = squash(r['title'])
        return any(w in t or (w.endswith('尾門') and '尾門' in t) for w in wanted)
    rows.sort(key=lambda r: not about(r))
    return [dict(標題=r['title'], 網址=link(r['id'])) for r in rows[:6]]  # never a short link: see direct()


MAKES = {'TOYOTA': '豐田|TOYOTA', 'LEXUS': '凌志|雷克薩斯|LEXUS', 'HONDA': '本田|HONDA', 'NISSAN': '日產|NISSAN', 'MAZDA': '馬自達|MAZDA',
         'MITSUBISHI': '三菱|MITSUBISHI', 'SUBARU': '速霸陸|SUBARU', 'SUZUKI': '鈴木|SUZUKI', 'HYUNDAI': '現代汽車|HYUNDAI', 'KIA': '起亞|KIA',
         'FORD': '福特|FORD', 'VOLKSWAGEN': '福斯|VOLKSWAGEN|VW', 'SKODA': '斯柯達|SKODA', 'AUDI': '奧迪|AUDI', 'BENZ': '賓士|BENZ|MERCEDES',
         'BMW': '寶馬|BMW', 'PORSCHE': '保時捷|PORSCHE', 'VOLVO': '富豪|VOLVO', 'LUXGEN': '納智捷|LUXGEN', 'TESLA': '特斯拉|TESLA', 'MG': r'\bMG\b'}


def other_make(title, brand):
    """The make a title is about when it is not the customer's (「福斯全車系環景改裝」 for a Lexus owner), else ''.
    Says nothing when we do not know the customer's make, or the title names theirs too."""
    mine = (brand or '').upper().replace('MERCEDES-BENZ', 'BENZ').replace('MERCEDES', 'BENZ')
    if mine not in MAKES:
        return ''
    found = [make for make, pattern in MAKES.items() if re.search(pattern, title or '', re.I)]
    return '' if not found or mine in found else found[0]


def direct(store, text):
    """Short links that lead to a YouTube video become the video's own youtu.be link (Wayne 11/14: on a phone,
    reurl.cc shows a full-screen advert before a YouTube video; links to our website are not affected)."""
    from .links import URL
    found = {}
    for url in dict.fromkeys(u.rstrip('.,;:!?') for u in URL.findall(text or '')):
        if not re.match(r'https?://(www\.)?reurl\.cc/', url):
            continue
        row = store.one("SELECT target FROM desk_links WHERE url=%s AND state='ok'", (url,))
        m = row and re.search(r'(?:youtu\.be/|shorts/|[?&]v=)(' + VIDEO_ID.pattern + ')', row['target'] or '')
        if m:
            found[url] = link(m.group(1))
    for short, video in found.items():
        text = re.sub(re.escape(short) + r'(?![A-Za-z0-9])', video, text)
    return text, list(found.values())


def titles(store):
    """url -> title for every channel video, in the form drafts link them."""
    return {link(r['id']): 'YouTube：' + r['title'] for r in store.all('SELECT id,title FROM desk_videos')}
