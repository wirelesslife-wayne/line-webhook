"""「AI 回覆依據」（Wayne 11/16 approved the preview after ChatGPT and Gemini reviewed it): what the drafts lean on,
in three parts Wayne can see — his own rules (add, stop, start again), the replies the desk learned from (search, stop,
start again) and the company's official data (read only; mistakes are reported to Claude, who fixes the source).
It replaces the old 知識審核 page, which nobody ever used."""
import json
import re
import uuid
from .errors import ActionError
from .knowledge import require_reviewer
from .worker import local_time

OFF = 'Wayne 停用｜'  # desk_learned keeps its two verdicts; a stopped example is 'no' with this in front of its reason
PAGE = 20
DOCS = {  # what 公司正式資料 shows, in this order
    'price': ('價格表', 'wlf-data.json', '官網車型查詢的完工價'),
    'hours': ('安裝工時表', 'install_hours.json', ''),
    'dealers': ('經銷商門市', '經銷商清單.txt', ''),
    'canned': ('制式回覆', 'canned_v2.json', ''),
    'faq': ('常見問答', 'faq_v2.json', ''),
    'tone': ('說話方式', '口吻規則.md', '語氣與用詞規則'),
}


def rules(store):
    return [dict(id=r['id'], text=r['instruction'], active=r['active'], time=local_time(r['created_at'])[5:10].replace('-', '/'))
            for r in store.all('SELECT id,instruction,active,created_at FROM desk_edit_rules ORDER BY active DESC,id DESC LIMIT 300')]


def add_rule(store, staff, data):
    require_reviewer(staff)
    text = (data or {}).get('text') if isinstance(data, dict) else None
    if not isinstance(text, str) or not 2 <= len(text.strip()) <= 500:
        raise ActionError('請寫一句規則，最多 500 字', 400)
    row = store.one("""INSERT INTO desk_edit_rules(instruction,channel,conversation_summary,original_draft,created_by)
      VALUES(%s,'all','','',%s) RETURNING id""", (text.strip(), staff['id']))
    return dict(ok=True, id=row['id'])


def learned(store, q='', page=0, stopped=False):
    q = (q or '').strip()[:40]
    where = "reason LIKE %s" if stopped else "verdict='better'"
    args = [OFF + '%'] if stopped else []
    if q:
        where += ' AND (question ILIKE %s OR answer ILIKE %s)'
        like = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        args += [like, like]
    rows = store.all(f"""SELECT digest,source,question,answer,judged_at FROM desk_learned WHERE {where}
      ORDER BY judged_at DESC LIMIT %s OFFSET %s""", (*args, PAGE + 1, max(0, int(page)) * PAGE))
    total = store.one(f'SELECT COUNT(*) AS n FROM desk_learned WHERE {where}', tuple(args))['n']
    counts = store.one(f"""SELECT COUNT(*) FILTER (WHERE verdict='better') AS on_n,
      COUNT(*) FILTER (WHERE reason LIKE %s) AS off_n FROM desk_learned""", (OFF + '%',))
    label = {'fb': 'FB', 'ig': 'IG', 'line': 'LINE'}
    return dict(items=[dict(id=r['digest'], source=label.get(r['source'], r['source']), question=r['question'][:300],
                            answer=r['answer'][:1200], time=local_time(r['judged_at'])[5:10].replace('-', '/'))
                       for r in rows[:PAGE]],
                has_more=len(rows) > PAGE, total=total, active=counts['on_n'], stopped=counts['off_n'])


def set_learned(store, staff, digest, active):
    require_reviewer(staff)
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or type(active) is not bool:
        raise ActionError('操作格式不正確', 400)
    if active:
        done = store.execute("""UPDATE desk_learned SET verdict='better',reason=substr(reason,%s)
          WHERE digest=%s AND reason LIKE %s""", (len(OFF) + 1, digest, OFF + '%'))
    else:
        done = store.execute("UPDATE desk_learned SET verdict='no',reason=%s||reason WHERE digest=%s AND verdict='better'",
                             (OFF, digest))
    if not done:
        raise ActionError('這筆已經改過了，請重新載入', 409)
    return dict(ok=True)


# 「回報資料有誤」(Wayne 11/17 picked LINE): kept in desk_marks as kbr:<id> with state open -> sent -> done.
# The office PC's one-minute job (WL-DeskSync) sends each new one to Wayne by LINE (the server has no LINE to him);
# Claude fixes the source and closes it with `python -m desk.tools.basis_reports done <id>`.
def report(store, staff, data):
    require_reviewer(staff)
    if not isinstance(data, dict) or data.get('key') not in DOCS:
        raise ActionError('沒有這份資料', 400)
    where, should = data.get('where'), data.get('should', '')
    if not isinstance(where, str) or not 2 <= len(where.strip()) <= 300 or not isinstance(should, str) or len(should) > 300:
        raise ActionError('請寫哪裡錯（最多 300 字）', 400)
    rid = uuid.uuid4().hex[:10]
    store.execute("INSERT INTO desk_marks(name,value) VALUES(%s,%s)", ('kbr:' + rid, json.dumps(dict(
        key=data['key'], title=DOCS[data['key']][0], where=where.strip(), should=should.strip(), state='open'), ensure_ascii=False)))
    return dict(ok=True, id=rid)


def reports(store, states=('open', 'sent')):
    out = []
    for r in store.all("SELECT name,value,at FROM desk_marks WHERE name LIKE 'kbr:%%' ORDER BY at"):
        try:
            v = json.loads(r['value'])
        except ValueError:
            continue
        if v.get('state') in states:
            out.append(dict(v, id=r['name'][4:], at=local_time(r['at'])[5:16]))
    return out


def set_report(store, rid, state):
    row = store.one('SELECT value FROM desk_marks WHERE name=%s', ('kbr:' + str(rid),))
    if not row:
        raise ActionError('找不到這筆回報', 404)
    v = json.loads(row['value'])
    v['state'] = state
    store.execute('UPDATE desk_marks SET value=%s WHERE name=%s', (json.dumps(v, ensure_ascii=False), 'kbr:' + str(rid)))
    return dict(ok=True)


def docs(store):
    rows = {r['name']: r for r in store.all('SELECT name,content,updated_at FROM desk_kb WHERE name=ANY(%s)',
                                            ([d[1] for d in DOCS.values()],))}
    out = []
    waiting = {}
    for r in reports(store):
        waiting[r['key']] = waiting.get(r['key'], 0) + 1
    for key, (title, name, note) in DOCS.items():
        r = rows.get(name)
        c = r['content'] if r else None
        size = {'price': lambda: f"{len((c or {}).get('cars') or [])} 個車款年份",
                'hours': lambda: f"{len((c or {}).get('items') or [])} 項",
                'dealers': lambda: f"{sum(1 for l in str(c or '').splitlines() if '｜' in l)} 個據點",
                'canned': lambda: f"{len(c or [])} 篇", 'faq': lambda: f"{len(c or [])} 題", 'tone': lambda: note}[key]()
        out.append(dict(key=key, title=title, size=size if key != 'price' else note,
                        updated=local_time(r['updated_at'])[5:10].replace('-', '/') if r else '', ok=bool(r),
                        reported=waiting.get(key, 0)))
    return out


def doc(store, key, q=''):
    if key not in DOCS:
        raise ActionError('沒有這份資料', 404)
    row = store.one('SELECT content FROM desk_kb WHERE name=%s', (DOCS[key][1],))
    if not row:
        raise ActionError('這份資料還沒上傳', 404)
    c = row['content']
    if key == 'hours':
        return dict(kind='table', head=['項目', '工時', '依據'], rows=[[i.get('item', ''), i.get('hours', ''), i.get('source', '')]
                                                                     for i in c.get('items') or []])
    if key == 'dealers':
        return dict(kind='table', head=['門市', '地址', '電話'],
                    rows=[(l.split('｜') + ['', ''])[:3] for l in str(c).splitlines() if '｜' in l])
    if key == 'canned':
        return dict(kind='list', items=[dict(title=i.get('topic', ''), text=i.get('text', '')) for i in c])
    if key == 'faq':
        return dict(kind='list', items=[dict(title=i.get('q', ''), text=i.get('a', '')) for i in c])
    if key == 'tone':
        return dict(kind='text', text=str(c))
    return dict(kind='price', lines=price(c, q))


def price(wlf, q):
    """The same lines a draft gets for one car (Engine.price_lookup), found from what Wayne types: 「CX-30」「Corolla Cross 2023」."""
    q = (q or '').strip()
    if not q:
        return []
    def norm(s):
        return re.sub(r'[\s\-]', '', str(s or '').upper())
    year = next((int(y) for y in re.findall(r'(?:19|20)\d\d', q)), None)
    words = norm(re.sub(r'(?:19|20)\d\d', '', q))
    if not words:
        return []
    all_cars = [c for c in wlf.get('cars') or [] if norm(c['model'])]
    # exact model (or brand + model) first; then models that contain what he typed; a short model inside his words
    # (「X3」 inside 「CX30」) only when nothing else matched
    cars = ([c for c in all_cars if words in (norm(c['model']), norm(c['b']) + norm(c['model']))]
            or [c for c in all_cars if words in norm(c['model']) or words in norm(c['b']) + norm(c['model'])]
            or [c for c in all_cars if len(norm(c['model'])) >= 4 and norm(c['model']) in words])
    if year:
        cars = [c for c in cars if c['year'] == year] or cars
    agg = {}
    for c in cars:
        for series, items in (wlf['plans'][c['p']] or {}).items():
            for it in items:
                agg.setdefault((c['b'], c['model'], series, it['code']), set()).add((it['price'], c['year']))
    lines = []
    for (b, m, series, code), vals in sorted(agg.items()):
        prices = sorted({p for p, _ in vals}); years = sorted({y for _, y in vals})
        ps = f'${prices[0]:,}' if len(prices) == 1 else '依年份 ' + '／'.join(f'${p:,}' for p in prices)
        lines.append(f'{b} {m}（{years[0]}～{years[-1]}）｜{series} 系列 {code}｜完工價 {ps}')
    return lines[:60]
