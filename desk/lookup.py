"""Reference material shown above a draft and in the 查資料 panel (Wayne 11/14, agreed by ChatGPT and Gemini):
our saved replies, and what colleagues answered before (desk_qa: names, phones and plates already masked, 2026 onward)."""
import math
import re
import time
from collections import Counter
from .worker import local_time

_QA = {}
# Greetings and filler outweighed the real question (11/14, a B42 memory-card question got CarPlay answers)
FILLER = re.compile(r'你好|您好|哈囉|請問|想問|一下|謝謝|感謝|麻煩|不好意思|是什麼|什麼|怎麼|可以|需要|的時候|但是|因為|所以|嗎|呢|喔|哦|啊|吧')
MIN_WEIGHT = 1.25  # shared rare pairs must outweigh 1.25 "never seen" pairs (≈12 for the 16k past answers)


def grams(text):
    t = FILLER.sub('', re.sub(r'[\W_]+', '', (text or '').lower()))
    return {t[i:i + 2] for i in range(len(t) - 1)}


class Index:
    """Rare shared pairs of characters (a model name, 記憶卡, 格式化) count for more than common ones."""

    def __init__(self, docs):
        self.size, self.where = [], {}
        for i, d in enumerate(docs):
            g = grams(d)
            self.size.append(len(g))
            for x in g:
                self.where.setdefault(x, []).append(i)
        n = len(docs) + 1
        self.idf = {g: math.log(n / (len(ids) + 1)) for g, ids in self.where.items()}
        self.unseen = math.log(n)

    def top(self, query):
        # rarity means nothing in a handful of rows (tests, a new install): two shared pairs are enough there
        q, floor = grams(query), MIN_WEIGHT * self.unseen if len(self.size) >= 20 else 0
        total = sum(self.idf.get(g, self.unseen) for g in q) or 1
        weight, shared = Counter(), Counter()
        for g in q:
            for i in self.where.get(g, ()):
                weight[i] += self.idf[g]
                shared[i] += 1
        hits = sorted(((w / total / (1 + math.log(1 + self.size[i] / 40)), i) for i, w in weight.items()
                       if shared[i] >= 2 and w >= floor), reverse=True)
        return [i for _, i in hits]


def history_rows(store):
    # ~16k rows; read and index at most every 10 minutes instead of on every opened conversation
    hit = _QA.get('rows')
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    rows = store.all('SELECT source_id,asked_at,question,answer FROM desk_qa WHERE length(answer) >= 8 ORDER BY asked_at DESC')
    _QA['rows'] = (time.monotonic(), rows)
    _QA['index'] = Index([r['question'] for r in rows])
    return rows


AUTO_REPLY = re.compile(r'請先留言|請您先留言|請點選您感興趣|客服回覆時間|感謝您預約我們的服務|工作時間是週一')


def _pick(rows, order, k, key):
    """Best first, the same answer only once, and never an automatic reply."""
    out, seen = [], set()
    for i in order:
        sig = re.sub(r'\W+', '', key(rows[i]))[:60]
        if sig not in seen and not AUTO_REPLY.search(key(rows[i])[:200]):
            seen.add(sig)
            out.append(rows[i])
        if len(out) == k:
            break
    return out


def canned_rows(store):
    kb = store.kb(['canned_v2.json', 'line_saved_replies.json'])
    out = [dict(title=c['topic'], text=c['text']) for c in kb.get('canned_v2.json') or [] if c.get('text')]
    out += [dict(title='LINE 預設訊息：' + c['topic'], text=c['text'])
            for c in kb.get('line_saved_replies.json') or [] if c.get('text') and not c.get('skip')]
    return out


def search(store, query, kind, k):
    query = (query or '').strip()[:300]
    if not query:
        return []
    if kind == 'canned':
        rows = canned_rows(store)
        order = Index([c['title'] + ' ' + c['text'][:300] for c in rows]).top(query)
        return [dict(kind='canned', title=c['title'], text=c['text'][:1500]) for c in _pick(rows, order, k, lambda c: c['text'])]
    rows = history_rows(store)
    order = _QA['index'].top(query)
    return [dict(kind='history', title='同事回答', question=r['question'][:300], text=r['answer'][:1500], date=local_time(r['asked_at'])[:10].replace('-', '/'))
            for r in _pick(rows, order, k, lambda r: r['answer'])]


def for_thread(store, messages, context=''):
    """What the customer asked most recently, matched against saved replies and past answers (2 each).
    Messages like 「好 謝謝」 are skipped so the last real question is used."""
    said = [m['text'] for m in messages if m['role'] == 'user' and len(grams(m.get('text'))) >= 3][-2:]
    query = ' '.join(said)
    canned = search(store, query + ' ' + (context or '')[:100], 'canned', 2)
    return dict(query=said[-1][:60] if said else '', canned=canned, history=search(store, query, 'history', 2))
