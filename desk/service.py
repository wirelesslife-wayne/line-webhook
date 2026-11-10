"""Authenticated action service. All external sends go through LineNotifier."""
from datetime import timedelta
import uuid
from psycopg2.extras import Json
from .db import now
from .errors import ActionError
from . import media


def send(store, notifier, staff, data):
    uid = data.get('user_id')
    text = data.get('text')
    images = data.get('images',[])
    tokens = data.get('media',[])
    try:
        key = str(uuid.UUID(data.get('retry_key','')))
    except (ValueError,TypeError,AttributeError):
        raise ActionError('請重新開啟送出確認框',400)
    if not isinstance(uid,str) or not isinstance(text,str) or not text.strip() or len(text)>5000:
        raise ActionError('回覆需有 1～5000 個字元',400)
    if (not isinstance(tokens,list) or len(tokens)>4 or len(set(t for t in tokens if isinstance(t,str)))!=len(tokens)
            or any(not isinstance(t,str) or not media.TOKEN.fullmatch(t) for t in tokens)
            or not isinstance(images,list) or len(images)>4 or any(not isinstance(u,str) for u in images)
            or (tokens and images)):
        raise ActionError('附件資料不正確，最多 4 個檔案',400)
    with store.lock('desk-send:'+uid) as acquired:
        if not acquired:
            raise ActionError('這位客人正在傳送中，請稍候')
        try:
            expected = int(data.get('source_upto'))
        except (TypeError,ValueError):
            raise ActionError('請重新載入對話',400)
        old = store.one('SELECT * FROM desk_send_attempts WHERE retry_key=%s',(key,))
        if old:
            old_tokens = [i['id'] for i in old['images'] if isinstance(i,dict)]
            same_media = old_tokens==tokens and (not images if old_tokens else old['images']==images)
            if old['user_id']!=uid or old['staff_id']!=staff['id'] or old['content']!=text or not same_media or old['source_upto']!=expected:
                raise ActionError('此確認編號已用於不同內容，請先查核原傳送結果')
            images = old['images']
            if old['status']=='sent':
                return {'ok':True,'already_sent':True}
            if old['status'] not in ('sending','unknown'):
                raise ActionError('此確認編號已結束，請重新開啟確認框')
            if old['created_at']<now()-timedelta(hours=23):
                raise ActionError('此傳送超過安全重試時間，請先到 LINE 後台確認，勿重送')
        elif images:
            raise ActionError('商品圖片建議已停用，請改用加入圖片／影片',400)
        ambiguous = store.one("SELECT retry_key FROM desk_send_attempts WHERE user_id=%s AND status IN ('sending','unknown') ORDER BY created_at DESC LIMIT 1",(uid,))
        if ambiguous and str(ambiguous['retry_key'])!=key:
            raise ActionError('此客人有一筆傳送結果待確認；請先以原確認框重試或到 LINE 後台查核')
        recent = store.one('SELECT updated_at FROM desk_send_attempts WHERE user_id=%s ORDER BY updated_at DESC LIMIT 1',(uid,))
        if recent and (now()-recent['updated_at']).total_seconds()<10:
            raise ActionError('每位客人需間隔 10 秒才能再送出',429)
        with store.conversation_transaction(uid) as (cur,c):
            if not c:
                raise ActionError('找不到對話',404)
            if not old:
                if expected <= c['handled_upto']:
                    raise ActionError('這批訊息已處理，請勿重複回覆')
                if c['last_source_id']!=expected or store.latest_source_cursor(cur,uid)!=expected:
                    raise ActionError('客人有新訊息，請看完再送出')
                images = media.resolve(cur,staff,uid,tokens)
                cur.execute('''INSERT INTO desk_send_attempts
                  (retry_key,user_id,staff_id,content,images,source_upto,status) VALUES(%s,%s,%s,%s,%s,%s,'sending')''',
                  (key,uid,staff['id'],text,Json(images),expected))
                if tokens:
                    cur.execute('UPDATE desk_media SET retry_key=%s WHERE id=ANY(%s)',(key,tokens))
            else:
                if tokens:
                    media.media_lock(cur)
                    cur.execute('''SELECT COUNT(*) AS n FROM desk_media WHERE id=ANY(%s) AND retry_key=%s
                      AND data IS NOT NULL AND removed_at IS NULL AND uploaded_at > NOW()-INTERVAL '30 days' ''',(tokens,key))
                    if cur.fetchone()['n'] != len(tokens):
                        raise ActionError('原確認附件已失效，請到 LINE 後台查核，勿另送',409)
                cur.execute("UPDATE desk_send_attempts SET status='sending',updated_at=NOW() WHERE retry_key=%s",(key,))
        upto = expected
        try:
            result = notifier.push(uid,text,images,key)
        except Exception:
            result = {'ok':False,'unknown':True,'status':0,'reason':'傳送結果待確認，請保留原確認編號並到 LINE 後台查核'}
        status = 'sent' if result['ok'] else ('unknown' if result.get('unknown') else 'failed')
        # Durable intent precedes network. Success + conversation change are atomic.
        with store.conversation_transaction(uid) as (cur,c):
            cur.execute('UPDATE desk_send_attempts SET status=%s,http_status=%s,reason=%s,updated_at=NOW() WHERE retry_key=%s',
                (status,result['status'],result['reason'],key))
            if result['ok']:
                if tokens:
                    media.media_lock(cur)
                    cur.execute('UPDATE desk_media SET sent_at=NOW() WHERE id=ANY(%s) AND retry_key=%s',(tokens,key))
                cur.execute('''INSERT INTO desk_outgoing(retry_key,user_id,staff_id,content,images,http_status)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(retry_key) DO NOTHING''',(key,uid,staff['id'],text,Json(images),result['status']))
                cur.execute('''UPDATE desk_conversations SET handled_upto=GREATEST(handled_upto,%s),
                  status=CASE WHEN last_source_id=%s AND %s=%s THEN 'done' ELSE 'waiting' END,updated_at=NOW() WHERE user_id=%s''',(upto,upto,store.latest_source_cursor(cur,uid),upto,uid))
        if not result['ok']:
            raise ActionError(result['reason'],502)
        return {'ok':True}


def mark_done(store,staff,uid,upto):
    with store.lock('desk-send:'+uid) as acquired:
        if not acquired:
            raise ActionError('此對話正在傳送中，請稍後再標記')
        with store.conversation_transaction(uid) as (cur,c):
            if not c:
                raise ActionError('找不到對話',404)
            if upto!=c['last_source_id'] or store.latest_source_cursor(cur,uid)!=upto:
                raise ActionError('客人有新訊息，請看完再標記')
            cur.execute("""UPDATE desk_conversations SET status='done',handled_upto=%s,updated_at=NOW()
                WHERE user_id=%s AND last_source_id=%s AND handled_upto=%s""",(upto,uid,upto,c['handled_upto']))
            if cur.rowcount != 1:
                raise ActionError('客人有新訊息，請看完再標記')
            cur.execute("INSERT INTO desk_actions(user_id,staff_id,action) VALUES(%s,%s,'manual_done')",(uid,staff['id']))
            # Explicit staff confirmation also resolves ambiguous attempts; no outgoing is invented.
            cur.execute("UPDATE desk_send_attempts SET status='manually_checked',updated_at=NOW() WHERE user_id=%s AND status IN ('sending','unknown')",(uid,))


def redraft(store,staff,uid,upto):
    with store.lock('desk-send:'+uid) as acquired:
        if not acquired:
            raise ActionError('此對話正在傳送中，請稍候')
        with store.conversation_transaction(uid) as (cur,c):
            if not c or c['status'] in ('done','skip','drafting') or c['handled_upto'] >= c['last_source_id']:
                raise ActionError('目前狀態無法重新擬稿，請重新載入對話')
            if upto != c['last_source_id'] or store.latest_source_cursor(cur,uid) != upto:
                raise ActionError('客人有新訊息，請重新載入對話')
            cur.execute("SELECT retry_key FROM desk_send_attempts WHERE user_id=%s AND status IN ('sending','unknown')",(uid,))
            if cur.fetchone():
                raise ActionError('請先查核待確認的傳送')
            cur.execute("UPDATE desk_conversations SET status='waiting',draft=NULL,drafted_upto=NULL,updated_at=NOW() WHERE user_id=%s AND last_source_id=%s AND status=%s",(uid,upto,c['status']))
            if cur.rowcount != 1:
                raise ActionError('對話已更新，請重新載入')
            cur.execute("INSERT INTO desk_actions(user_id,staff_id,action) VALUES(%s,%s,'redraft')",(uid,staff['id']))
