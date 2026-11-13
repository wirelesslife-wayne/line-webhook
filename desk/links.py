"""Where does each link in a reply really go? Short links (reurl.cc) are opened and named before Wayne sends.

audit() also says whether the words in front of a link match the page it opens (Wayne 11/14: a reply labelled
the tailgate link 「環景介紹」 and the surround-view link 「尾門介紹」; short links all look alike).

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


# What a label or a page is about. Only a clear contradiction counts: both sides name a product and share none.
FAMILIES = [
    ('電動尾門', r'尾門|撐桿|腳踢'),
    ('360 環景', r'環景|360'),
    ('電子後視鏡', r'後視鏡|後照鏡|四錄|雙錄|四路|WRM|S14|B42|418|R3D'),
    ('安卓主機', r'安卓(?!盒)|車機|主機|[THA] ?系列|[EDM]X ?系列|DS ?系列'),
    ('安卓盒', r'安卓盒|Z8|Z ?系列'),
    ('CarPlay', r'car ?play'),
    ('盲點偵測', r'盲點|BSD'),
    ('隔音', r'隔音|制震'),
    ('音響', r'音響|喇叭|重低音|擴大機|DSP'),
    ('後座螢幕', r'吸頂|後座|背掛'),
    ('氣氛燈', r'氣氛燈|氛圍燈'),
    ('遮陽簾', r'遮陽簾'),
    ('經銷門市', r'經銷|門市|據點|通路'),
]


def about(text):
    return [name for name, pattern in FAMILIES if re.search(pattern, text or '', re.I)]


def labels(text):
    """{url: the words written in front of it}: the same line, or the line above when the link stands alone."""
    out, above = {}, ''
    for line in (text or '').splitlines():
        found = URL.findall(line)
        if not found:
            if line.strip():  # a blank line between a label and its link is still the same pair
                above = '' if '[[' in line else re.sub(r'[\s：:]+$', '', line.strip())
            continue
        own = re.sub(r'^[\s👉・•\-]+|[\s：:]+$', '', line[:line.index(found[0])])
        for url in found:
            out.setdefault(url.rstrip('.,;:!?'), own or (above if len(above) <= 60 else ''))
        above = ''
    return out


def judge(text, found):
    """A verdict for each link already looked up: 'mismatch' when the words in front of it name one product and the page another.
    A link with no product in its label (「產品介紹：」「安裝影片：」) must be about something the reply talks about."""
    named = labels(text)
    rest = [n for n in about(URL.sub('', text or '')) if n != '經銷門市']  # shops are mentioned in almost every reply
    out = []
    for link in found:
        label = named.get(link['url'], '')
        goes = about(link['title'])
        says = about(label) or ([] if goes == ['經銷門市'] else rest)
        wrong = link['state'] == 'ok' and bool(says) and bool(goes) and not set(says) & set(goes)
        out.append(dict(link, label=label, says=says, goes=goes, verdict='mismatch' if wrong else link['state']))
    return out


def audit(store, text, get=None, budget=12.0):
    return judge(text, check(store, text, get, budget))


def complaint(link):
    front = f"「{link['label']}」" if link['label'] and about(link['label']) else '這則回覆講的是' + '、'.join(link['says']) + '，'
    return f"{front}的連結實際開到「{link['title'][:40]}」（{'、'.join(link['goes'])}）：{link['url']}".replace('，的連結', '，但這個連結')
