"""知識庫衝突題 (Wayne 11/18 picked 「A 先做衝突題」).

The one-time knowledge consolidation read every past reply and found topics where we told customers different things
(an old price and a new one, two materials for the same job...). Each topic is one question here: Wayne taps the
option that is right today, writes his own, or leaves it for later. Every decided question goes into
every draft about that product as one of Wayne's own rules (for_draft below); a changed answer replaces the old one.

The questions are loaded from the office PC (desk/tools/load_kb_conflicts.py) because the source file holds masked
customer replies and must not go into this public repository.
"""
import re
from .errors import ActionError
from .knowledge import require_reviewer
from .worker import local_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_kb_conflicts (
 id BIGSERIAL PRIMARY KEY, digest TEXT UNIQUE NOT NULL, grp TEXT NOT NULL, topic TEXT NOT NULL,
 options JSONB NOT NULL, suggest TEXT NOT NULL DEFAULT '', rank INT NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','decided','later')),
 choice INT, answer TEXT NOT NULL DEFAULT '', decided_by BIGINT REFERENCES desk_users(id), decided_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
"""
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


def _item(r):
    opts = []
    for o in r['options']:
        named = [plain(s) for s in o.get('sources') or [] if not PAST.fullmatch(str(s))]
        opts.append(dict(say=plain(o.get('say', '')), latest=str(o.get('latest') or ''),
                         replies=len(o.get('sources') or []) - len(named), named=named[:3]))
    return dict(id=r['id'], group=r['grp'].replace('×', '・'), topic=plain(r['topic']), options=opts,
                suggest=plain(r['suggest']), status=r['status'],
                choice=r['choice'], answer=r['answer'],
                decided=local_time(r['decided_at'])[5:16].replace('-', '/') if r['decided_at'] else '')


def counts(store):
    row = store.one("""SELECT COUNT(*) FILTER (WHERE status='open') AS open, COUNT(*) FILTER (WHERE status='later') AS later,
      COUNT(*) FILTER (WHERE status='decided') AS decided FROM desk_kb_conflicts""")
    return dict(row)


def listing(store, tab='open', page=0):
    if tab not in TABS:
        raise ActionError('分頁不正確', 400)
    order = 'decided_at DESC' if tab == 'decided' else 'rank, id'
    rows = store.all(f"""SELECT * FROM desk_kb_conflicts WHERE status=%s ORDER BY {order} LIMIT %s OFFSET %s""",
                     (tab, PAGE + 1, max(0, int(page)) * PAGE))
    return dict(items=[_item(r) for r in rows[:PAGE]], has_more=len(rows) > PAGE, **counts(store))


def decide(store, staff, data):
    """data: {id, choice: option index | -1 for his own words | null to put it off, answer: his own words}."""
    require_reviewer(staff)
    if not isinstance(data, dict) or type(data.get('id')) is not int:
        raise ActionError('操作格式不正確', 400)
    row = store.one('SELECT options FROM desk_kb_conflicts WHERE id=%s', (data['id'],))
    if not row:
        raise ActionError('找不到這一題，請重新載入', 404)
    choice, answer = data.get('choice'), data.get('answer') or ''
    if not isinstance(answer, str) or len(answer) > 500:
        raise ActionError('自己寫的說法最多 500 字', 400)
    answer = answer.strip()
    if choice is None:
        store.execute("""UPDATE desk_kb_conflicts SET status='later',choice=NULL,answer='',decided_by=%s,decided_at=NOW()
          WHERE id=%s""", (staff['id'], data['id']))
        return dict(ok=True, **counts(store))
    if type(choice) is not int or not -1 <= choice < len(row['options']):
        raise ActionError('請選一個說法', 400)
    if choice == -1 and len(answer) < 2:
        raise ActionError('請寫正確的說法', 400)
    store.execute("""UPDATE desk_kb_conflicts SET status='decided',choice=%s,answer=%s,decided_by=%s,decided_at=NOW()
      WHERE id=%s""", (choice, answer, staff['id'], data['id']))
    return dict(ok=True, **counts(store))


def reopen(store, staff, data):
    require_reviewer(staff)
    if not isinstance(data, dict) or type(data.get('id')) is not int:
        raise ActionError('操作格式不正確', 400)
    if not store.execute("""UPDATE desk_kb_conflicts SET status='open',choice=NULL,answer='',decided_by=NULL,decided_at=NULL
      WHERE id=%s AND status<>'open'""", (data['id'],)):
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


def for_draft(store, text):
    """Lines for the drafting prompt: 「題目：Wayne 選的說法」 for each decided question that applies to this conversation."""
    rows = store.all("SELECT grp,topic,options,choice,answer FROM desk_kb_conflicts WHERE status='decided' ORDER BY rank,id")
    out = []
    for r in rows:
        product = r['grp'].split('×')[0]
        pat = PRODUCT_WORDS.get(product)
        if product not in ('不分產品', '其他產品') and not (pat and re.search(pat, text or '', re.I)):
            continue
        said = r['options'][r['choice']]['say'] if r['choice'] is not None and 0 <= r['choice'] < len(r['options']) else ''
        rule = said + ('（補充：' + r['answer'] + '）' if said and r['answer'] else r['answer'])
        if rule:
            out.append(f"- {plain(r['topic'])}：{rule}")
    return out[:80]
