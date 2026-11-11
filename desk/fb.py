"""Browser-only ingestion, drafts and approval receipts. No outbound transport."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import threading
import time
import uuid
from psycopg2.extras import Json
from .db import now
from .errors import ActionError
from .worker import local_time, TAIPEI

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
            cur.execute('SELECT role,content FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(tid,))
            old = [(e['role'],squash(e['content']) or '') for e in cur.fetchall()]
            seen = [(m['role'],squash(m['text']) or '') for m in row['messages']]
            def added(role):
                now_said = said(seen,role)
                return bool(now_said) and not said(old,role).endswith(now_said[-30:])
            users, changed = added('user'), added('user') or added('page')
            state, draft, drafted, note = c['status'], c['draft'], c['drafted_version'], c['note']
            if users:
                state, draft, drafted, note = 'waiting', None, None, ''
                cur.execute("UPDATE desk_fb_threads SET pending_instruction='',pending_original='',pending_rule=NULL WHERE id=%s",(tid,))
                cur.execute("""UPDATE desk_fb_attempts SET status='expired',reason='客人有新訊息，請重新擬稿',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
            if changed and seen[-1][0]=='page':
                if state!='done':
                    counts['auto_closed'] += 1
                state, note = 'done', '同事已回覆'
                cur.execute("""UPDATE desk_fb_attempts SET status='cancelled',reason='同事已回覆',updated_at=NOW()
                  WHERE thread_id=%s AND status='approved'""",(tid,))
            if seen and old[-len(seen):]!=seen:
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
    rows = store.all('SELECT * FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(tid,))
    if rows and all(r['event_at'] for r in rows):
        rows.sort(key=lambda r:(r['event_at'],r['id']))
    handled = max((i for i,r in enumerate(rows) if r['role']=='page'),default=-1)
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
                content=a['content'],last_customer_text=last['content'] if last else '',approved_at=a['approved_at'].isoformat(),
                is_public=c['channel'].endswith('_comment')))
    return result


def approve(store, staff, data):
    from .knowledge import require_reviewer
    require_reviewer(staff)
    if not isinstance(data,dict) or data.get('confirmed') is not True:
        raise ActionError('請先確認收件對象及內容',400)
    tid,text,version = data.get('thread_id'),data.get('text'),data.get('version')
    if not isinstance(tid,str) or not isinstance(text,str) or not text.strip() or len(text)>2000 or type(version) is not int:
        raise ActionError('回覆內容或快照不正確，文字上限 2,000 字',400)
    try:
        key = str(uuid.UUID(data.get('retry_key','')))
    except (ValueError,TypeError,AttributeError):
        raise ActionError('確認編號不正確',400)
    with store.cursor() as cur:
        c = locked(cur,tid,version,check_state=False)
        public = data.get('public_confirmed') is True
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
        cur.execute("""INSERT INTO desk_fb_attempts(retry_key,thread_id,staff_id,source_version,content,public_confirmed,status)
          VALUES(%s,%s,%s,%s,%s,%s,'approved')""",(key,tid,staff['id'],version,text,public))
        cur.execute("UPDATE desk_fb_threads SET status='approved',note='',draft_token=NULL,updated_at=NOW() WHERE id=%s",(tid,))
    return dict(ok=True,attempt_id=key,status='approved')


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
            cur.execute("UPDATE desk_fb_threads SET status=%s,note=%s,updated_at=NOW() WHERE id=%s",
                ('done' if outcome=='sent' else 'ready','已由瀏覽器送出' if outcome=='sent' else '送出失敗：'+(reason or '請人工查核'),c['id']))
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
    remember = data.get('remember', True)
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


def start_worker(store):
    def drafter():
        while True:
            try:
                draft_once(store)
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=drafter,daemon=True,name='desk-fb-drafter').start()
