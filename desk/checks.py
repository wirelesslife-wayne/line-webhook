"""The draft's key facts as chips above the draft box (Wayne 11/19, discussed with ChatGPT and Gemini).

Each chip is ok (對得上), warn (待確認: the AI guessed it or the data is not sure) or bad (有衝突). Colour is never the
only signal: every chip carries words, and tapping it says why. An amount is only ok when the car's own price lines
or a saved reply about the same product carry it - a price that exists somewhere is not enough (both AIs warned that
green must not make him trust a wrong product's price). Nothing here changes the draft or sends anything.
"""
import itertools
import json
import re
import time

_KB = {'at': 0.0}
AMOUNT = re.compile(r'(?<![\d.])(\d{1,3}(?:,\d{3})+|\d{4,6})(?![\d年])')
YEAR = re.compile(r'(?<![\d,$])((?:19[89]|20[0-3])\d)(?!\d)')
YEAR_RANGE = re.compile(r'(?:19|20)\d{2}\s*[～~\-–至到]\s*(?:19|20)?\d{2}')  # 「2013～2020」 is a fit range, not the customer's year
SHORT_YEAR = re.compile(r'(?<!\d)([0-3]\d)\s*年(?:式|份|款)?')
PHONE = re.compile(r'(?<!\d)(0\d{1,2}-?\d{3,4}-?\d{3,4}|09\d{2}-?\d{3}-?\d{3})(?!\d)')
LINK = re.compile(r'https?://[^\s<>"「」（）()，。、]+')
# install time only (「10 小時停車監控」 is a feature, not how long the install takes)
HOURS = re.compile(r'(?:施工|安裝|工時)[^\n，。]{0,6}?(?:約|大約)?\s*(\d+(?:\s*[～~\-–]\s*\d+)?\s*(?:個)?(?:小時|分鐘))')
WARRANTY = re.compile(r'保固\s*([一二三兩\d]+\s*(?:年|個月))')
PRODUCT = re.compile(r'WRM-?\s?\w+|SV-?\s?\w+|\b(?:S14|B42(?:\s?MAX)?|418|R3D(?:\s?Plus)?|T\d{1,2}(?:\s?Plus)?|H2\d|A3\d(?:\s?Plus)?|DX\d|EX\d|MX\d|Z8(?:\s?Plus)?|KD3)\b', re.I)


def norm(s):
    return re.sub(r'[\s\-.．・]', '', str(s or '')).upper()


def _kb(store):
    if time.time() - _KB['at'] > 600:
        kb = store.kb(['canned_v2.json', 'wlf-data.json', '經銷商清單.txt', 'install_hours.json'])
        cars = (kb.get('wlf-data.json') or {}).get('cars') or []
        models = {str(c.get('model') or '').strip() for c in cars} - {''}
        canned = [str(x.get('title') or x.get('topic') or x.get('name') or '') + '\n' + str(x.get('text') or '') for x in kb.get('canned_v2.json') or [] if isinstance(x, dict)]
        try:  # our own YouTube videos and checked links are known, not 「資料裡沒有」
            from .videos import titles
            links = set(titles(store)) | {r['url'] for r in store.all('SELECT url FROM desk_links')}
        except Exception:
            links = set()
        rules = [x['instruction'] for x in store.all('SELECT instruction FROM desk_edit_rules WHERE active')]  # prices Wayne confirmed
        _KB.update(at=time.time(), links=links, rules=rules,
                   models=sorted((m for m in models if len(norm(m)) >= 3 and not norm(m).isdigit()), key=len, reverse=True),
                   canned=canned, dealers=re.sub(r'\D', '', str(kb.get('經銷商清單.txt') or '')),
                   hours=json.dumps(kb.get('install_hours.json') or {}, ensure_ascii=False))
    return _KB


def car_pattern(m):
    """A model name in free text: spaces and dashes optional (CR-V, CRV); short ones need edges (500 in $2,500 is not a FIAT)."""
    n = norm(m)
    body = r'[\s\-.]?'.join(re.escape(ch) for ch in n)
    return re.compile(r'(?<![A-Za-z0-9,$])' + body + r'(?![A-Za-z0-9])' if len(n) <= 4 else body, re.I)


def cars_in(text, models):
    found = []
    for m in models:  # longest first: 「COROLLA CROSS」 before 「COROLLA」
        n = norm(m)
        if not any(n in norm(f) for f in found) and car_pattern(m).search(text or ''):
            found.append(m)
    return found


def same_car(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b and (a in b or b in a))


def years_in(text):
    text = YEAR_RANGE.sub(' ', text or '')
    ys = {int(y) for y in YEAR.findall(text)}
    ys |= {2000 + int(y) for y in SHORT_YEAR.findall(text or '') if int(y) <= 35}
    return ys


def amounts(text):
    out = []
    for m in AMOUNT.finditer(text or ''):
        v = int(m.group(1).replace(',', ''))
        if v < 1000 or 1980 <= v <= 2039:
            continue
        out.append((m.group(1), v))
    return out


def numbers(text):
    return {int(x.replace(',', '')) for x in re.findall(r'\d{1,3}(?:,\d{3})+|\d{3,6}', (text or '').replace('$', ' '))}


def _other_price(v, codes, kinds, lines, same_product):
    """The official price of the very product this amount is for, when it is another number; '' when unsure."""
    from .engine import PRODUCT_TOPICS
    if not codes and not kinds:
        return ''
    if codes:
        src = [x for x in lines if codes & {norm(c) for c in PRODUCT.findall(x)}]
        src += [c for c in same_product if any(k in norm(c) for k in codes)]
    else:
        src = [x for x in lines if kinds & {t for pat, t in PRODUCT_TOPICS if re.search(pat, x, re.I)}]
        src += [c for c in same_product if c.split('\n', 1)[0].strip() in kinds]
    found = []
    for s in src:
        for row in s.split('\n'):
            if re.search(r'價|\$|元', row):
                found += [n for n in sorted(numbers(row)) if 1000 <= n <= 200000 and not 1980 <= n <= 2039 and n not in found]
    if not found or v in found:
        return ''
    return '、'.join(f'${n:,}' for n in found[:2])


def chips(store, thread, text):
    kb = _kb(store)
    full = text
    # phones and links hold digits that are not prices, years or car names (「2952-5859」 is not $5,859)
    text = LINK.sub(' ', PHONE.sub(' ', text or ''))
    r = thread.get('draft') or {}
    if not isinstance(r, dict):
        r = {}
    customer = ' '.join(m.get('content') or '' for m in thread.get('events') or [] if m.get('role') == 'user')
    customer += ' ' + str(thread.get('context') or '')
    ex = r.get('extract') if isinstance(r.get('extract'), dict) else {}
    out = []

    # 車款
    said = cars_in(customer, kb['models']) + ([ex['model']] if ex.get('model') else [])
    for m in cars_in(text, kb['models']):
        if any(same_car(m, c) for c in said):
            out.append(dict(kind='車款', label=m, state='ok', why='跟客人說的一樣'))
        elif said:
            out.append(dict(kind='車款', label=m, state='bad', why=f'客人說 {said[0]}'))
        else:
            out.append(dict(kind='車款', label=m, state='warn', why='客人沒說，AI 推測'))

    # 年份
    told = years_in(customer) | ({int(ex['year'])} if str(ex.get('year') or '').isdigit() else set())
    for y in sorted(years_in(text)):
        if y in told:
            out.append(dict(kind='年份', label=f'{y} 年', state='ok', why='跟客人說的一樣'))
        elif told:
            out.append(dict(kind='年份', label=f'{y} 年', state='bad', why=f'客人說 {"、".join(str(t) for t in sorted(told))} 年'))
        else:
            out.append(dict(kind='年份', label=f'{y} 年', state='warn', why='客人沒說，AI 推測'))

    # 金額: green only when the car's own price lines, or a saved reply about the same product, carry it
    lines = [str(x) for x in r.get('price_lines') or []]
    from .engine import PRODUCT_TOPICS  # the same 「電動尾門 → 電動尾門銷售回覆」 map the drafting uses
    topics = {title for pat, title in PRODUCT_TOPICS if re.search(pat, text, re.I)}
    same_product = [c for c in kb['canned'] if any(c.split('\n', 1)[0].strip() == t for t in topics)]
    pool = numbers(' '.join(lines) + ' ' + ' '.join(kb['canned']))
    small = sorted(p for p in pool if 100 <= p <= 100000)
    seen = set()
    for raw, v in amounts(text):
        if v in seen:
            continue
        seen.add(v)
        label = f'${raw}' if ',' in raw else f'${v:,}'
        # what the amount is the price of: its own line in the draft and the line above it (「👉 日鈦 SV-72｜完工價 $23,000」)
        rows = text.split('\n')
        at = next((i for i, row in enumerate(rows) if raw in row), 0)
        ctx = '\n'.join(rows[max(0, at - 1):at + 1])
        codes = {norm(c) for c in PRODUCT.findall(ctx)}
        kinds = {t for pat, t in PRODUCT_TOPICS if re.search(pat, ctx, re.I)}
        # a price line counts only for the same product: same model code, or (no code given) the same kind of product
        line = next((x for x in lines if v in numbers(x) and (
            codes & {norm(c) for c in PRODUCT.findall(x)} if codes
            else (kinds or topics) & {t for pat, t in PRODUCT_TOPICS if re.search(pat, x, re.I)})), None)
        reply = next((c for c in same_product if v in numbers(c) and (not codes or any(k in norm(c) for k in codes))), None)
        rule = next((x for x in kb['rules'] if v in numbers(x) and (any(k in norm(x) for k in codes) if codes
                     else (kinds or topics) & {t for pat, t in PRODUCT_TOPICS if re.search(pat, x, re.I)})), None)
        if rule and not line and not reply:
            out.append(dict(kind='金額', label=f'${raw}' if ',' in raw else f'${v:,}', state='ok', why='你確認過：' + rule[:30]))
            continue
        if line:
            out.append(dict(kind='金額', label=label, state='ok', why='價格表：' + line[:30]))
        elif reply:
            first = next((x.strip() for x in reply.split('\n') if x.strip()), '')
            out.append(dict(kind='金額', label=label, state='ok', why='制式回覆：' + first[:30]))
        elif any(a + b == v for a, b in itertools.combinations(small, 2)):
            out.append(dict(kind='金額', label=label, state='warn', why='像兩個價格相加，請核對'))
        elif other := _other_price(v, codes, kinds, lines, same_product):
            # ChatGPT 11/19: red only when the same product's official price says something else
            out.append(dict(kind='金額', label=label, state='bad', why=f'正式價 {other}'))
        elif v in pool:
            out.append(dict(kind='金額', label=label, state='warn', why='不確定是哪個產品的價格'))
        else:
            out.append(dict(kind='金額', label=label, state='warn', why='查無依據，請核對'))

    # 電話
    for p in dict.fromkeys(PHONE.findall(full)):
        ok = re.sub(r'\D', '', p) in kb['dealers']
        out.append(dict(kind='電話', label=p, state='ok' if ok else 'bad', why='門市清單有' if ok else '門市清單沒有這支'))

    # 連結
    known = ' '.join(lines) + ' '.join(kb['canned'])
    for u in dict.fromkeys(x.rstrip('.,;:!?') for x in LINK.findall(full)):
        ok = u in known or u in kb['links']
        flagged = next((str(f) for f in r.get('flags') or [] if u in str(f)), '')
        short = re.sub(r'^https?://(www\.)?', '', u)[:28]
        # the drafting AI's own note about this link: show it, yellow (11/19 a known link went red with 「資料裡有這個連結」)
        out.append(dict(kind='連結', label=short, state='warn' if flagged or not ok else 'ok',
                        why='AI 擬稿時有提醒，請點開確認' if flagged else '資料裡有' if ok else '資料裡沒有，送出前會再查'))

    # 施工時間、保固
    for h in dict.fromkeys(HOURS.findall(text)):
        ok = norm(h) in norm(kb['hours'] + ' '.join(kb['canned']))
        out.append(dict(kind='工時', label=h.strip(), state='ok' if ok else 'warn', why='工時表有' if ok else '工時表沒有，請核對'))
    for w in dict.fromkeys(WARRANTY.findall(text)):
        ok = norm('保固' + w) in norm(' '.join(kb['canned']))
        # the same product's saved reply states another warranty: that is a conflict, not a maybe (ChatGPT 11/19)
        other = next((m for c in same_product for m in WARRANTY.findall(c) if norm(m) != norm(w)), None) if not ok else None
        out.append(dict(kind='保固', label='保固' + w, state='ok' if ok else 'bad' if other else 'warn',
                        why='制式回覆有' if ok else f'制式回覆寫保固{other}' if other else '查無依據，請核對'))

    # 客人問了、草稿沒回到 (the AI's own list when it drafted)
    for p in r.get('points') or []:
        if isinstance(p, dict) and p.get('status') == '未回答' and p.get('q'):
            out.append(dict(kind='沒回到', label=str(p['q'])[:24], state='warn', why='客人問了，草稿沒答'))

    rank = {'bad': 0, 'warn': 1, 'ok': 2}
    return sorted(out, key=lambda c: rank[c['state']])[:16]


def for_thread(store, thread_id, text):
    from .errors import ActionError
    if not isinstance(text, str) or len(text) > 6000 or not isinstance(thread_id, str):
        raise ActionError('內容格式不正確', 400)
    t = store.one('SELECT id,context,draft FROM desk_fb_threads WHERE id=%s', (thread_id,))
    if not t:
        raise ActionError('找不到這則對話', 404)
    t = dict(t)
    t['events'] = store.all("SELECT role,content FROM desk_fb_events WHERE thread_id=%s AND role='user' ORDER BY id DESC LIMIT 40", (thread_id,))
    return chips(store, t, text)
