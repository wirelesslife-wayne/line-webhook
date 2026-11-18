"""AI 草稿學得如何 (Wayne 11/18 照三家 AI 共識做): four numbers that do not flatter the AI.

首稿可用率  the AI's first draft went out as written or with small wording edits: no 修改指示 redraft before the approval,
            no amount / phone / link the AI wrote taken out, and at least 85% of the text kept.
重大錯誤    the AI wrote an amount, phone number or link that Wayne took out or changed (missing ones he added are not counted
            here; they only make the draft not usable).
數字比對通過率 (was 抽查正確率; ChatGPT 11/18: it only checks numbers) drafts sent unchanged, checked against the official data (price table, canned replies, FAQ, hours, stores):
            catches what Wayne may have missed. Up to SPOT drafts a week, free (no AI call).
樣本        how many approved drafts the numbers come from; under ENOUGH a number is shown as 樣本不足.
Counted from BASELINE: before it, redrafts were not logged and the numbers read too high.
"""
import difflib
import hashlib
import re
from datetime import datetime, timedelta

from .worker import TAIPEI

BASELINE = datetime(2026, 11, 19, tzinfo=TAIPEI)
ENOUGH = 30          # fewer approved drafts than this: 樣本不足
SPOT = 30            # spot checks a week
USABLE_RATIO = 0.85  # text kept for a small edit (tone, a greeting) to still count as usable
SHORT = 60           # a reply shorter than this (characters) must keep 95%
GATE = dict(weeks=4, rate=95, weekly=25)  # Gemini／ChatGPT 11/18: every week needs enough drafts, not just the total  # a category may be considered for full automation
CHANNEL = {'fb_message': 'FB 私訊', 'ig_message': 'IG 私訊', 'fb_comment': 'FB 留言', 'ig_comment': 'IG 留言',
           'line_message': 'LINE'}
INTENTS = (('報價', r'多少|價格|價錢|報價|費用|完工價|\$|元'), ('門市地點', r'地址|在哪|門市|哪裡|店'),
           ('預約付款', r'預約|訂金|匯款|轉帳|刷卡|分期|付款'), ('保固售後', r'保固|壞|故障|維修|異常|沒反應|不能用|當機'),
           ('能不能裝', r'可以裝|能裝|適用|支援|可以用|有沒有'), ('安裝時間', r'多久|幾小時|工時|幾點|什麼時候'))
AMOUNT = re.compile(r'\d{1,3}(?:,\d{3})+|\d{4,6}')
PHONE = re.compile(r'(?:\+886[ -]?|\(?0)\d{1,3}\)?[ -]?\d{3,4}-?\d{3,4}')
URL = re.compile(r'https?://[A-Za-z0-9./?=&_%#:+~-]+')


def _norm(t):
    return re.sub(r'\s+', '', (t or '').replace('[[下一則]]', ''))


def _digits(s):
    return re.sub(r'\D', '', s)


def intent(question):
    return next((name for name, pat in INTENTS if re.search(pat, question or '')), '其他')


def wrong(ai, sent):
    """What the AI wrote that Wayne took out: amounts, phones, links (each a short plain sentence).
    A phone only reformatted ((02) 2952-5859 for 02-29525859) is not a change."""
    out = []
    sent_digits = _digits(sent)
    gone = [p for p in PHONE.findall(ai) if _digits(p) not in sent_digits]
    if gone:
        out.append('電話 ' + '、'.join(dict.fromkeys(gone)) + (' 被改掉' if PHONE.search(sent) else ' 被拿掉'))
    ai_np, sent_np = PHONE.sub(' ', ai), PHONE.sub(' ', sent)
    kept = {_digits(n) for n in AMOUNT.findall(sent_np)}
    gone = [n for n in AMOUNT.findall(ai_np) if _digits(n) not in kept and not re.fullmatch(r'(19|20)\d\d', _digits(n))]
    if gone:
        out.append('金額 ' + '、'.join(dict.fromkeys(gone)) + (' 被改掉' if kept else ' 被拿掉'))
    links = set(URL.findall(sent))
    gone = [u for u in URL.findall(ai) if u not in links]
    if gone:
        out.append('連結 ' + '、'.join(dict.fromkeys(gone)) + ' 被拿掉')
    return out


def grade(r):
    """One approved draft: usable / major error / how much of the text was kept."""
    ratio = difflib.SequenceMatcher(None, _norm(r['ai_draft']), _norm(r['content'])).ratio()
    errors = wrong(r['ai_draft'], r['content'])
    # Gemini 11/18: in a two-line reply three changed characters (有現貨 -> 需預訂) still keep 90% of the text
    need = USABLE_RATIO if len(_norm(r['ai_draft'])) >= SHORT else 0.95
    usable = not r['redrafted'] and not errors and ratio >= need
    return dict(ratio=ratio, errors=errors, usable=usable, unchanged=not r['redrafted'] and ratio >= 0.98)


def rows(store, since, until=None):
    until = until or datetime.now(TAIPEI) + timedelta(days=1)
    out = store.all("""SELECT a.retry_key::text AS id,a.thread_id,a.content,a.ai_draft,a.question,a.approved_at,t.channel,t.name,
      EXISTS(SELECT 1 FROM desk_fb_redrafts r WHERE r.thread_id=a.thread_id AND r.created_at<a.approved_at
        AND r.created_at>COALESCE((SELECT MAX(b.approved_at) FROM desk_fb_attempts b
          WHERE b.thread_id=a.thread_id AND b.approved_at<a.approved_at),'-infinity'::timestamptz)) AS redrafted,
      (t.context LIKE '%%廣告「%%' OR EXISTS(SELECT 1 FROM desk_fb_events e WHERE e.thread_id=a.thread_id
        AND e.note LIKE '%%廣告預設問題%%')) AS ad
      FROM desk_fb_attempts a JOIN desk_fb_threads t ON t.id=a.thread_id
      WHERE a.status IN ('approved','sent') AND a.ai_draft<>'' AND a.approved_at>=%s AND a.approved_at<%s
      ORDER BY a.approved_at""", (since, until))
    for r in out:
        r.update(grade(r))
        r['intent'] = intent(r['question'])
    return out


def spot_check(kb, items):
    """Drafts sent unchanged, checked against every number in the official data (free; same check as the 衝突題 page).
    At most SPOT, picked the same way every time so the numbers do not jump around on reload."""
    from .kb_conflicts import corpus, differs
    known = corpus(kb)
    picked = sorted((r for r in items if r['unchanged']), key=lambda r: hashlib.sha1(r['id'].encode()).hexdigest())[:SPOT]
    return [dict(r, flags=differs('price', r['content'], [], known)) for r in picked]


def _pct(a, b):
    return round(100 * a / b) if b else None


def summary(items, spots):
    n = len(items)
    bad = [s for s in spots if s['flags']]
    return dict(samples=n, enough=n >= ENOUGH, usable=sum(r['usable'] for r in items),
                usable_pct=_pct(sum(r['usable'] for r in items), n),
                major=sum(1 for r in items if r['errors']), redrafted=sum(r['redrafted'] for r in items),
                ads=sum(bool(r['ad']) for r in items), spot_checked=len(spots), spot_ok=len(spots) - len(bad),
                spot_pct=_pct(len(spots) - len(bad), len(spots)))


def _week_start(d):
    d = d.astimezone(TAIPEI)
    return datetime(d.year, d.month, d.day, tzinfo=TAIPEI) - timedelta(days=d.weekday())


def _short(d):
    return f'{d.month}/{d.day}'


def _problem(r, what):
    return dict(day=r['approved_at'].astimezone(TAIPEI).strftime('%m/%d %H:%M'), channel=CHANNEL.get(r['channel'], r['channel']),
                name=r['name'] or '客人', thread_id=r['thread_id'], what='；'.join(what), intent=r['intent'])


def merged(problems):
    """Gemini／ChatGPT 11/18: the same wrong link seven times is one line 「共 7 次」, newest first."""
    out = {}
    for p in sorted(problems, key=lambda p: p['day'], reverse=True):
        g = out.setdefault(p['what'], dict(p, times=0, more=[]))
        g['times'] += 1
        g['more'].append(f"{p['day']}・{p['channel']}・{p['name']}")
    return list(out.values())


def gate(weeks):
    """Per category: may it be considered for full automation? weeks = the last GATE['weeks'] weekly summaries."""
    # 11/18 ChatGPT＋Gemini review: say what is still missing, not only that it is missing
    thin = [w for w in weeks if w['samples'] < GATE['weekly']]
    if thin:
        return dict(ok=False, text=f'還差：{len(thin)} 週不到 {GATE["weekly"]} 則（最近一週 {weeks[-1]["samples"]} 則）')
    if any(w['major'] for w in weeks):
        return dict(ok=False, text=f'還差：近 {GATE["weeks"]} 週重大錯誤 {sum(w["major"] for w in weeks)} 次，要 0 次')
    low = [w for w in weeks if not w['samples'] or w['usable_pct'] < GATE['rate']]
    if low:
        return dict(ok=False, text=f'還差：{len(low)} 週可用率不到 {GATE["rate"]}%（最低 {min(w["usable_pct"] or 0 for w in low)}%）')
    # ChatGPT 11/18: the numbers only say Wayne rarely edits; a person still checks car fit, features and warranty first
    return dict(ok=True, text=f'數字連續 {GATE["weeks"]} 週達標，下一步：人工抽查內容（車款、功能、保固）')


def report(store, today=None, since=None):
    """Everything the 準確率 card and page show. since: count from here instead of BASELINE (previews only)."""
    today = (today or datetime.now(TAIPEI)).astimezone(TAIPEI)
    start = since or BASELINE
    first_week = _week_start(today) - timedelta(weeks=GATE['weeks'] - 1)
    items = rows(store, max(start, first_week))
    kb = store.kb()
    recent = [r for r in items if r['approved_at'] >= max(start, today - timedelta(days=7))]
    spots = spot_check(kb, recent)
    card = summary(recent, spots)
    weeks = []
    for i in range(GATE['weeks']):
        a = first_week + timedelta(weeks=i)
        b = a + timedelta(weeks=1)
        part = [r for r in items if a <= r['approved_at'] < b]
        w = summary(part, spot_check(kb, part))
        w.update(label=f'{_short(a)}～{_short(b - timedelta(days=1))}', before=b <= start, current=a <= today < b)
        weeks.append(w)
    groups = [('廣告客人', lambda r: r['ad']), ('一般客人', lambda r: not r['ad'])] + \
             [(name, (lambda n: lambda r: r['intent'] == n)(name)) for name, _ in INTENTS + (('其他', ''),)]
    cats = []
    for name, keep in groups:
        per = []
        for i in range(GATE['weeks']):
            a = first_week + timedelta(weeks=i)
            per.append(summary([r for r in items if keep(r) and a <= r['approved_at'] < a + timedelta(weeks=1)], []))
        mine = [r for r in items if keep(r)]
        cats.append(dict(name=name, kind='who' if name.endswith('客人') else 'topic', **summary(mine, []), gate=gate(per)))
    problems = merged([_problem(r, [e]) for r in recent for e in r['errors']])
    flagged = [_problem(s, s['flags']) for s in spots if s['flags']]
    return dict(card=card, weeks=weeks, categories=cats, problems=problems, flagged=flagged[:20],
                since=_short(max(start, today - timedelta(days=7))), baseline=_short(BASELINE), enough=ENOUGH, gate=GATE)


def weekly_text(store, today=None, url='https://line-webhook-airz.onrender.com/desk/quality'):
    """Monday LINE summary for Wayne (≤12 lines, conclusion first). None when last week had no approved drafts."""
    today = (today or datetime.now(TAIPEI)).astimezone(TAIPEI)
    end = _week_start(today)
    a, b = end - timedelta(weeks=1), end
    items = [r for r in rows(store, max(a, BASELINE), b)]
    if not items:
        return None
    kb = store.kb()
    w = summary(items, spot_check(kb, items))
    prev_items = rows(store, max(a - timedelta(weeks=1), BASELINE), a) if a > BASELINE else []
    prev = summary(prev_items, spot_check(kb, prev_items)) if prev_items else None
    rate = f"{w['usable_pct']}%" if w['enough'] else f"樣本不足（{w['samples']}／{ENOUGH} 則）"
    if prev and prev['enough'] and w['enough']:
        rate += f"（前一週 {prev['usable_pct']}%）"
    kinds = {}
    for r in items:
        for e in r['errors']:
            k = e.split(' ')[0]
            kinds[k] = kinds.get(k, 0) + 1
    major = f"{w['major']} 次" + ('（' + '、'.join(f'{k} {v}' for k, v in kinds.items()) + '）' if kinds else '')
    head = '🔴' if w['major'] else '📈'
    lines = [f"{head} AI 客服草稿上週成績（{_short(a)}～{_short(b - timedelta(days=1))}）",
             f"首稿可用率：{rate}",
             f"重大錯誤：{major}",
             f"數字比對通過：{w['spot_ok']}／{w['spot_checked']} 則" if w['spot_checked'] else '數字比對：上週沒有原樣送出的草稿',
             f"樣本：{w['samples']} 則（廣告客人 {w['ads']}、一般 {w['samples'] - w['ads']}）"]
    if w['redrafted']:
        lines.append(f"叫 AI 重擬：{w['redrafted']} 則")
    worst = {}
    for r in items:
        if not r['usable']:
            worst[r['intent']] = worst.get(r['intent'], 0) + 1
    if worst:
        k, v = max(worst.items(), key=lambda x: x[1])
        lines.append(f"最常要改的題目：{k}（{v} 則）")
    lines.append(url)
    return '\n'.join(lines)
