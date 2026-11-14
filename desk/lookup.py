"""Reference material shown above a draft and in the 查資料 panel (Wayne 11/14, agreed by ChatGPT and Gemini):
our saved replies, and what colleagues answered before (desk_qa: names, phones and plates already masked, 2026 onward)."""
import math
import re
import time
from collections import Counter
from .catalog import kinds
from .worker import local_time

_QA = {}
# Greetings and filler outweighed the real question (11/14, a B42 memory-card question got CarPlay answers)
FILLER = re.compile(r'你好|您好|哈囉|請問|想問|一下|謝謝|感謝|麻煩|不好意思|是什麼|什麼|怎麼|可以|需要|的時候|但是|因為|所以|嗎|呢|喔|哦|啊|吧')
MIN_WEIGHT = 1.4  # shared rare pairs must outweigh 1.4 "never seen" pairs (≈13.5 for the 16k past answers; 1.25 let 記憶＋影片 pull in an unrelated saved reply)
MODEL = re.compile(r'[a-z0-9]')  # pairs from model names and numbers (b42, rav4)


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
        # 11/15 ChatGPT and Gemini: the same model name alone is not a match (a B42 memory-card question got
        # B42 quotes and comparisons); the words about the problem itself must match too
        # short questions are mostly a model name (「B42 多少錢」): ask for a share of their few topic words instead
        need = min(floor / 2, 0.4 * sum(self.idf.get(g, self.unseen) for g in q if not MODEL.search(g)))
        weight, topic, shared, on_topic = Counter(), Counter(), Counter(), Counter()
        for g in q:
            model = bool(MODEL.search(g))
            for i in self.where.get(g, ()):
                weight[i] += self.idf[g]
                shared[i] += 1
                if not model:
                    topic[i] += self.idf[g]
                    on_topic[i] += 1
        hits = sorted(((w / total / (1 + math.log(1 + self.size[i] / 40)), i) for i, w in weight.items()
                       if shared[i] >= 2 and w >= floor and on_topic[i] and topic[i] >= need), reverse=True)
        return [i for _, i in hits]


def history_rows(store):
    # ~16k rows; read and index at most every 10 minutes instead of on every opened conversation
    hit = _QA.get('rows')
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1], hit[2]
    rows = store.all('SELECT source_id,asked_at,question,answer FROM desk_qa WHERE length(answer) >= 8 ORDER BY asked_at DESC')
    hit = _QA['rows'] = (time.monotonic(), rows, Index([r['question'] for r in rows]))  # one assignment: other requests never see rows without their index
    return rows, hit[2]


# also answers that only handle booking, not the question (11/15 ChatGPT and Gemini: 喇叭破音 → 「今天預約額滿」)
AUTO_REPLY = re.compile(r'請先留言|請您先留言|請點選您感興趣|客服回覆時間|感謝您預約我們的服務|工作時間是週一|預約[^，。]{0,6}額滿')


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


# Saved replies written for one car brand or model (Grok 11/15: a Tesla sunshade quote showed on 15 of 45
# drafts because 「電動」 matched 電動尾門): only when the customer names that car.
CAR_ONLY = [(r'tesla|特斯拉', r'tesla|特斯拉|model ?[3ysx]'), (r'carnival|嘉年華', r'carnival|嘉年華'),
            (r'j ?space', r'j ?space'), (r'tucson', r'tucson'), (r'luxgen|納智捷', r'luxgen|納智捷|[mnu]7'),
            (r'hszs|\bmg\b', r'\bmg\b|\bhs\b|\bzs\b'), (r'focus|kuga', r'focus|kuga'), (r'cr-?v ?6', r'cr-?v'),
            (r'e60|bmw', r'bmw|寶馬|e60')]


def same_product(about, text):
    """A reference about another product is never shown (Grok 11/15: a 418 warranty question got the
    power-tailgate warranty answer). Nothing is dropped when either side names no product."""
    want, have = kinds(about), kinds(text)
    return not want or not have or bool(want & have)


def fits_car(about, title):
    return all(re.search(car, about, re.I) for only, car in CAR_ONLY if re.search(only, title, re.I))


def search(store, query, kind, k, about=None):
    """about: everything known about what the customer wants (their words and the ad), used for the
    product and car checks; the typed query alone in the 查資料 panel."""
    query = (query or '').strip()[:300]
    if not query:
        return []
    about = query if about is None else about
    if kind == 'canned':
        rows = canned_rows(store)
        order = [i for i in Index([c['title'] + ' ' + c['text'][:300] for c in rows]).top(query)
                 if fits_car(about, rows[i]['title']) and same_product(about, rows[i]['title'])]
        return [dict(kind='canned', title=c['title'], text=c['text'][:1500]) for c in _pick(rows, order, k, lambda c: c['text'])]
    rows, index = history_rows(store)
    order = [i for i in index.top(query) if same_product(about, rows[i]['question'] + ' ' + rows[i]['answer'][:200])]
    return [dict(kind='history', title='同事回答', question=r['question'][:300], text=r['answer'][:1500], date=local_time(r['asked_at'])[:10].replace('-', '/'))
            for r in _pick(rows, order, k, lambda r: r['answer'])]


def for_thread(store, messages, context=''):
    """What the customer asked most recently, matched against saved replies and past answers (2 each).
    Messages like 「好 謝謝」 are skipped so the last real question is used."""
    said = [m['text'] for m in messages if m['role'] == 'user' and len(grams(m.get('text'))) >= 3][-2:]
    query = ' '.join(said)
    about = query + ' ' + (context or '')[:300]
    canned = search(store, query + ' ' + (context or '')[:100], 'canned', 2, about)
    return dict(query=said[-1][:60] if said else '', canned=canned, history=search(store, query, 'history', 2, about))
