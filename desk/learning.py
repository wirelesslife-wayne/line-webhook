"""Learn from replies typed directly in Meta (FB/IG) or LINE, keeping only the ones better than what the desk already has.

Wayne 11/12: 「在 FB 或 IG 甚至 LINE 打過的回覆，工作台可以順便學習嗎？自己過濾有沒有比原本的紀錄還好？沒有的話就不用記錄」.
Every candidate is judged once, in batches, against the closest examples the desk already uses. Only 'better' keeps its
text and becomes a drafting example; the rest leave just a marker so they are never judged (or paid for) again.
"""
import hashlib
import json
from .fb import answers, squash

BATCH = 8
LINE_DAYS = 7          # LINE colleagues' replies arrive daily (WL-DeskQaSync); older ones were already history
MIN_ANSWER = 20        # 「好的」「稍等」 teach nothing
SCAN = 400


def digest(text):
    return hashlib.sha256(squash(text).encode('utf-8')).hexdigest()


def candidates(store, limit=BATCH):
    judged = {r['digest'] for r in store.all('SELECT digest FROM desk_learned')}
    sent = {squash(r['content']) for r in store.all("SELECT content FROM desk_fb_attempts WHERE status='sent'")}
    out, seen = [], set()

    def add(source, question, answer):
        d = digest(answer)
        if d in judged or d in seen or len(squash(answer)) < MIN_ANSWER or not squash(question):
            return
        seen.add(d)
        out.append(dict(digest=d, source=source, question=question[-600:], answer=answer[:1500]))

    # FB / IG: a person's reply typed in Meta, with what the customer said since the previous real reply.
    rows = store.all("""SELECT e.thread_id,e.role,e.content,e.note,t.channel FROM desk_fb_events e
      JOIN desk_fb_threads t ON t.id=e.thread_id ORDER BY e.thread_id,e.id""")
    thread, asked = None, []
    for r in rows:
        if r['thread_id'] != thread:
            thread, asked = r['thread_id'], []
        if r['role'] == 'user':
            if r['content'].strip():
                asked.append(r['content'].strip())
        elif answers(r['role'], r['note']):
            if asked and squash(r['content']) not in sent:  # the desk's own sends were already approved by Wayne
                add('ig' if r['channel'].startswith('ig') else 'fb', ' / '.join(asked), r['content'])
            asked = []
    # LINE: colleagues' replies synced from the LINE OA (newest first).
    for r in store.all(f"""SELECT question,answer FROM desk_qa WHERE asked_at > NOW() - INTERVAL '{LINE_DAYS} days'
      AND length(answer) >= {MIN_ANSWER} ORDER BY asked_at DESC LIMIT {SCAN}"""):
        add('line', r['question'], r['answer'])
    return out[:limit]


def kept(store, limit=400):
    return store.all("SELECT source,question,answer FROM desk_learned WHERE verdict='better' ORDER BY judged_at DESC LIMIT %s", (limit,))


CRITERIA = (
    '你是無限科技（汽車影音安裝）的客服主管。下面每一題是同事或老闆直接在 FB／IG／LINE 回給客人的話，'
    '另附工作台目前對類似問題會參考的範本。判斷這則回覆值不值得收進範本：\n'
    '收（better）：實質回答了客人的問題，資訊具體正確，而且比現有範本更完整、更清楚、更好懂，'
    '或是現有範本沒涵蓋的問題且回得好。\n'
    '不收（no）：只說稍等／確認中／請加 LINE／請來電；罐頭群發或廣告文；只有表情或客套；答非所問；'
    '價格或規格跟範本衝突、看起來有錯；寫出客人電話、車牌等個資；跟現有範本差不多或比較差。\n'
    '拿不準就不收。回傳 JSON：{"results":[{"n":題號,"verdict":"better"或"no","reason":"20 字內原因"}]}'
)


def judge_once(store, engine_factory=None):
    """Judge one batch. Returns how many were judged; provider errors (budget, outage) leave them for the next run."""
    batch = candidates(store)
    if not batch:
        return 0
    from .engine import Engine
    engine = (engine_factory or Engine)(store)
    items = [dict(n=i + 1, 來源=c['source'], 客人=c['question'], 回覆=c['answer'], 現有範本=engine.approved(c['question'], 2))
             for i, c in enumerate(batch)]
    out = engine.provider([{'role': 'system', 'content': CRITERIA},
                           {'role': 'user', 'content': json.dumps(items, ensure_ascii=False)}])
    verdicts = {}
    for r in (out or {}).get('results') or []:
        if isinstance(r, dict) and isinstance(r.get('n'), int):
            verdicts[r['n']] = r
    with store.cursor() as cur:
        for i, c in enumerate(batch):
            v = verdicts.get(i + 1)
            if not v:
                continue  # unanswered: judge it again next time
            better = v.get('verdict') == 'better'
            reason = str(v.get('reason') or '')[:200]
            cur.execute("""INSERT INTO desk_learned(digest,source,question,answer,verdict,reason)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(digest) DO NOTHING""",
                        (c['digest'], c['source'], c['question'] if better else '', c['answer'] if better else '',
                         'better' if better else 'no', reason))
    return len(batch)
