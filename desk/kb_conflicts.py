"""知識庫衝突題 (Wayne 11/18 picked 「A 先做衝突題」).

The one-time knowledge consolidation read every past reply and found topics where we told customers different things
(an old price and a new one, two materials for the same job...). Each topic is one question here: Wayne taps the
option that is right today, writes his own, or leaves it for later. Every decided question goes into
every draft about that product as one of Wayne's own rules (for_draft below); a changed answer replaces the old one.

Stores, phones, prices and hours (Wayne 11/18 picked 「A」 after ChatGPT, Gemini and Claude all warned that a pick
could silently override the official dealer list): those questions show what the official data says next to the
options, an option that disagrees with it is marked, and choosing one asks him to confirm first. A confirmed
override wins in the drafts and is reported so the official file gets fixed; otherwise the official data wins.

The questions are loaded from the office PC (desk/tools/load_kb_conflicts.py) because the source file holds masked
customer replies and must not go into this public repository.
"""
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_kb_conflicts (
 id BIGSERIAL PRIMARY KEY, digest TEXT UNIQUE NOT NULL, grp TEXT NOT NULL, topic TEXT NOT NULL,
 options JSONB NOT NULL, suggest TEXT NOT NULL DEFAULT '', rank INT NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','later')),
 choice INT, answer TEXT NOT NULL DEFAULT '', decided_by BIGINT REFERENCES desk_users(id), decided_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
ALTER TABLE desk_kb_conflicts ADD COLUMN IF NOT EXISTS override BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE desk_kb_conflicts ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';
"""
# Imported after SCHEMA: desk.db reads SCHEMA from here while it is still importing (knowledge -> service -> db).
from .errors import ActionError  # noqa: E402
from .knowledge import require_reviewer  # noqa: E402
from .worker import local_time  # noqa: E402
from . import dealers  # noqa: E402
PAGE = 10
TABS = ('open', 'later', 'decided')
PAST = re.compile(r'(?:li|wa|di|fa)\d+')  # a past reply's id from the consolidation; shown only as a count


# Gemini and ChatGPT 11/18: the consolidation's own words (官方facts, 核准範例...) mean nothing to Wayne
PLAIN = (('官方facts', '公司正式規則'), ('facts', '正式規則'), ('核准範例', '你核准過的回覆'), ('產品知識檔', '產品資料'),
         ('canned', '制式回覆'), ('ad_auto', '廣告自動回覆'), ('edit_rules', '你的回覆規則'), ('合作門市', '經銷商門市'),
         ('老闆', '你'))


def plain(text):
    text = str(text or '')
    for a, b in PLAIN:
        text = text.replace(a, b)
    return text


# ---------- official data next to fact questions ----------
OFFICIAL = ['經銷商清單.txt', 'canned_v2.json', 'faq_v2.json', 'install_hours.json']
REPORT_KEY = {'dealer': 'dealers', 'price': 'canned', 'hours': 'hours'}
NUMBER = re.compile(r'0\d{1,3}-?\d{3,4}-?\d{3,4}|\d{1,3}(?:,\d{3})+|\d{4,6}')
SUFFIX = re.compile(r'(汽車音響|汽車影音|汽車精品|汽車百貨|汽車|音響|影音|車業|有限公司|企業社|工作室|精品|館|店)$')


def kind(topic, text=''):
    """Which official data a question is about. A store question must name a county in its own title (「台中現行經銷商門市名單」),
    otherwise 「颱風營業」「到府施工」 were taken for store lists."""
    if dealers.county_of(topic) and re.search(r'門市|經銷|據點|地址|電話|分店|店', topic):
        return 'dealer'
    if re.search(r'價|費|元|加收|折|分期|刷卡|\$|\d,\d{3}', topic + ' ' + text):
        return 'price'
    if re.search(r'工時|小時|幾天|多久', topic):
        return 'hours'
    return ''


def official(kb, what, text, topic=''):
    """What the official data says about this question: [{title, text}], at most 3 pieces."""
    from .engine import topk
    if what == 'dealer':
        county = dealers.county_of(topic)
        rows = [s for s in dealers.stores(kb.get('經銷商清單.txt') or '') if county and s['county'] == county]
        return [dict(title=f'正式經銷商清單（{county}）', text='\n'.join(f"{s['name']}｜{s['address']}｜{s['phone']}" for s in rows)
                     or f'正式清單裡沒有{county}的門市')] if county else []
    if what == 'price':
        pieces = []
        for c in kb.get('canned_v2.json') or []:
            for para in re.split(r'\n\s*\n', c.get('text', '')):
                if NUMBER.search(para):
                    pieces.append(dict(title='制式回覆：' + c.get('topic', ''), text=para.strip()[:400]))
        pieces += [dict(title='常見問答：' + f.get('q', ''), text=f.get('a', '')[:400]) for f in kb.get('faq_v2.json') or []]
        return topk(text, pieces, lambda p: p['title'] + p['text'], 2)
    if what == 'hours':
        items = (kb.get('install_hours.json') or {}).get('items') or []
        return [dict(title='正式工時表', text='\n'.join(f"{i.get('item', '')}：{i.get('hours', '')}" for i in
                                                     topk(text, items, lambda i: str(i.get('item', '')), 4)))] if items else []
    return []


def digits(s):
    return re.sub(r'\D', '', s)


def corpus(kb):
    """Every number anywhere in the official data (price table text, canned replies, FAQ, hours, stores, Wayne's 11/15 facts).
    A number is only called different when it appears nowhere at all: checking against the two pieces shown next to the
    question flagged the VIP prices and every one-off quote."""
    from .engine import FACTS
    import json
    blob = json.dumps([kb.get(n) for n in OFFICIAL], ensure_ascii=False) + FACTS
    return {digits(n) for n in NUMBER.findall(blob)}


def differs(what, said, pieces, known=frozenset()):
    """Why this wording disagrees with the official data (empty when nothing we can check disagrees)."""
    if not what or not said:
        return []
    ref = '\n'.join(p['text'] for p in pieces)
    known = set(known) | {digits(n) for n in NUMBER.findall(ref)}
    out = []
    parts = [int(digits(n)) for n in NUMBER.findall(said) if digits(n) in known and not n.startswith('0')]
    for n in NUMBER.findall(said):
        d = digits(n)
        if len(d) < 4 or re.fullmatch(r'(19|20)\d\d', d) or d in known:
            continue
        if not n.startswith('0') and any(a + b == int(d) for i, a in enumerate(parts) for b in parts[i + 1:]):
            continue  # a total of two official amounts (9,600 + 3,000 = 12,600)
        out.append(('電話 ' if n.startswith('0') else '金額／數字 ') + n + ' 在正式資料裡找不到')
    if what == 'dealer':
        names = [SUFFIX.sub('', s.split('｜')[0].strip()) for s in ref.splitlines() if '｜' in s]
        if names and not any(n and n in said for n in names) and re.search(r'門市|經銷|店|汽車音響', said):
            out.append('沒有提到正式清單裡的門市')
    return out


def _item(r, kb=None, known=frozenset()):
    text = plain(r['topic']) + ' ' + ' '.join(o.get('say', '') for o in r['options'])
    what = kind(plain(r['topic']), text)
    pieces = official(kb, what, text, plain(r['topic'])) if kb is not None and what else []
    opts = []
    for o in r['options']:
        named = [plain(s) for s in o.get('sources') or [] if not PAST.fullmatch(str(s))]
        say = plain(o.get('say', ''))
        opts.append(dict(say=say, latest=str(o.get('latest') or ''), replies=len(o.get('sources') or []) - len(named),
                         named=named[:3], warn=differs(what, say, pieces, known)))
    return dict(id=r['id'], group=r['grp'].replace('×', '・'), topic=plain(r['topic']), options=opts,
                suggest=plain(r['suggest']), status=r['status'], choice=r['choice'], answer=r['answer'],
                override=bool(r.get('override')), scope=r.get('scope') or '', kind=what, official=pieces,
                decided=local_time(r['decided_at'])[5:16].replace('-', '/') if r['decided_at'] else '')


def counts(store):
    row = store.one("""SELECT COUNT(*) FILTER (WHERE status='open') AS open, COUNT(*) FILTER (WHERE status='later') AS later,
      COUNT(*) FILTER (WHERE status='decided') AS decided FROM desk_kb_conflicts""")
    return dict(row)


def listing(store, tab='open', page=0, product=''):
    """product: only this product's questions (the part of grp before ×; Gemini 11/18 page review, batch 2)."""
    if tab not in TABS:
        raise ActionError('分頁不正確', 400)
    if not isinstance(product, str) or len(product) > 40:
        raise ActionError('產品分類不正確', 400)
    order = 'decided_at DESC' if tab == 'decided' else 'rank, id'
    rows = store.all(f"""SELECT * FROM desk_kb_conflicts WHERE status=%s AND (%s='' OR split_part(grp,'×',1)=%s)
      ORDER BY {order} LIMIT %s OFFSET %s""", (tab, product, product, PAGE + 1, max(0, int(page)) * PAGE))
    products = store.all("""SELECT split_part(grp,'×',1) AS name, COUNT(*) AS n FROM desk_kb_conflicts WHERE status=%s
      GROUP BY 1 ORDER BY 2 DESC, 1""", (tab,))
    kb = store.kb(OFFICIAL)
    known = corpus(kb)
    return dict(items=[_item(r, kb, known) for r in rows[:PAGE]], has_more=len(rows) > PAGE,
                products=[dict(p) for p in products], product=product, **counts(store))


def decide(store, staff, data):
    """data: {id, choice: option index | -1 for his own words | null to put it off, answer: his own words,
    override: once he saw that the wording disagrees with the official data — false (keep the official stores/prices),
    'replace' (this wording replaces the official data) or, for a store question, 'add' (this store is added and the
    official ones are still listed; ChatGPT 11/18: picking 尚迪 must not silently drop 德威 and 佑禾)}."""
    require_reviewer(staff)
    if not isinstance(data, dict) or type(data.get('id')) is not int:
        raise ActionError('操作格式不正確', 400)
    row = store.one('SELECT * FROM desk_kb_conflicts WHERE id=%s', (data['id'],))
    if not row:
        raise ActionError('找不到這一題，請重新載入', 404)
    choice, answer = data.get('choice'), data.get('answer') or ''
    if not isinstance(answer, str) or len(answer) > 500:
        raise ActionError('自己寫的說法最多 500 字', 400)
    answer = answer.strip()
    if choice is None:
        store.execute("""UPDATE desk_kb_conflicts SET status='later',choice=NULL,answer='',override=FALSE,scope='',decided_by=%s,
          decided_at=NOW() WHERE id=%s""", (staff['id'], data['id']))
        return dict(ok=True, **counts(store))
    if type(choice) is not int or not -1 <= choice < len(row['options']):
        raise ActionError('請選一個說法', 400)
    if choice == -1 and len(answer) < 2:
        raise ActionError('請寫正確的說法', 400)
    said = answer if choice == -1 else plain(row['options'][choice].get('say', ''))
    kb = store.kb(OFFICIAL)
    item = _item(row, kb)
    what = kind(item['topic'], ' '.join(o['say'] for o in item['options']))
    why = differs(what, said, item['official'], corpus(kb))
    mode = data.get('override')
    if mode is True:
        mode = 'replace'
    if why and mode not in (False, 'replace', 'add'):
        raise ActionError('這個說法跟正式資料不同：' + '；'.join(why) + '。', 409)
    if mode == 'add' and what != 'dealer':
        raise ActionError('只有門市題可以「加進清單」', 400)
    scope = (mode or 'official') if why else ''  # official: his wording, but stores/phones/prices follow the official data
    override = scope in ('add', 'replace')
    store.execute("""UPDATE desk_kb_conflicts SET status='decided',choice=%s,answer=%s,override=%s,scope=%s,decided_by=%s,
      decided_at=NOW() WHERE id=%s""", (choice, answer, override, scope, staff['id'], data['id']))
    if override:
        # The official file is now the wrong one: same path as 「回報資料有誤」, so Claude fixes the source.
        from . import basis
        how = '加進正式清單（原有門市保留）' if scope == 'add' else '取代正式資料'
        basis.report(store, staff, dict(key=REPORT_KEY.get(what, 'faq'), where=f"衝突題「{item['topic']}」Wayne 確認{how}"[:300],
                                         should=said[:300]))
    return dict(ok=True, override=override, scope=scope, **counts(store))


def reopen(store, staff, data):
    require_reviewer(staff)
    if not isinstance(data, dict) or type(data.get('id')) is not int:
        raise ActionError('操作格式不正確', 400)
    if not store.execute("""UPDATE desk_kb_conflicts SET status='open',choice=NULL,answer='',override=FALSE,scope='',decided_by=NULL,
      decided_at=NULL WHERE id=%s AND status<>'open'""", (data['id'],)):
        raise ActionError('這一題已經是待決定了', 409)
    return dict(ok=True, **counts(store))


# Which decided questions a draft gets: 不分產品 ones always, the rest when the conversation, the ad or the extracted
# products mention that product. (All 346 in every prompt would be about 15,000 tokens and bury the ones that matter.)
PRODUCT_WORDS = {
    '安卓主機': r'安卓|車機|主機|螢幕|導航|雙系統|T系列|H系列|A系列|EX|DX|MX|DS|T9|T10|T3|A3\d|H2\d',
    '360環景': r'環景|360',
    '電子後視鏡': r'電子後視鏡|電子後照鏡|後視鏡|後照鏡|S14|B42|418|R3D',
    '行車記錄器': r'行車紀錄|行車記錄|記錄器|紀錄器|行車|R3D|418',
    '電動尾門': r'尾門|撐桿|腳踢|行李箱',
    '盲點偵測': r'盲點|BSD|盲區',
    'CarPlay與安卓盒': r'carplay|安卓盒|Z8|Z系列',
    '隔音': r'隔音|制震|胎噪|靜音',
    '音響': r'音響|喇叭|重低音|擴大機|DSP',
    '後座螢幕': r'吸頂|後座|背掛',
    '氣氛燈': r'氣氛燈|氛圍燈',
    'Tesla專用配件': r'tesla|特斯拉|model ?[3ysx]',
}
OVERRIDE = '【Wayne 確認改用，蓋過正式資料】'
ADDED = '【Wayne 確認新增，正式清單上的門市也照列】'
OFFICIAL_WINS = '（門市、電話、價格照正式資料，這句裡不同的不要用）'


def for_draft(store, text):
    """Lines for the drafting prompt: 「題目：Wayne 選的說法」 for each decided question that applies to this conversation."""
    rows = store.all("""SELECT grp,topic,options,choice,answer,override,scope FROM desk_kb_conflicts WHERE status='decided'
      ORDER BY rank,id""")
    out = []
    for r in rows:
        product = r['grp'].split('×')[0]
        pat = PRODUCT_WORDS.get(product)
        if product not in ('不分產品', '其他產品') and not (pat and re.search(pat, text or '', re.I)):
            continue
        said = r['options'][r['choice']]['say'] if r['choice'] is not None and 0 <= r['choice'] < len(r['options']) else ''
        rule = said + ('（補充：' + r['answer'] + '）' if said and r['answer'] else r['answer'])
        if rule:
            mark = {'replace': OVERRIDE, 'add': ADDED, 'official': OFFICIAL_WINS}.get(r['scope'] or '', OVERRIDE if r['override'] else '')
            out.append(f"- {plain(r['topic'])}：{rule}" + mark)
    return out[:80]
