"""FB/IG conversations, drafts and approval receipts. The browser run (or, when switched on, desk.meta's webhook and
Send API) does the reading and sending; this module only keeps the state."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import threading
import time
import uuid
from decimal import Decimal
from psycopg2.extras import Json
from .db import now
from .errors import ActionError
from .worker import local_time, TAIPEI

# Wayne 11/13: a long reply can go out as up to 3 separate messages; the approved text keeps them joined by this line.
SPLIT_MARK = '[[下一則]]'
MAX_PARTS = 3
NO_REPLY = '（建議不回覆）'  # Wayne 11/15: off-topic comments get no answer; the draft says so instead


def parts(text):
    return [p.strip('\n') for p in str(text or '').split(SPLIT_MARK)]

# Wayne 11/13: the official LINE account joins the same review page; the browser run reads and sends in the LINE OA back office.
CHANNELS = ('fb_message', 'ig_message', 'fb_comment', 'ig_comment', 'line_message')
LABELS = dict(zip(CHANNELS, ('FB 私訊', 'IG 私訊', 'FB 留言', 'IG 留言', 'LINE 官方')))
PRIVATE = ('fb_message', 'ig_message', 'line_message')
LINE_CHAT_URL = 'https://chat.line.biz/Uec58b69514d4768bb1176cd4b2a22ef6/chat/{}'  # WirelessLife 無限 (@infinite5000)
TODO = ('waiting','drafting','ready','blocked','error')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def thread_id(channel, key):
    return 'fb:' + digest([channel, key])


def event_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def validate(payload):
    if not isinstance(payload, list) or len(payload) > 500:
        raise ActionError('輸入必須是對話陣列，每批最多 500 則', 400)
    normalized = []
    for item in payload:
        if not isinstance(item, dict) or item.get('channel') not in CHANNELS:
            raise ActionError('管道格式不正確', 400)
        row = dict(item)
        for name, limit in [('name',200),('key',1000),('url',4000),('context',10000)]:
            value = row.get(name, '')
            if not isinstance(value, str) or len(value) > limit or (name == 'name' and not value.strip()):
                raise ActionError('對話欄位格式不正確', 400)
            row[name] = value
        if row['url'] and not row['url'].startswith('https://'):
            raise ActionError('url 必須是 https 網址', 400)
        row['key'] = row['key'] or 'name:' + digest([row['channel'],row['name']])
        if row['channel'] == 'line_message' and not row['key'].startswith('name:'):
            row['key'] = row['key'].replace('-', '')  # the LINE run writes chat ids in groups of four
            row['id'] = thread_id(row['channel'], row['key'])
        if row['channel'] == 'line_message' and not row['url'] and not row['key'].startswith('name:'):
            row['url'] = LINE_CHAT_URL.format(row['key'])  # the LINE run leaves links out of its JSON
        row['id'] = thread_id(row['channel'], row['key'])
        if not isinstance(row.get('messages'), list) or len(row['messages']) > 2000:
            raise ActionError('messages 必須是依畫面時間由舊到新排列的陣列', 400)
        for msg in row['messages']:
            if not isinstance(msg,dict) or msg.get('role') not in ('user','page'):
                raise ActionError('訊息角色格式不正確', 400)
            for name,limit in [('text',10000),('at',200),('note',1000)]:
                if not isinstance(msg.get(name,''),str) or len(msg.get(name,'')) > limit:
                    raise ActionError('訊息欄位格式不正確', 400)
            if 'text' not in msg or not msg.get('at'):
                raise ActionError('每則訊息需有 text 與 at', 400)
        normalized.append(row)
    return normalized


def squash(text):
    return ''.join(str(text).split())


def answers(role, note):
    """A Page message only answers the customer when a person wrote it with substance.
    The browser run notes Meta auto-replies (away message, greeting, promotion, ad FAQ) as 「自動…」
    and colleagues' holding replies (「稍等」「幫您查」) as 「暫回」; neither closes the conversation."""
    # 「群發」: a broadcast reaches everyone, so it never answers this customer's question (11/12: 尤詠申 got one mid-question).
    return role == 'page' and not any(w in (note or '') for w in ('自動', '暫回', '群發', '推播'))


def kind(role, note):
    """'user' only for words the customer typed: a button pressed on an ad (「廣告預設問題」) is answered by
    the ad's own automatic reply, so on its own it is not a question waiting for us (Wayne 11/12)."""
    if role == 'user':
        # 「選單」: a LINE rich-menu tap (所有門市, 南部地區, 未購買_XX_客服) is written as a customer message but nobody typed it.
        # Except 「選單電話」: tapping one store's phone number says which store they want (Wayne 11/15), so someone answers it.
        if '選單電話' in (note or ''):
            return 'user'
        return 'auto' if any(w in (note or '') for w in ('廣告預設問題', '選單')) else 'user'
    return 'page' if answers(role, note) else 'auto'


def said(sequence, role):
    return ''.join(text for who, text in sequence if who == role)


def push(store, payload):
    """Each row is the conversation as the inbox shows it now (old to new); the thread keeps the latest snapshot.

    The browser run re-reads conversations every hour and writes times and line breaks differently each time,
    so "new" is decided by what one side has said in total, never by per-message time or wording of the post summary.
    """
    rows = validate(payload)  # Validate the whole batch before the transaction.
    counts = dict(threads=0, new_customer_messages=0, auto_closed=0)
    with store.cursor() as cur:
        # Stable order prevents cross-batch deadlocks; all mutations also lock the thread.
        for row in sorted(rows,key=lambda r:r['id']):
            tid = row['id']
            cur.execute("""INSERT INTO desk_fb_threads(id,channel,key,name,url,context)
              VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(channel,key) DO NOTHING""",
              (tid,row['channel'],row['key'],row['name'],row['url'],row['context']))
            cur.execute('SELECT * FROM desk_fb_threads WHERE id=%s FOR UPDATE',(tid,))
            c = cur.fetchone()
            cur.execute('SELECT role,content,note FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(tid,))
            old = [(kind(e['role'],e['note']),squash(e['content']) or '') for e in cur.fetchall()]
            seen = [(kind(m['role'],m.get('note','')),squash(m['text']) or '') for m in row['messages']]
            def added(role):
                now_said = said(seen,role)
                return bool(now_said) and not said(old,role).endswith(now_said[-30:])
            users, changed = added('user'), added('user') or added('page')
            state, draft, drafted, note = c['status'], c['draft'], c['drafted_version'], c['note']
            pressed = any('廣告預設問題' in (m.get('note') or '') for m in row['messages'] if m['role'] == 'user')
            if not old and not users and pressed:
                # Wayne 11/15 13:19 (勝哥, 陳天天): a lead who only pressed ad buttons still gets a person's follow-up,
                # drafted like any other (it replaced the 11/12 rule that skipped them).
                state, draft, drafted, note = 'waiting', None, None, '客人只點了廣告預設問題，請 AI 擬跟進'
            elif not old and not users:
                state, note = 'skip', '沒有客人自己打的字'
            if users:
                # Wayne 11/16: one we closed by itself (only ad buttons) comes back the moment the customer writes
                state, draft, drafted, note = 'waiting', None, None, '客人有新訊息（之前自動處理過）' if c['status']=='skip' and c.get('auto_at') else ''
                cur.execute("UPDATE desk_fb_threads SET pending_instruction='',pending_original='',pending_rule=NULL WHERE id=%s",(tid,))
                cur.execute("UPDATE desk_fb_threads SET hold='',hold_note='',hold_at=NULL,hold_by='' WHERE id=%s AND hold='customer'",(tid,))
                cur.execute("""UPDATE desk_fb_attempts SET status='expired',reason='客人有新訊息，請重新擬稿',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
            if not c['context'] and row['context'] and state in ('ready','error','blocked'):
                # The ad/post this lead came from was missing when drafting: the draft may be about the wrong product.
                state, draft, drafted, note = 'waiting', None, None, ''
            last = next((who for who,_ in reversed(seen) if who!='auto'), None)
            if changed and last=='page' and weak_after_complaint(
                    dict(role=m['role'], content=m['text'], note=m.get('note', '')) for m in row['messages']):
                last = None  # a greeting after a complaint does not close it (Wayne 11/17)
            if changed and last=='page':
                if state!='done':
                    counts['auto_closed'] += 1
                state, note = 'done', '同事已回覆'
                cur.execute("""UPDATE desk_fb_attempts SET status='cancelled',reason='同事已回覆',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
                clear_ai_hold(cur, tid)
                unstar_later(cur, tid)
            if old and len(seen)>len(old) and seen[:len(old)]==old:
                # Exactly what we had plus newer messages (always so for the webhook): add only the new ones.
                for i,msg in enumerate(row['messages'][len(old):],len(old)):
                    cur.execute("""INSERT INTO desk_fb_events(thread_id,fingerprint,role,content,at,note,event_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(thread_id,fingerprint) DO NOTHING""",
                      (tid,digest([i,msg['role'],msg['text'],msg['at']]),msg['role'],msg['text'],msg['at'],msg.get('note',''),event_time(msg['at'])))
            elif seen and old[-len(seen):]!=seen or any(o[0]!=n[0] for o,n in zip(old[-len(seen):],seen)):
                # Keep the longer history when this read simply saw fewer old messages; otherwise take the new snapshot.
                cur.execute('DELETE FROM desk_fb_events WHERE thread_id=%s',(tid,))
                for i,msg in enumerate(row['messages']):
                    cur.execute("""INSERT INTO desk_fb_events(thread_id,fingerprint,role,content,at,note,event_at)
                      VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                      (tid,digest([i,msg['role'],msg['text'],msg['at']]),msg['role'],msg['text'],msg['at'],msg.get('note',''),event_time(msg['at'])))
            cur.execute("""UPDATE desk_fb_threads SET name=%s,url=%s,context=%s,version=%s,status=%s,draft=%s,
              drafted_version=%s,note=%s,last_at=CASE WHEN %s THEN NOW() ELSE last_at END,
              draft_token=CASE WHEN %s THEN NULL ELSE draft_token END,updated_at=NOW() WHERE id=%s""",
              (row['name'],row['url'],c['context'] or row['context'],c['version']+int(users),state,
               Json(draft) if draft is not None else None,drafted,note,changed,changed,tid))
            if state in ('waiting','ready','blocked','error') and line_closable(dict(c, pending_instruction='' if users else c['pending_instruction'])):
                cur.execute('SELECT role,note,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(tid,))
                if asked_back(cur.fetchall()):  # Wayne 11/16: a colleague asked what they need; the customer is next
                    close_line(cur, dict(c, version=c['version']+int(users)), 'asked', None, (state,), False)
            counts['new_customer_messages'] += int(users)
        counts['threads'] = len({r['id'] for r in rows})
    return counts


def unstar_later(cur, tid):
    """Wayne 11/16 20:20: Meta's 持續追蹤 list kept conversations the desk had already closed (a colleague answered,
    結案 pressed, 3 days without an answer); only 不發送 and the ad-button ones were queued. The next FB/IG round
    removes the star (step 1.5 of the schedule). LINE has no star."""
    cur.execute("UPDATE desk_fb_threads SET unstar='todo' WHERE id=%s AND channel<>'line_message'", (tid,))


def clear_ai_hold(cur, tid):
    """Wayne 11/16: a 📌 the AI raised goes away once a colleague answers the customer (LINE colleagues answer in
    the LINE inbox, never press ✅ here); one Wayne marked himself stays until he clears it."""
    cur.execute("UPDATE desk_fb_threads SET hold='',hold_note='',hold_at=NULL WHERE id=%s AND hold='ours' AND hold_by<>'wayne'",(tid,))


def conversation(store, tid):
    return store.one('SELECT * FROM desk_fb_threads WHERE id=%s',(tid,))


def messages(store, tid, for_ai=False):
    return shape(store.all('SELECT * FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(tid,)))


def shape(rows):
    """One conversation's stored events (ordered by id) as the timeline shown to people and to the AI."""
    rows = list(rows)
    if rows and all(r['event_at'] for r in rows):
        rows.sort(key=lambda r:(r['event_at'],r['id']))
    handled = max((i for i,r in enumerate(rows) if answers(r['role'],r['note'])),default=-1)
    return [dict(time=r['at'],role='assistant' if r['role']=='page' else 'user',
                 text=r['content']+(' '+r['note'] if r['note'] else ''),new=i>handled and r['role']=='user',origin=r['origin'])
            for i,r in enumerate(rows)]


def status(store):
    rows = store.all('SELECT status,COUNT(*) AS n FROM desk_fb_threads GROUP BY status')
    counts = {r['status']:r['n'] for r in rows}
    return dict(waiting_draft=sum(counts.get(s,0) for s in ('waiting','drafting')),
                awaiting_review=counts.get('ready',0),
                draft_failed=sum(counts.get(s,0) for s in ('blocked','error')),
                approved_unsent=counts.get('approved',0),
                send_failed=store.one("SELECT COUNT(*) AS n FROM desk_fb_threads WHERE note LIKE '送出失敗：%%'")['n'],
                # Wayne 11/16: the 「擬不出草稿」 notice guessed 「OpenAI 餘額用完」; it now says what the desk recorded
                draft_failed_list=[dict(name=r['name'],channel=LABELS.get(r['channel'],r['channel']),reason=r['reason'] or '')
                                   for r in store.all("""SELECT name,channel,draft->>'staff_note' AS reason FROM desk_fb_threads
                                     WHERE status IN ('blocked','error') ORDER BY updated_at LIMIT 10""")],
                # Wayne 11/15: what we still have to check, for the once-a-day reminder (oldest first)
                hold_ours=[dict(name=r['name'],channel=LABELS.get(r['channel'],r['channel']),note=r['hold_note'],
                                days=int(r['days'] or 0)) for r in store.all(
                    """SELECT name,channel,hold_note,EXTRACT(DAY FROM NOW()-hold_at) AS days FROM desk_fb_threads
                      WHERE hold='ours' ORDER BY hold_at LIMIT 20""")])


# Wayne 11/17 (蕭自強): on FB/IG the check compares the customer's last typed words; a sticker or photo at the end
# made the send check fail. LINE keeps its own check (the LINE run reads the back office directly).
TYPED = r" AND btrim(e.content)<>'' AND e.content !~ '^\[(貼圖|圖片|影片|語音|檔案|位置)\]'"


def awaiting(store, line=False):
    """Conversations still open on the desk, oldest first, so the browser run can re-check that nobody has answered them."""
    rows = store.all("""SELECT t.channel,t.key,t.name,t.url,
      (SELECT content FROM desk_fb_events e WHERE e.thread_id=t.id AND e.role='user'
        AND (t.channel='line_message' OR (TRUE""" + TYPED + """)) ORDER BY e.id DESC LIMIT 1) AS last_customer_text
      FROM desk_fb_threads t WHERE t.status=ANY(%s) AND (t.channel='line_message')=%s AND NOT (t.key LIKE 'psid:%%' OR t.key LIKE 'igsid:%%')
      ORDER BY t.last_at,t.id LIMIT 60""",(list(TODO),line))
    return [dict(r,last_customer_text=r['last_customer_text'] or '',is_public=r['channel'].endswith('_comment')) for r in rows]


def pending(store, line=False):
    """Approved replies to send. The Meta run gets FB/IG only, the LINE run (line=True) LINE only.
    Conversations that came through the Meta webhook (key psid:/igsid:) are sent by desk.meta, never by the browser."""
    result = []
    with store.cursor() as cur:
        cur.execute("""SELECT t.* FROM desk_fb_threads t WHERE EXISTS(SELECT 1 FROM desk_fb_attempts a WHERE a.thread_id=t.id AND a.status='approved')
          AND (t.channel='line_message')=%s AND NOT (t.key LIKE 'psid:%%' OR t.key LIKE 'igsid:%%')
          ORDER BY (SELECT MIN(a.approved_at) FROM desk_fb_attempts a WHERE a.thread_id=t.id AND a.status='approved'),t.id
          FOR UPDATE OF t""",(line,))
        for c in cur.fetchall():
            cur.execute("SELECT * FROM desk_fb_attempts WHERE thread_id=%s AND status='approved'",(c['id'],))
            a = cur.fetchone()
            if a['source_version'] != c['version'] or c['status']!='approved':
                cur.execute("UPDATE desk_fb_attempts SET status='expired',reason='對話已更新，請重新擬稿',updated_at=NOW() WHERE retry_key=%s",(a['retry_key'],))
                if c['status'] not in ('done','skip'):
                    cur.execute("UPDATE desk_fb_threads SET status='waiting',draft=NULL,drafted_version=NULL,draft_token=NULL WHERE id=%s",(c['id'],))
                continue
            cur.execute("SELECT content FROM desk_fb_events e WHERE thread_id=%s AND role='user'"
                        + ("" if c['channel'] == 'line_message' else TYPED) + " ORDER BY id DESC LIMIT 1",(c['id'],))
            last = cur.fetchone()
            result.append(dict(attempt_id=str(a['retry_key']),channel=c['channel'],key=c['key'],name=c['name'],url=c['url'],
                content=a['content'],parts=parts(a['content']),last_customer_text=last['content'] if last else '',approved_at=a['approved_at'].isoformat(),
                is_public=c['channel'].endswith('_comment'),files=[]))
    return result


def approve(store, staff, data):
    from .knowledge import require_reviewer
    require_reviewer(staff)
    if not isinstance(data,dict) or data.get('confirmed') is not True:
        raise ActionError('請先確認收件對象及內容',400)
    tid,text,version = data.get('thread_id'),data.get('text'),data.get('version')
    if not isinstance(tid,str) or not isinstance(text,str) or not text.strip() or len(text)>2000 or type(version) is not int:
        raise ActionError('回覆內容或快照不正確，文字上限 2,000 字',400)
    pieces = parts(text)
    if len(pieces) > MAX_PARTS or any(not p.strip() for p in pieces):
        raise ActionError('最多分成 3 則，而且每一則都要有內容',400)
    if NO_REPLY in text:  # the AI's "don't answer this" marker must never reach a customer
        raise ActionError('這則 AI 建議不回覆，請按「不發送」；真的要回請改寫內容',400)
    from .links import audit, complaint
    found = audit(store, text)
    broken = [l['url'] for l in found if l['state'] == 'broken']
    if broken:
        raise ActionError('這個連結打不開，請改掉再核准：' + '、'.join(broken), 400)
    wrong = [complaint(l) for l in found if l['verdict'] == 'mismatch']
    if wrong and data.get('links_confirmed') is not True:  # Wayne 11/14: two short links pasted the wrong way round
        raise ActionError('連結和前面寫的產品對不上，請改掉；確定沒錯再勾「連結我核對過了」：' + '；'.join(wrong), 400)
    try:
        key = str(uuid.UUID(data.get('retry_key','')))
    except (ValueError,TypeError,AttributeError):
        raise ActionError('確認編號不正確',400)
    with store.cursor() as cur:
        c = locked(cur,tid,version,check_state=False)
        public = data.get('public_confirmed') is True
        if c['channel'].endswith('_comment') and len(pieces) > 1:
            raise ActionError('公開留言只能回一則，請先合併成一則',400)
        if c['channel'].endswith('_comment') and not public:
            raise ActionError('這是公開留言，所有人看得到；請勾選確認',400)
        cur.execute('SELECT * FROM desk_fb_attempts WHERE retry_key=%s',(key,))
        old = cur.fetchone()
        if old:
            if (old['thread_id'],old['staff_id'],old['content'],old['source_version'],old['public_confirmed']) != (tid,staff['id'],text,version,public):
                raise ActionError('確認快照不一致，禁止重送',409)
            if old['status'] in ('approved','sent'):
                return dict(ok=True,duplicate=True,attempt_id=key,status=old['status'])
            raise ActionError('這筆核准已取消或失效，請重新確認',409)
        if c['status'] not in ('waiting','ready','error','blocked'):
            raise ActionError('對話已更新、已核准或已處理，請重新載入',409)
        # What the customer asked and what the AI proposed: approved replies become examples for later drafts.
        cur.execute("SELECT role,content,note FROM desk_fb_events WHERE thread_id=%s ORDER BY id",(tid,))
        events = cur.fetchall()
        answered = max((i for i,e in enumerate(events) if answers(e['role'],e['note'])),default=-1)
        question = '\n'.join(e['content'] for e in events[answered+1:] if e['role']=='user' and e['content'])[:2000]
        cur.execute("""INSERT INTO desk_fb_attempts(retry_key,thread_id,staff_id,source_version,content,public_confirmed,status,question,ai_draft)
          VALUES(%s,%s,%s,%s,%s,%s,'approved',%s,%s)""",(key,tid,staff['id'],version,text,public,question,chosen(c['draft'], text)[:4000]))
        from .fb_media import bind
        bind(cur,tid,key,data.get('media_ids'),c['channel'])
        cur.execute("UPDATE desk_fb_threads SET status='approved',note='',draft_token=NULL,updated_at=NOW() WHERE id=%s",(tid,))
        if data.get('hold') is True:  # 「📌 標記待處理並核准」: the reply goes out, the thing we still owe stays in 待處理
            cur.execute("""UPDATE desk_fb_threads SET hold='ours',hold_by='wayne',hold_at=NOW(),hold_note=%s WHERE id=%s AND hold=''""",
                        (str((c['draft'] or {}).get('staff_note') or '回覆送出後還有事要處理')[:200],tid))
    return dict(ok=True,attempt_id=key,status='approved')


def chosen(draft, text):
    """The AI version Wayne started from (he can pick one of 2-3): the closest one to what he approved."""
    import difflib
    versions = [x.get('draft') or '' for x in [draft or {}] + list((draft or {}).get('alternatives') or []) if isinstance(x, dict)]
    return max(versions, key=lambda v: difflib.SequenceMatcher(None, v, text).ratio()) if versions else ''


def locked(cur, tid, version, check_state=True):
    if not isinstance(tid,str) or type(version) is not int:
        raise ActionError('請重新載入對話',400)
    cur.execute('SELECT * FROM desk_fb_threads WHERE id=%s FOR UPDATE',(tid,))
    c = cur.fetchone()
    if not c:
        raise ActionError('找不到對話',404)
    if c['version']!=version or (check_state and c['status'] not in TODO):
        raise ActionError('對話已更新或已處理，請重新載入',409)
    return c


def action(store,staff,data,kind):
    from .knowledge import require_reviewer
    require_reviewer(staff)
    if not isinstance(data,dict):
        raise ActionError('操作格式不正確',400)
    with store.cursor() as cur:
        c = locked(cur,data.get('thread_id'),data.get('version'),check_state=False)
        if kind=='cancel' and c['status']!='approved' or kind=='skip' and c['status'] not in TODO:
            raise ActionError('狀態已更新，請重新載入',409)
        if kind not in ('cancel','skip'):
            raise ActionError('操作格式不正確',400)
        cur.execute("UPDATE desk_fb_attempts SET status='cancelled',reason='Wayne 取消核准',updated_at=NOW() WHERE thread_id=%s AND status='approved'",(c['id'],))
        cur.execute("UPDATE desk_fb_threads SET status=%s,note=%s,draft_token=NULL,updated_at=NOW() WHERE id=%s",('ready' if kind=='cancel' else 'skip','取消核准' if kind=='cancel' else '不回覆',c['id']))
        if kind=='skip' and c['channel']!='line_message':  # Wayne 11/15 23:50: the next Meta run removes the 持續追蹤 star
            cur.execute("UPDATE desk_fb_threads SET unstar='todo' WHERE id=%s",(c['id'],))
    return dict(ok=True)


def to_unstar(store):
    """FB/IG conversations Wayne marked 不發送: the browser run removes Meta's 持續追蹤 star, then calls unstarred()."""
    # auto: closed by itself (only ad buttons). The run first checks the last line is a button or our automatic reply.
    return [dict(thread_id=r['id'],channel=r['channel'],key=r['key'],name=r['name'],url=r['url'],is_public=r['channel'].endswith('_comment'),
                 auto=r.get('auto_at') is not None and r['status']=='skip')
            for r in store.all("""SELECT * FROM desk_fb_threads WHERE unstar='todo' AND channel<>'line_message'
              ORDER BY updated_at LIMIT 40""")]


def unstarred(store, thread_id):
    if not store.execute("UPDATE desk_fb_threads SET unstar='done' WHERE id=%s AND unstar='todo'",(str(thread_id),)):
        raise ActionError('找不到這則待取消持續追蹤的對話',404)
    return dict(ok=True)


def record_result(store, attempt_id, outcome, reason='', via='browser'):
    if outcome not in ('sent','failed') or not isinstance(reason,str) or len(reason)>1000:
        raise ActionError('結果必須是 sent 或 failed，原因上限 1,000 字',400)
    try:
        attempt_id = str(uuid.UUID(attempt_id))
    except (ValueError,TypeError,AttributeError):
        raise ActionError('確認編號不正確',400)
    with store.cursor() as cur:
        cur.execute('SELECT thread_id FROM desk_fb_attempts WHERE retry_key=%s',(attempt_id,))
        found = cur.fetchone()
        if not found:
            raise ActionError('找不到核准紀錄',404)
        cur.execute('SELECT * FROM desk_fb_threads WHERE id=%s FOR UPDATE',(found['thread_id'],))
        c = cur.fetchone()
        cur.execute('SELECT * FROM desk_fb_attempts WHERE retry_key=%s FOR UPDATE',(attempt_id,))
        a = cur.fetchone()
        if a['status']==outcome:
            return dict(ok=True,duplicate=True,status=outcome)
        # A sent receipt is historical evidence even if cancelled/expired during browser work.
        # It must never clear a newer customer version or another approval.
        if a['status']!='approved' and outcome!='sent':
            raise ActionError('這筆核准已失效，不能改寫結果',409)
        if a['status']=='sent':
            raise ActionError('已送出紀錄不能改成失敗',409)
        cur.execute('UPDATE desk_fb_attempts SET status=%s,reason=%s,updated_at=NOW() WHERE retry_key=%s',(outcome,reason,attempt_id))
        if outcome=='sent':
            stamp = now().isoformat()
            cur.execute("""INSERT INTO desk_fb_events(thread_id,fingerprint,role,content,at,event_at,origin,source_version)
              VALUES(%s,%s,'page',%s,%s,%s,%s,%s) ON CONFLICT(thread_id,fingerprint) DO NOTHING""",
              (c['id'],'attempt:'+attempt_id,a['content'],stamp,event_time(stamp),via,a['source_version']))
            # Wayne 11/17 (Jeffery Tai): the sending round is told to remove Meta's 持續追蹤 star but sometimes does not;
            # the next FB/IG round's step 1.5 removes it as a backup (already gone: it just moves on).
            unstar_later(cur, c['id'])
            # Wayne 11/16 20:50 (ChatGPT, Gemini, Claude agreed): nothing is marked by itself any more. 「等客人回」 only
            # made the list long (a customer who writes back returns to 待審核 anyway), and he reads the AI's note on
            # every draft. 📌 待處理 holds only what he marked; the confirm box offers the mark when the AI's note
            # says something is still ours to do (see approve).
        if c['version']==a['source_version'] and a['status']=='approved' and c['status']=='approved':
            # Wayne 11/12: a colleague had already answered in Meta; leaving it in review looked like a bad draft.
            answered = outcome=='failed' and '已經有人回覆' in reason
            state = 'done' if outcome=='sent' or answered else 'ready'
            note = ('已即時送出' if via=='api' else '已由瀏覽器送出') if outcome=='sent' else '同事已回覆' if answered else '送出失敗：'+(reason or '請人工查核')
            cur.execute("UPDATE desk_fb_threads SET status=%s,note=%s,updated_at=NOW() WHERE id=%s",(state,note,c['id']))
    return dict(ok=True,status=outcome)


HOLDS = ('ours','customer')
# We told the customer we would come back to them (ChatGPT 11/15: when both sides owe something, keep it ours).
WE_OWE = re.compile(r'(確認|查詢|查好|查看|問過|詢問)[^。！？!?\n]{0,8}(後|好)[^。！？!?\n]{0,10}(回覆|回報|通知|告訴)|'
                    r'(稍後|晚點|盡快|馬上|再)(回覆|回報|通知)您')
ASKS = re.compile(r'[？?]|請問|麻煩(您)?提供|方便(提供|告訴)|請(您)?提供|請告訴我們|再告訴我們')


def hold_after_send(text):
    """'ours' when the reply promises we will come back, 'customer' when it ends asking them something, else ''."""
    text = str(text or '')
    if WE_OWE.search(text):
        return 'ours'
    tail = text.replace('[[下一則]]', '\n').strip()[-120:]
    return 'customer' if ASKS.search(tail) else ''


def hold_from(result):
    """The AI marks what needs us (to_human, 請對帳, 要確認營業日) or the customer (等客人回車款年份)."""
    note = str(result.get('staff_note') or '')
    if result.get('to_human') is True or re.search(r'對帳|確認營業|要確認|請確認|待公司確認', note):
        return 'ours'
    return 'customer' if '等客人' in note else ''


CLOSED_NOTE = 'Wayne 確認完了，結案'


def set_hold(store, staff, data):
    """Wayne marks or clears 待處理 by hand: 'ours', 'customer' or '' (✅ 確認完了，結案).
    Clearing also closes a draft still waiting for review (nothing is sent; a reply already approved still goes out)
    and returns `undo`, which 復原 sends back as `restore` within 10 seconds (Wayne 11/16 11:03)."""
    from .knowledge import require_reviewer
    require_reviewer(staff)
    if not isinstance(data,dict) or data.get('hold') not in HOLDS+('',) or not isinstance(data.get('note',''),str):
        raise ActionError('操作格式不正確',400)
    tid = str(data.get('thread_id',''))
    with store.cursor() as cur:
        cur.execute('SELECT * FROM desk_fb_threads WHERE id=%s FOR UPDATE',(tid,))
        c = cur.fetchone()
        if not c:
            raise ActionError('找不到對話',404)
        back = data.get('restore')
        if back is not None:
            if (not isinstance(back,dict) or back.get('hold') not in HOLDS or back.get('by') not in ('','wayne')
                    or not isinstance(back.get('note',''),str) or c['hold']):
                raise ActionError('已經不能復原，請重新載入',409)
            at = event_time(str(back.get('at') or '')) or now()
            cur.execute("UPDATE desk_fb_threads SET hold=%s,hold_note=%s,hold_at=%s,hold_by=%s WHERE id=%s",
                        (back['hold'],back.get('note','')[:200],at,back['by'],tid))
            if back.get('status') in TODO and c['status']=='done' and c['note']==CLOSED_NOTE:
                cur.execute("UPDATE desk_fb_threads SET status=%s,note=%s,updated_at=NOW() WHERE id=%s",(back['status'],back.get('was_note','')[:500],tid))
            return dict(ok=True)
        cur.execute("""UPDATE desk_fb_threads SET hold=%s,hold_note=%s,hold_at=CASE WHEN %s='' THEN NULL ELSE NOW() END,
          hold_by=CASE WHEN %s='' THEN '' ELSE 'wayne' END WHERE id=%s""",
          (data['hold'],data.get('note','')[:200],data['hold'],data['hold'],tid))
        undo = None
        if data['hold']=='' and c['hold']:
            undo = dict(hold=c['hold'],note=c['hold_note'],at=c['hold_at'].isoformat() if c['hold_at'] else '',by=c['hold_by'],
                        status=c['status'],was_note=c['note'])
            if c['status'] in ('ready','blocked','error'):
                cur.execute("UPDATE desk_fb_threads SET status='done',note=%s,draft_token=NULL,updated_at=NOW() WHERE id=%s",(CLOSED_NOTE,tid))
                unstar_later(cur, tid)
    return dict(ok=True,undo=undo)


THUMBS = ('[貼圖] 👍', '[貼圖] 讚')  # Wayne 11/16 11:03 (Sunny Chung): a lone 👍 after the buttons needs no answer
AWAY = re.compile(r'不在線上|無法回覆您的問題')  # the Page's after-hours message is not an answer to the button
PROMISE = re.compile(r'專人|稍後|客服人員|(會|將)(盡快|儘快|再|主動)?(與您)?(聯繫|聯絡|回電)|為您安排')
AUTO_NOTE = '🤖 自動處理：只點廣告按鈕，自動回覆已答完'
TRANSIENT_TRIES = 3


def promises(text):
    """Wayne 11/17 (ChatGPT, Gemini agreed): 「加入官方LINE，專人為您介紹」 points the customer to LINE, it does not
    promise that someone will come back to them; only the other sentences can promise a person."""
    return any(PROMISE.search(x) for x in re.split(r'[。！!？?\n]', text or '') if 'LINE' not in x.upper())


def ad_only(rows):
    """Wayne 11/16 11:03: '' unless the customer only pressed the ad's preset questions (maybe one 👍 after them),
    every press got the ad's own automatic answer that promises no person, and nobody from us wrote anything.
    Then 'buttons' or 'thumb': the thread is closed without a draft and without review."""
    # The broadcast that brought them in (「群發」「推播」) is often read back repeated between the presses
    # (張木榮 11/16 05:21): it answers nothing and asks nothing, so it is left out (Wayne 11/16).
    rows = [r for r in rows if not (r['role'] == 'page' and any(w in (r['note'] or '') for w in ('群發', '推播')))]
    users = [i for i, r in enumerate(rows) if r['role'] == 'user']
    buttons = [i for i in users if '廣告預設問題' in (rows[i]['note'] or '')]
    if not buttons:
        return ''
    thumbs = [i for i in users if i not in buttons]
    if any((rows[i]['note'] or '').strip() not in THUMBS or (rows[i]['content'] or '').strip() or i < buttons[0] for i in thumbs):
        return ''  # typed words, a photo, voice, another sticker
    if any(answers(r['role'], r['note']) for r in rows):
        return ''  # a colleague already wrote: not ours to close
    # Wayne 11/17: Meta shows the automatic replies out of order when several buttons are pressed at once (周安南), so
    # count instead of pairing: every different button pressed needs an automatic reply after the first press, and the
    # last press must have one after it (the answer may not be in yet).
    replies = [r['content'] or '' for r in rows[buttons[0]:] if r['role'] == 'page' and '自動回覆' in (r['note'] or '')
               and not AWAY.search(r['content'] or '')]
    pressed = {squash(rows[k]['content']) for k in buttons}
    after_last = any(r['role'] == 'page' and '自動回覆' in (r['note'] or '') for r in rows[buttons[-1] + 1:])
    if len(replies) < len(pressed) or not after_last or any(promises(t) for t in replies):
        return ''
    return 'thumb' if thumbs else 'buttons'


def ad_buttons_all_answered(store, thread_id):
    return bool(ad_only(store.all('SELECT role,note,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (thread_id,))))


def close_ad_only(store, c):
    """Closes one thread that is only ad buttons (see ad_only); True when it did. The version check makes a message
    arriving at this moment win: the thread then stays open for a draft."""
    if c['channel'] not in ('fb_message', 'ig_message') or c.get('hold') or c.get('no_auto') or c.get('pending_instruction'):
        return False
    why = ad_only(store.all('SELECT role,note,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (c['id'],)))
    if not why:
        return False
    note = AUTO_NOTE + ('（客人只回 👍）' if why == 'thumb' else '')
    return bool(store.execute("""UPDATE desk_fb_threads SET status='skip',note=%s,draft=%s,drafted_version=%s,draft_token=NULL,
      auto_at=NOW(),unstar='todo',updated_at=NOW() WHERE id=%s AND version=%s AND status='drafting' AND draft_token=%s""",
      (note, Json(dict(draft=NO_REPLY, flags=[], staff_note=note, auto=why)), c['version'], c['id'], c['version'], c['draft_token'])))


# Wayne 11/16 16:30 (LINE 24 vs the LINE inbox): two kinds of LINE conversation leave 待審核 by themselves.
# 'noreply': the AI says nothing needs saying (a store's phone number tapped, "I'll be late"); a 📌 it raised stays in 待處理.
# 'asked': after the customer's last message a colleague asked what they need (「請問有什麼地方需要為您服務的嗎？」,
# often after a sticker): the next move is the customer's. Nothing is sent; a new customer message reopens it (push).
ASK_BACK = re.compile(r'請問有什麼(地方)?(需要|可以)|有什麼(地方)?需要(為您|幫您|我們)?(服務|協助|幫忙)|需要什麼(協助|服務|幫忙)')
COLLEAGUE = ('', '暫回', '自動回覆')  # LINE notes for what a colleague sent (自動回覆 = a saved reply, 暫回 = a sticker)
LINE_AUTO = {'noreply': '🤖 自動處理：AI 判斷不用回覆', 'asked': '🤖 自動處理：同事已問客人需求，等客人回',
             'thanks': '🤖 自動處理：客人道謝收尾，不用回覆'}
# 'thanks' (Wayne 11/16 17:50, 自動結案不傳訊息): a colleague answered and the customer only said 好的／謝謝／a sticker.
# Of 41 such turns in the LINE history a third still had business afterwards (deposit, booking), so nothing is sent
# and it only applies when our last messages asked nothing and promised nothing, and no 📌 is on the conversation.
THANKS = re.compile(r'^(?:謝謝|感謝|感恩|多謝|辛苦了|thanks?|thx|ok+|okay|好的?|好[喔唷啊哦]|嗯+|了解|收到|知道了|沒問題'
                    r'|[您你喔囉了啦唷]|[\s~～!！。.，,、]|[👍🙏😊☺🙂😄❤️])+$', re.I)
OPEN_END = re.compile(r'[?？]|嗎|呢|稍後|稍等|確認後|確認一下|再回覆|再跟您|幫您(?:查|確認|詢問|問)|回覆您|通知您|聯[繫絡]您')


def is_thanks(row):
    text, note = (row['content'] or '').strip(), row['note'] or ''
    if text in ('', '[貼圖]'):
        return '貼圖' in text + note
    return not note and len(text) <= 12 and bool(THANKS.match(text))


def thanks_only(rows):
    rows = list(rows)
    kinds = [kind(r['role'], r['note']) for r in rows]
    i, said = len(rows) - 1, []
    while i >= 0 and kinds[i] != 'page':
        if rows[i]['role'] == 'user':
            said.append(rows[i])  # a menu tap counts too: it is not a thank-you
        elif '暫回' in (rows[i]['note'] or '') and (rows[i]['content'] or '').strip() != '[貼圖]':
            return False  # 「稍等，幫您查」: we still owe the answer
        i -= 1
    if i < 0 or not said or not all(is_thanks(r) for r in said):
        return False
    while i >= 0 and rows[i]['role'] == 'page':  # everything we said in that turn
        if kinds[i] == 'page' and OPEN_END.search(rows[i]['content'] or ''):
            return False  # we asked something (「明天 13:00 方便嗎？」→「好的」is an answer) or promised to come back
        i -= 1
    return True


# Wayne 11/17 (志宏): the customer said the install broke a part and pressed 「呼叫客服」; a colleague answered with the
# saved greeting and the desk closed it. A complaint is only answered by a real reply after it.
COMPLAINT = re.compile(r'壞了|壞掉|斷了|弄斷|刮傷|刮到|掉漆|漏水|退費|退款|退貨|客訴|投訴|失望|不滿|生氣|消保|騙|弄壞')
COMPLAINT_FLAG = '⚠ 客人在反映問題，同事只回了問候或「稍等」，請確認有接住'
WEAK = re.compile(r'^\s*(\[貼圖\]|您好[，,]?)?\s*(請問有什麼(地方)?需要|有什麼(地方)?需要|收到|好的|了解|稍等)')


def weak_after_complaint(rows):
    """True when the customer's latest messages complain and everything we said after them is a greeting or a
    holding reply (or nothing)."""
    rows = list(rows)
    users = [i for i, r in enumerate(rows) if kind(r['role'], r['note']) == 'user' or '呼叫客服' in (r['content'] or '')]
    if not users:
        return False
    i = users[-1]
    run = [rows[k] for k in users if k > i - 8]  # the customer's last few messages
    if not any(COMPLAINT.search(r['content'] or '') for r in run):
        return False
    ours = [r for r in rows[i + 1:] if r['role'] == 'page' and (r['note'] or '') in COLLEAGUE]
    return all(len((r['content'] or '').strip()) < 60 and (WEAK.search(r['content'] or '') or (r['content'] or '').strip() in ('', '[貼圖]'))
               for r in ours)


def asked_back(rows):
    rows = list(rows)
    if weak_after_complaint(rows):
        return False
    last = max((i for i, r in enumerate(rows) if kind(r['role'], r['note']) == 'user'), default=-1)
    return last >= 0 and any(r['role'] == 'page' and (r['note'] or '') in COLLEAGUE and ASK_BACK.search(r['content'] or '')
                             for r in rows[last + 1:])


def auto_closable(c):
    return not c.get('no_auto') and not c.get('pending_instruction') and c.get('hold_by') != 'wayne'


def line_closable(c):
    return c['channel'] == 'line_message' and auto_closable(c)


def close_line(cur, c, why, draft=None, states=('drafting',), token=True):
    """Closes one LINE conversation by itself (see LINE_AUTO); the version check lets a message arriving now win."""
    shown = dict(draft or dict(draft=NO_REPLY, flags=[]), auto=why, staff_note=(draft or {}).get('staff_note') or LINE_AUTO[why])
    cur.execute("""UPDATE desk_fb_threads SET status='skip',note=%s,draft=%s,drafted_version=%s,draft_token=NULL,
      auto_at=NOW(),updated_at=NOW() WHERE id=%s AND version=%s AND status=ANY(%s)"""
                + (" AND draft_token=%s" if token else '') + " RETURNING id",
                (LINE_AUTO[why], Json(shown), c['version'], c['id'], c['version'], list(states)) + ((c['draft_token'],) if token else ()))
    closed = bool(cur.fetchone())
    if closed:
        unstar_later(cur, c['id'])  # FB/IG: the next round removes Meta's 持續追蹤 star (LINE has none)
    if closed and why == 'asked':
        clear_ai_hold(cur, c['id'])  # a colleague answered (asked what they need)
    return closed


# Wayne 11/16 18:30 (兩個都做): the 「立即同步」 button and send-right-after-approve. The office PC's one-minute job
# (WL-DeskSync, scripts\win-jobs\desk_sync) starts a round; it and the scheduled rounds take the same lock per inbox,
# so two rounds never send the same approved reply.
LOCK_MINUTES = {'line': 9, 'fb': 25}  # a round that died frees itself after this long
SYNC_GAP = 180  # seconds between two presses of the button


def run_lock(store, what, owner):
    if what not in LOCK_MINUTES or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', owner or ''):
        raise ActionError('鎖的格式不正確', 400)
    with store.cursor() as cur:
        cur.execute("""INSERT INTO desk_marks(name,value,at) VALUES(%s,%s,NOW())
          ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()
          WHERE desk_marks.value NOT LIKE 'run:%%' OR desk_marks.at < NOW()-make_interval(mins => %s) RETURNING name""",
          ('run_lock_' + what, 'run:' + owner, LOCK_MINUTES[what]))
        return dict(ok=bool(cur.fetchone()))


def run_unlock(store, what, owner):
    if what not in LOCK_MINUTES:
        raise ActionError('鎖的格式不正確', 400)
    return dict(ok=bool(store.execute("UPDATE desk_marks SET value=%s WHERE name=%s AND value=%s",
                                      ('free:' + str(owner), 'run_lock_' + what, 'run:' + str(owner)))))


def run_busy(store, what):
    return bool(store.one("""SELECT 1 AS x FROM desk_marks WHERE name=%s AND value LIKE 'run:%%'
      AND at > NOW()-make_interval(mins => %s)""", ('run_lock_' + what, LOCK_MINUTES[what])))


def sync_state(store):
    """What the page shows next to 「立即同步」: idle, queued (pressed, the PC has not started yet) or running."""
    rows = {r['name']: r for r in store.all("""SELECT name,value,at,EXTRACT(EPOCH FROM NOW()-at) AS age FROM desk_marks
      WHERE name IN ('sync_request','sync_state')""")}
    try:
        done = json.loads(rows['sync_state']['value']) if 'sync_state' in rows else {}
    except ValueError:
        done = {}
    asked = rows.get('sync_request')
    running = done.get('state') == 'running' and rows['sync_state']['age'] < 45 * 60
    queued = bool(asked and asked['value'] and asked['value'] != done.get('request') and asked['age'] < 30 * 60)
    return dict(state='running' if running else 'queued' if queued else 'idle', request=asked['value'] if queued else '',
                wait=max(0, int(SYNC_GAP - asked['age'])) if asked else 0, text=str(done.get('text') or '')[:200],
                at=local_time(rows['sync_state']['at'])[5:16] if 'sync_state' in rows else '')


def request_sync(store, staff):
    from .knowledge import require_reviewer
    require_reviewer(staff)
    s = sync_state(store)
    if s['state'] != 'idle':
        raise ActionError('已經在同步了，跑完會通知你', 409)
    if s['wait']:
        raise ActionError('剛同步過，%d 秒後才能再按' % s['wait'], 429)
    store.execute("""INSERT INTO desk_marks(name,value,at) VALUES('sync_request',%s,NOW())
      ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()""", (str(uuid.uuid4()),))
    return dict(ok=True)


def reopen(store, staff, data):
    """Wayne's 「改回待審核」 on one we closed by itself: the AI writes a follow-up and it never closes by itself again."""
    from .knowledge import require_reviewer
    require_reviewer(staff)
    if not isinstance(data, dict) or not isinstance(data.get('thread_id'), str):
        raise ActionError('操作格式不正確', 400)
    if not store.execute("""UPDATE desk_fb_threads SET status='waiting',no_auto=TRUE,auto_at=NULL,draft=NULL,drafted_version=NULL,
      draft_token=NULL,pending_instruction='Wayne 把這則改回待審核：請擬一則跟進訊息',pending_original='',pending_rule=NULL,
      unstar=CASE WHEN unstar='todo' THEN '' ELSE unstar END,note='Wayne 改回待審核',updated_at=NOW()
      WHERE id=%s AND status='skip' AND auto_at IS NOT NULL""", (data['thread_id'],)):
        raise ActionError('這則已經不是自動處理的狀態，請重新載入', 409)
    return dict(ok=True, status='waiting')


def recheck(store):
    """Closed by itself in the last 7 days: the hourly browser run compares each one's last line in Meta's list.
    A customer who wrote again but whose message was opened on a phone first is no longer 「未讀」."""
    out = []
    for r in store.all("""SELECT id,channel,key,name,url FROM desk_fb_threads WHERE status='skip' AND auto_at > NOW()-INTERVAL '7 days'
      AND channel IN ('fb_message','ig_message') ORDER BY auto_at DESC LIMIT 200"""):
        pressed = [e['content'] for e in store.all("SELECT content FROM desk_fb_events WHERE thread_id=%s AND role='user' AND note LIKE '%%廣告預設問題%%' ORDER BY id", (r['id'],))]
        out.append(dict(thread_id=r['id'], channel=r['channel'], key=r['key'], name=r['name'], url=r['url'], pressed=list(dict.fromkeys(pressed))))
    return out


def voided_reply(store, c):
    """The last reply Wayne approved that was voided before sending (a newer customer message arrived) and that nothing
    we really sent has replaced since: the customer never saw it, so the next draft must say it all again.
    FB/IG only (Wayne 11/16: LINE 不要動)."""
    if c['channel'] == 'line_message':
        return ''
    a = store.one("""SELECT content,approved_at FROM desk_fb_attempts WHERE thread_id=%s AND status='expired'
      ORDER BY approved_at DESC LIMIT 1""", (c['id'],))
    if not a:
        return ''
    if store.one("SELECT 1 AS x FROM desk_fb_attempts WHERE thread_id=%s AND status='sent' AND approved_at>=%s", (c['id'], a['approved_at'])):
        return ''
    later = store.all('SELECT role,note FROM desk_fb_events WHERE thread_id=%s AND received_at>=%s', (c['id'], a['approved_at']))
    if any(answers(e['role'], e['note']) for e in later):
        return ''  # a colleague answered by hand afterwards
    return a['content'].replace(SPLIT_MARK, '\n')


def draft_thread(store, c, engine_factory=None, instruction='', original='', rule_id=None):
    from .engine import Engine, ProviderPending
    from .provider import BudgetExceeded, Unavailable, Transient, month_key
    if not instruction and close_ad_only(store, c):  # Wayne 11/16 11:03: no draft, no review
        return dict(ok=True,applied=True,status='skip',remembered=False)
    if not instruction and line_closable(c):
        rows = store.all('SELECT role,note,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (c['id'],))
        why = 'asked' if asked_back(rows) else 'thanks' if not c.get('hold') and thanks_only(rows) and not weak_after_complaint(rows) else ''
        with store.cursor() as cur:
            if why and close_line(cur, c, why):
                return dict(ok=True,applied=True,status='skip',remembered=False)
    try:
        unsent = voided_reply(store, c)
        result = (engine_factory or Engine)(store).draft(messages(store,c['id'],True),now().astimezone(TAIPEI),
            channel=c['channel'],context=c['context'],instruction=instruction,original_draft=original,
            **({'unsent': unsent} if unsent else {}))
        state = 'ready'
    except BudgetExceeded as e:
        result,state = dict(draft=original,staff_note=str(e),budget_month=month_key()),'blocked'
    except Unavailable as e:
        result,state = dict(draft=original,staff_note=str(e),retry=True),'blocked'
    except Transient:
        # OpenAI timed out or failed on its side: try again in 30 minutes, at most 3 times in a row
        tries = int((c.get('draft') or {}).get('tries') or 0) + 1
        result = dict(draft=original,tries=tries,retry=tries < TRANSIENT_TRIES,
                      staff_note='AI 這次沒有回應（OpenAI 逾時或暫時故障），30 分鐘內會自動重擬' if tries < TRANSIENT_TRIES
                      else 'AI 連續 3 次沒有回應，請手動回覆或按「請 AI 重寫」')
        state = 'blocked'
    except ProviderPending as e:
        result,state = dict(draft=original,staff_note=str(e)),'blocked'
    except Exception:
        result,state = dict(draft=original,staff_note='擬稿失敗，請重新擬稿或手動填寫'),'error'
    complaint = weak_after_complaint(store.all('SELECT role,note,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (c['id'],)))
    if complaint:  # Wayne 11/17 (志宏): stays in 待審核 with a red line, never closed by itself
        result = dict(result, flags=[COMPLAINT_FLAG] + [f for f in (result.get('flags') or []) if f != COMPLAINT_FLAG])
    with store.cursor() as cur:
        cur.execute("""UPDATE desk_fb_threads SET status=%s,draft=%s,drafted_version=%s,draft_token=NULL,
          pending_instruction='',pending_original='',pending_rule=NULL,updated_at=NOW()
          WHERE id=%s AND version=%s AND status='drafting' AND draft_token=%s RETURNING id""",
          (state,Json(result),c['version'],c['id'],c['version'],c['draft_token']))
        applied = bool(cur.fetchone())
        if rule_id and applied and state=='ready':
            cur.execute('UPDATE desk_edit_rules SET revised_draft=%s WHERE id=%s',(result['draft'],rule_id))
        # Wayne 11/17 (A): FB/IG too — 52 joke comments under the WD40 video sat in 待審核 waiting for 不發送
        if (applied and state=='ready' and not instruction and auto_closable(c) and not complaint
                and NO_REPLY in str(result.get('draft') or '') and close_line(cur, c, 'noreply', result, ('ready',), False)):
            state = 'skip'
    return dict(ok=True,applied=applied,status=state,remembered=bool(rule_id))


def revise(store,staff,data):
    """Queue a redraft with Wayne's instruction; the background drafter does the AI call."""
    from .knowledge import can_review
    if not isinstance(data,dict) or not isinstance(data.get('instruction'),str) or not 1<=len(data['instruction'].strip())<=2000:
        raise ActionError('請填寫要改哪裡，最多 2,000 字',400)
    remember = data.get('remember', False)
    if type(remember) is not bool:
        raise ActionError('記住選項格式不正確',400)
    instruction, rule_id = data['instruction'].strip(), None
    with store.cursor() as cur:
        c = locked(cur,data.get('thread_id'),data.get('version'))
        if c['status']=='drafting':
            raise ActionError('正在擬稿，請稍後再試',409)
        original = (c['draft'] or {}).get('draft','')
        if 'original_draft' in data:
            if not isinstance(data['original_draft'],str) or len(data['original_draft'])>2000:
                raise ActionError('原草稿格式不正確',400)
            original = data['original_draft']
        if remember and can_review(staff):
            cur.execute("SELECT content FROM desk_fb_events WHERE thread_id=%s AND role='user' ORDER BY id DESC LIMIT 1",(c['id'],))
            last = cur.fetchone()
            cur.execute("""INSERT INTO desk_edit_rules(instruction,channel,conversation_summary,original_draft,created_by)
              VALUES(%s,%s,%s,%s,%s) RETURNING id""",(instruction,c['channel'],last['content'][:1000] if last else '',original,staff['id']))
            rule_id = cur.fetchone()['id']
        cur.execute("""UPDATE desk_fb_threads SET status='waiting',pending_instruction=%s,pending_original=%s,pending_rule=%s,
          draft_token=NULL,note='',updated_at=NOW() WHERE id=%s""",(instruction,original,rule_id,c['id']))
    return dict(ok=True,status='waiting',remembered=bool(rule_id),rule_id=rule_id)


def draft_once(store,engine_factory=None):
    from .provider import month_key
    with store.lock('desk-fb-draft') as acquired:
        if not acquired:
            return
        store.execute("""UPDATE desk_fb_threads SET status='waiting',draft_token=NULL WHERE
          (status='drafting' AND draft_started_at<NOW()-INTERVAL '10 minutes') OR
          (status='blocked' AND draft->>'budget_month' IS NOT NULL AND draft->>'budget_month'<>%s) OR
          (status='blocked' AND draft->>'retry'='true' AND updated_at<NOW()-INTERVAL '30 minutes')""",(month_key(),))
        # Webhook conversations arrive message by message: wait for a quiet minute instead of drafting every line.
        for c in store.all("""SELECT * FROM desk_fb_threads WHERE status='waiting' AND (NOT (key LIKE 'psid:%%' OR key LIKE 'igsid:%%')
          OR last_at < NOW()-INTERVAL '60 seconds') ORDER BY last_at LIMIT 20"""):
            c['draft_token'] = str(uuid.uuid4())
            if store.execute("UPDATE desk_fb_threads SET status='drafting',draft_token=%s,draft_started_at=NOW() WHERE id=%s AND version=%s AND status='waiting'",(c['draft_token'],c['id'],c['version'])):
                draft_thread(store,c,engine_factory,c['pending_instruction'],c['pending_original'],c['pending_rule'])


def close_stale_waits(store):
    """Wayne 11/15 22:06: ⏳ 等客人回 with no answer for 3 days closes itself. Nothing is sent; a reply queued
    to send (approved) or a draft being written is left alone. A customer who writes back clears the hold before this."""
    return store.execute("""UPDATE desk_fb_threads SET hold='',hold_note='',hold_at=NULL,hold_by='',
      unstar=CASE WHEN channel<>'line_message' THEN 'todo' ELSE unstar END,
      status=CASE WHEN status IN ('ready','blocked','error') THEN 'done' ELSE status END,
      note='等客人回 3 天沒回，自動結案',updated_at=NOW()
      WHERE hold='customer' AND hold_at < NOW()-INTERVAL '3 days' AND status NOT IN ('approved','waiting','drafting')""")


REDRAFT_AT = (9, 30)            # Taipei, Monday to Saturday, before the shop opens
REDRAFT_RESERVE = Decimal('5')  # always leave this much of the month's AI budget for new customers
REDRAFT_EACH = Decimal('0.04')  # generous cost of one redraft (three versions)


def daily_redraft(store, at=None):
    """Wayne 11/13: every morning, redraft every draft still waiting for him, using everything learned since
    (his approvals and edits, kept direct replies, active rules). The old draft stays as the fallback if the AI fails.
    Only as many as the month's remaining budget allows, newest customers first."""
    from .provider import budget_status
    at = (at or now()).astimezone(TAIPEI)
    if at.weekday() == 6 or (at.hour, at.minute) < REDRAFT_AT:
        return 0
    day = at.strftime('%Y-%m-%d')
    with store.lock('desk-daily-redraft') as acquired:
        if not acquired or store.one("SELECT 1 AS x FROM desk_marks WHERE name='daily_redraft' AND value=%s", (day,)):
            return 0
        b = budget_status(store)
        room = Decimal(b['limit_usd']) - Decimal(b['usd']) - Decimal(b['reserved_usd']) - REDRAFT_RESERVE
        n = max(0, int(room / REDRAFT_EACH))
        done = 0
        for r in store.all("SELECT id,draft FROM desk_fb_threads WHERE status='ready' ORDER BY last_at DESC LIMIT %s", (n,)):
            done += store.execute("""UPDATE desk_fb_threads SET status='waiting',draft=NULL,drafted_version=NULL,draft_token=NULL,
              pending_instruction='',pending_original=%s,pending_rule=NULL,note='早上依最新學習重擬' WHERE id=%s AND status='ready'""",
              (((r['draft'] or {}).get('draft') or ''), r['id']))
        store.execute("""INSERT INTO desk_marks(name,value) VALUES('daily_redraft',%s)
          ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()""", (day,))
        return done


def start_worker(store):
    from . import meta
    def drafter():
        try:
            meta.publish_mode(store)  # tells the browser tool on Wayne's PC who handles private messages
        except Exception:
            pass
        while True:
            jobs = [lambda: meta.process(store)] if meta.mode()!='off' else []
            for job in jobs + [lambda: meta.sweep(store), lambda: draft_once(store)]:
                try:
                    job()
                except Exception:
                    pass
            time.sleep(5)
    def learner():
        # Replies typed directly in FB/IG/LINE: one small batch every 10 minutes, only the better ones are kept.
        from .learning import judge_once, BATCH
        while True:
            busy = False
            try:
                from .cases import refresh_if_due
                refresh_if_due(store)  # the website's install-case index, about once a day
                from .videos import refresh_if_due as videos_due
                videos_due(store)  # new uploads on our YouTube channel
            except Exception:
                pass
            try:
                daily_redraft(store)  # 09:30 Mon-Sat; the drafter thread does the AI calls
            except Exception:
                pass
            try:
                close_stale_waits(store)  # ⏳ 等客人回 older than 3 days
            except Exception:
                pass
            try:
                busy = judge_once(store) >= BATCH  # a backlog: keep going every minute until it is cleared
            except Exception:
                pass
            time.sleep(60 if busy else 600)
    threading.Thread(target=drafter,daemon=True,name='desk-fb-drafter').start()
    threading.Thread(target=learner,daemon=True,name='desk-learner').start()
