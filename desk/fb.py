"""Browser-only ingestion, drafts and approval receipts. No outbound transport."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
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


def parts(text):
    return [p.strip('\n') for p in str(text or '').split(SPLIT_MARK)]

CHANNELS = ('fb_message', 'ig_message', 'fb_comment', 'ig_comment')
LABELS = dict(zip(CHANNELS, ('FB 私訊', 'IG 私訊', 'FB 留言', 'IG 留言')))
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
        return 'auto' if '廣告預設問題' in (note or '') else 'user'
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
            if not old and not users:
                # First sight of a conversation in which the customer only pressed ad buttons.
                state, note = 'skip', '客人只點了廣告預設問題，自動回覆已回答'
            if users:
                state, draft, drafted, note = 'waiting', None, None, ''
                cur.execute("UPDATE desk_fb_threads SET pending_instruction='',pending_original='',pending_rule=NULL WHERE id=%s",(tid,))
                cur.execute("""UPDATE desk_fb_attempts SET status='expired',reason='客人有新訊息，請重新擬稿',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
            if not c['context'] and row['context'] and state in ('ready','error','blocked'):
                # The ad/post this lead came from was missing when drafting: the draft may be about the wrong product.
                state, draft, drafted, note = 'waiting', None, None, ''
            last = next((who for who,_ in reversed(seen) if who!='auto'), None)
            if changed and last=='page':
                if state!='done':
                    counts['auto_closed'] += 1
                state, note = 'done', '同事已回覆'
                cur.execute("""UPDATE desk_fb_attempts SET status='cancelled',reason='同事已回覆',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
            if seen and old[-len(seen):]!=seen or any(o[0]!=n[0] for o,n in zip(old[-len(seen):],seen)):
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
            counts['new_customer_messages'] += int(users)
        counts['threads'] = len({r['id'] for r in rows})
    return counts


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
                send_failed=store.one("SELECT COUNT(*) AS n FROM desk_fb_threads WHERE note LIKE '送出失敗：%%'")['n'])


def awaiting(store):
    """Conversations still open on the desk, oldest first, so the browser run can re-check that nobody has answered them."""
    rows = store.all("""SELECT t.channel,t.key,t.name,t.url,
      (SELECT content FROM desk_fb_events e WHERE e.thread_id=t.id AND e.role='user' ORDER BY e.id DESC LIMIT 1) AS last_customer_text
      FROM desk_fb_threads t WHERE t.status=ANY(%s) ORDER BY t.last_at,t.id LIMIT 60""",(list(TODO),))
    return [dict(r,last_customer_text=r['last_customer_text'] or '',is_public=r['channel'].endswith('_comment')) for r in rows]


def pending(store):
    result = []
    with store.cursor() as cur:
        cur.execute("SELECT t.* FROM desk_fb_threads t WHERE EXISTS(SELECT 1 FROM desk_fb_attempts a WHERE a.thread_id=t.id AND a.status='approved') ORDER BY t.id FOR UPDATE")
        for c in cur.fetchall():
            cur.execute("SELECT * FROM desk_fb_attempts WHERE thread_id=%s AND status='approved'",(c['id'],))
            a = cur.fetchone()
            if a['source_version'] != c['version'] or c['status']!='approved':
                cur.execute("UPDATE desk_fb_attempts SET status='expired',reason='對話已更新，請重新擬稿',updated_at=NOW() WHERE retry_key=%s",(a['retry_key'],))
                if c['status'] not in ('done','skip'):
                    cur.execute("UPDATE desk_fb_threads SET status='waiting',draft=NULL,drafted_version=NULL,draft_token=NULL WHERE id=%s",(c['id'],))
                continue
            cur.execute("SELECT content FROM desk_fb_events WHERE thread_id=%s AND role='user' ORDER BY id DESC LIMIT 1",(c['id'],))
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
    from .links import check as check_links
    broken = [l['url'] for l in check_links(store, text) if l['state'] == 'broken']
    if broken:
        raise ActionError('這個連結打不開，請改掉再核准：' + '、'.join(broken), 400)
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
        bind(cur,tid,key,data.get('media_ids'),c['channel'].endswith('_comment'))
        cur.execute("UPDATE desk_fb_threads SET status='approved',note='',draft_token=NULL,updated_at=NOW() WHERE id=%s",(tid,))
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
    return dict(ok=True)


def record_result(store, attempt_id, outcome, reason=''):
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
              VALUES(%s,%s,'page',%s,%s,%s,'browser',%s) ON CONFLICT(thread_id,fingerprint) DO NOTHING""",
              (c['id'],'attempt:'+attempt_id,a['content'],stamp,event_time(stamp),a['source_version']))
        if c['version']==a['source_version'] and a['status']=='approved' and c['status']=='approved':
            # Wayne 11/12 (阮海燕): a colleague had already answered in Meta; leaving it in review looked like a bad draft.
            answered = outcome=='failed' and '已經有人回覆' in reason
            state = 'done' if outcome=='sent' or answered else 'ready'
            note = '已由瀏覽器送出' if outcome=='sent' else '同事已回覆' if answered else '送出失敗：'+(reason or '請人工查核')
            cur.execute("UPDATE desk_fb_threads SET status=%s,note=%s,updated_at=NOW() WHERE id=%s",(state,note,c['id']))
    return dict(ok=True,status=outcome)


def draft_thread(store, c, engine_factory=None, instruction='', original='', rule_id=None):
    from .engine import Engine, ProviderPending
    from .provider import BudgetExceeded, Unavailable, month_key
    try:
        result = (engine_factory or Engine)(store).draft(messages(store,c['id'],True),now().astimezone(TAIPEI),
            channel=c['channel'],context=c['context'],instruction=instruction,original_draft=original)
        state = 'ready'
    except BudgetExceeded as e:
        result,state = dict(draft=original,staff_note=str(e),budget_month=month_key()),'blocked'
    except Unavailable as e:
        result,state = dict(draft=original,staff_note=str(e),retry=True),'blocked'
    except ProviderPending as e:
        result,state = dict(draft=original,staff_note=str(e)),'blocked'
    except Exception:
        result,state = dict(draft=original,staff_note='擬稿失敗，請重新擬稿或手動填寫'),'error'
    with store.cursor() as cur:
        cur.execute("""UPDATE desk_fb_threads SET status=%s,draft=%s,drafted_version=%s,draft_token=NULL,
          pending_instruction='',pending_original='',pending_rule=NULL,updated_at=NOW()
          WHERE id=%s AND version=%s AND status='drafting' AND draft_token=%s RETURNING id""",
          (state,Json(result),c['version'],c['id'],c['version'],c['draft_token']))
        applied = bool(cur.fetchone())
        if rule_id and applied and state=='ready':
            cur.execute('UPDATE desk_edit_rules SET revised_draft=%s WHERE id=%s',(result['draft'],rule_id))
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
        for c in store.all("SELECT * FROM desk_fb_threads WHERE status='waiting' ORDER BY last_at LIMIT 20"):
            c['draft_token'] = str(uuid.uuid4())
            if store.execute("UPDATE desk_fb_threads SET status='drafting',draft_token=%s,draft_started_at=NOW() WHERE id=%s AND version=%s AND status='waiting'",(c['draft_token'],c['id'],c['version'])):
                draft_thread(store,c,engine_factory,c['pending_instruction'],c['pending_original'],c['pending_rule'])


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
    def drafter():
        while True:
            try:
                draft_once(store)
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
            except Exception:
                pass
            try:
                daily_redraft(store)  # 09:30 Mon-Sat; the drafter thread does the AI calls
            except Exception:
                pass
            try:
                busy = judge_once(store) >= BATCH  # a backlog: keep going every minute until it is cleared
            except Exception:
                pass
            time.sleep(60 if busy else 600)
    threading.Thread(target=drafter,daemon=True,name='desk-fb-drafter').start()
    threading.Thread(target=learner,daemon=True,name='desk-learner').start()
