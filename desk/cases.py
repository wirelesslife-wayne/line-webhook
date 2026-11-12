"""Install-case pages on our website, one per car model, with the products actually installed on that model.

Wayne 11/12: a RAV4 customer was sent a Corolla Cross tailgate video as 「安裝實例」. A case link may only go to
a customer whose car is that model and whose product was installed on it; this index says which pages qualify.
Refreshed from the site's public WordPress API about once a day.
"""
import html
import re
import requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_cases (
 url TEXT PRIMARY KEY, brand TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
 products TEXT NOT NULL DEFAULT '', refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""
SITE = 'https://carandroid.com.tw/wp-json/wp/v2/pages'
HEADERS = {'User-Agent': 'Mozilla/5.0 (install-case index for a customer-service desk)'}


def norm(s):
    return re.sub(r'[\s\-_.／/·]', '', (s or '')).upper()


def text_of(rendered):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', rendered or '', flags=re.S)
    return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t))).strip()


def parse(page):
    """One page from the WordPress API -> dict(url, brand, model, title, products) or None."""
    title = html.unescape(page.get('title', {}).get('rendered', '')).strip()
    text = text_of(page.get('content', {}).get('rendered', ''))
    if '實裝產品' not in text and '歷史案例' not in text:
        return None
    model = re.sub(r'\s*安裝(案例目錄|案例與升級項目|案例|實例)\s*$', '', title).strip()
    crumb = re.search(r'(\S+)\s*→\s*' + re.escape(model.split(' ')[0]), text)
    lines = []
    for m in re.finditer(r'(實裝產品|歷史案例)：(.*?)(?=查看完整|實裝照片整理中|實裝產品|歷史案例|$)', text):
        years = re.findall(r'(\d{4}\s*[–\-~～]\s*(?:\d{4})?)', text[max(0, m.start() - 80):m.start()])
        items = m.group(2).strip()[:200]
        if items:
            lines.append((years[-1].replace(' ', '') + '：' if years else '') + items)
    return dict(url=page.get('link', ''), brand=crumb.group(1) if crumb else '', model=model, title=title,
                products='；'.join(lines)[:1500])


def refresh(store, get=None):
    get = get or (lambda u: requests.get(u, headers=HEADERS, timeout=30))
    found = []
    for n in range(1, 20):
        r = get(f'{SITE}?per_page=100&page={n}&_fields=slug,title,link,content')
        if r.status_code != 200:
            break
        pages = r.json()
        if not pages:
            break
        found += [x for x in (parse(p) for p in pages if 'install-cases' in p.get('slug', '')) if x and x['url']]
        if len(pages) < 100:
            break
    if not found:
        return 0  # site down or changed: keep the old index rather than wiping it
    with store.cursor() as cur:
        cur.execute('DELETE FROM desk_cases')
        for c in found:
            cur.execute("""INSERT INTO desk_cases(url,brand,model,title,products) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(url) DO NOTHING""", (c['url'], c['brand'], c['model'], c['title'], c['products']))
    return len(found)


def refresh_if_due(store, get=None):
    row = store.one('SELECT MAX(refreshed_at) AS at FROM desk_cases')
    stale = store.one("SELECT %s::timestamptz IS NULL OR %s::timestamptz < NOW()-INTERVAL '1 day' AS due",
                      (row['at'], row['at']))['due']
    return refresh(store, get) if stale else 0


def models(store):
    return [r['model'] for r in store.all('SELECT DISTINCT model FROM desk_cases')]


def same_car(customer_model, page_model):
    """Page models can list several (「BMW 5系列 / 6系列」「V60/S60」)."""
    c = norm(customer_model)
    if not c:
        return False
    parts = [norm(p) for p in re.split(r'[／/]', page_model) if norm(p)] or [norm(page_model)]
    return any(p == c or (len(p) >= 3 and len(c) >= 3 and (p in c or c in p)) for p in parts)


def for_car(store, ex):
    model = (ex or {}).get('model') or ''
    if not model:
        return []
    rows = store.all('SELECT url,brand,model,products FROM desk_cases')
    return [dict(車款=r['model'], 網址=r['url'], 實裝產品=r['products']) for r in rows if same_car(model, r['model'])][:3]
