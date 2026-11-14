"""Reference material shown above a draft and in the 查資料 panel (Wayne 11/14, agreed by ChatGPT and Gemini):
our saved replies, and what colleagues answered before (desk_qa: names, phones and plates already masked, 2026 onward)."""
import time
from .engine import topk
from .worker import local_time

_QA = {}


def history_rows(store):
    # ~16k rows; read at most every 10 minutes instead of on every opened conversation
    hit = _QA.get('rows')
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    rows = store.all('SELECT source_id,asked_at,question,answer FROM desk_qa WHERE length(answer) >= 8 ORDER BY asked_at DESC')
    _QA['rows'] = (time.monotonic(), rows)
    return rows


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
        return [dict(kind='canned', title=c['title'], text=c['text'][:1500])
                for c in topk(query, canned_rows(store), lambda x: x['title'] + ' ' + x['text'][:300], k)]
    return [dict(kind='history', title='同事回答', question=r['question'][:300], text=r['answer'][:1500], date=local_time(r['asked_at'])[:10].replace('-', '/'))
            for r in topk(query, history_rows(store), lambda x: x['question'], k)]


def for_thread(store, messages, context=''):
    """What the customer asked most recently, matched against saved replies and past answers (2 each)."""
    said = [m['text'] for m in messages if m['role'] == 'user' and (m.get('text') or '').strip()][-2:]
    query = ' '.join(said)
    canned = search(store, query + ' ' + (context or '')[:100], 'canned', 2)
    return dict(query=said[-1][:60] if said else '', canned=canned, history=search(store, query, 'history', 2))
