"""Where does each link in a reply really go? Short links (reurl.cc) are opened and named before Wayne sends.

Results are cached for 12 hours. A link counts as broken only when the site clearly says so (404/410,
or a short link without a destination); network trouble is reported as "unchecked", never as broken.
"""
import html
import re
import requests

URL = re.compile(r'https?://[^\s<>"「」『』（）()，。、！？]+')
SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_links (
 url TEXT PRIMARY KEY, target TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL CHECK (state IN ('ok','broken','unchecked')), checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""
HEADERS = {'User-Agent': 'Mozilla/5.0 (link check for a customer-service desk)'}


def find(text):
    seen = []
    for url in URL.findall(text or ''):
        url = url.rstrip('.,;:!?')
        if url not in seen:
            seen.append(url)
    return seen[:10]


def title_of(page):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page, re.I) or \
        re.search(r'<title[^>]*>([^<]+)</title>', page, re.I)
    return html.unescape(m.group(1)).strip()[:200] if m else ''


def inspect(url, get=None):
    """Return (target, title, state) for one link."""
    get = get or (lambda u: requests.get(u, headers=HEADERS, timeout=(4, 6), allow_redirects=True))
    try:
        target = url
        if re.match(r'https?://(www\.)?reurl\.cc/', url):
            r = get(url)
            if r.status_code in (404, 410):
                return '', '', 'broken'
            m = re.search(r'id=["\']target["\'][^>]*value=["\']([^"\']+)', r.text) or \
                re.search(r'value=["\']([^"\']+)["\'][^>]*id=["\']target["\']', r.text)
            if not m:
                return '', title_of(r.text), 'broken' if r.status_code == 200 else 'unchecked'
            target = html.unescape(m.group(1))
        if re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/', target):
            r = get('https://www.youtube.com/oembed?format=json&url=' + requests.utils.quote(target, safe=''))
            if r.status_code in (400, 401, 403, 404):
                return target, '', 'broken'
            return target, 'YouTube：' + (r.json().get('title') or '')[:180], 'ok'
        r = get(target)
        if r.status_code in (404, 410):
            return target, '', 'broken'
        return target, title_of(r.text), 'ok' if r.status_code < 400 else 'unchecked'
    except Exception:
        return '', '', 'unchecked'


def check(store, text, get=None, budget=12.0):
    """budget: seconds of network time for this call; the web worker must answer well within 30 s."""
    import time
    from .db import now  # here, not at import time: db.py imports this module for its schema
    out, deadline = [], time.monotonic() + budget
    for url in find(text):
        row = store.one("SELECT * FROM desk_links WHERE url=%s AND checked_at > NOW()-INTERVAL '12 hours'", (url,))
        if not row or row['state'] == 'unchecked':
            if time.monotonic() > deadline:
                out.append(dict(url=url, target='', title='', state='unchecked'))
                continue
            target, title, state = inspect(url, get)
            store.execute("""INSERT INTO desk_links(url,target,title,state,checked_at) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(url) DO UPDATE SET target=EXCLUDED.target,title=EXCLUDED.title,state=EXCLUDED.state,checked_at=EXCLUDED.checked_at""",
                          (url, target, title, state, now()))
            row = dict(url=url, target=target, title=title, state=state)
        out.append(dict(url=row['url'], target=row['target'], title=row['title'], state=row['state']))
    return out
