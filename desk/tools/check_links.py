"""Look up where every link in the knowledge base really goes, so drafts can be checked without waiting for the network.

Usage: python -m desk.tools.check_links
Reads public pages only. Run again after the canned replies change.
"""
from .common import store
from ..links import find, inspect, about
from ..db import now

FILES = ['canned_v2.json', 'faq_v2.json', 'line_saved_replies.json', '口吻規則.md']


def run():
    db = store()
    kb = db.kb(FILES)
    urls = []
    for name in FILES:
        value = kb.get(name) or []
        texts = [value] if isinstance(value, str) else [x.get('text', '') + ' ' + x.get('answer', '') for x in value if isinstance(x, dict)]
        for text in texts:
            for line in text.splitlines():  # find() stops at 10 links; a canned topic can hold more
                urls += [u for u in find(line) if u not in urls]
    have = {r['url'] for r in db.all("SELECT url FROM desk_links WHERE state='ok' AND title<>''")}
    counts = dict(known=0, ok=0, broken=0, unchecked=0, no_product=0)
    for url in urls:
        if url in have:
            counts['known'] += 1
            continue
        target, title, state = inspect(url)
        db.execute("""INSERT INTO desk_links(url,target,title,state,checked_at) VALUES(%s,%s,%s,%s,%s)
          ON CONFLICT(url) DO UPDATE SET target=EXCLUDED.target,title=EXCLUDED.title,state=EXCLUDED.state,checked_at=EXCLUDED.checked_at""",
                   (url, target, title, state, now()))
        counts[state] += 1
        if state != 'ok':
            print(state, url)
        elif not about(title):
            counts['no_product'] += 1
            print('看不出產品', url, '→', title[:50])
    print('連結', len(urls), '個：', counts)


if __name__ == '__main__':
    run()
