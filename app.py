import os
import json
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort
from datetime import datetime

app = Flask(__name__)

# 公司客服 OA (WirelessLife 無限) — 既有監控用
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')

# 徐助理 OA (Claude → Wayne 個人通知頻道)
LINE_XU_CHANNEL_SECRET = os.environ.get('LINE_XU_CHANNEL_SECRET', '')
LINE_XU_ACCESS_TOKEN = os.environ.get('LINE_XU_ACCESS_TOKEN', '')

# 共用密鑰，授權 push-marketing 呼叫
PUSH_AUTH_TOKEN = os.environ.get('PUSH_AUTH_TOKEN', '')

messages = []


# ---- PostgreSQL 持久化（向下相容：DB 不可用時自動 fallback 回記憶體模式）----
DATABASE_URL = os.environ.get('DATABASE_URL', '')

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

_db_ready = False


def _db_enabled():
    return bool(DATABASE_URL) and _PSYCOPG2_AVAILABLE


def _get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    """啟動時確保資料表存在。任何失敗都只記 log、不影響服務（改用記憶體模式）。"""
    global _db_ready
    if not _db_enabled():
        print('[DB] DATABASE_URL 未設定或 psycopg2 不可用 — 使用記憶體模式')
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id            BIGSERIAL PRIMARY KEY,
                    time_str      TEXT NOT NULL,
                    oa            TEXT,
                    event_type    TEXT,
                    user_id       TEXT,
                    display_name  TEXT,
                    message_type  TEXT,
                    content       TEXT,
                    raw_timestamp BIGINT,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_messages_time_str ON messages(time_str)')
        conn.close()
        _db_ready = True
        print('[DB] 連線成功，messages 資料表已就緒')
    except Exception as e:
        _db_ready = False
        print('[DB] 初始化失敗，改用記憶體模式：' + repr(e))


def db_insert(time_str, oa, event_type, user_id, display_name, message_type, content, raw_timestamp):
    """寫入一筆訊息。失敗只記 log、不丟例外（避免 webhook 回非 200 導致 LINE 重送）。"""
    if not _db_ready:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO messages
                   (time_str, oa, event_type, user_id, display_name, message_type, content, raw_timestamp)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                (time_str, oa, event_type, user_id, display_name, message_type, content, raw_timestamp)
            )
        conn.close()
    except Exception as e:
        print('[DB] 寫入失敗：' + repr(e))


def _row_to_entry(row):
    """把 DB 一列還原成與記憶體模式「完全相同」的 dict 格式（含 key 順序）。"""
    if row['oa']:
        entry = {
            'time': row['time_str'],
            'oa': row['oa'],
            'event_type': row['event_type'],
            'user_id': row['user_id'],
        }
        if row['message_type'] is not None:
            entry['msg_type'] = row['message_type']
            entry['content'] = row['content']
        return entry
    return {
        'time': row['time_str'],
        'user': row['display_name'],
        'user_id': row['user_id'],
        'msg': row['content'],
        'type': row['message_type'],
    }


def db_fetch(limit, since, until, oa):
    """從 DB 查詢，回傳與記憶體模式相同格式的 list；DB 不可用時回 None（呼叫端 fallback）。"""
    if not _db_ready:
        return None
    try:
        clauses = []
        params = []
        if oa:
            clauses.append('oa = %s')
            params.append(oa)
        if since:
            clauses.append('time_str >= %s')
            params.append(since)
        if until:
            clauses.append('time_str <= %s')
            params.append(until)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        # 取最後 limit 筆（依插入順序），對齊原本記憶體模式的 result[-limit:]
        sql = ('SELECT * FROM (SELECT * FROM messages' + where +
               ' ORDER BY id DESC LIMIT %s) t ORDER BY id ASC')
        params.append(limit)
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()
        return [_row_to_entry(r) for r in rows]
    except Exception as e:
        print('[DB] 查詢失敗，fallback 記憶體：' + repr(e))
        return None


def verify_signature(body, signature, secret):
    hash_val = hmac.new(
        secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    return base64.b64encode(hash_val).decode('utf-8') == signature


def get_user_profile(user_id, token=None):
    if token is None:
        token = LINE_CHANNEL_ACCESS_TOKEN
    url = 'https://api.line.me/v2/bot/profile/' + user_id
    headers = {'Authorization': 'Bearer ' + token}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.ok:
            return res.json().get('displayName', 'Unknown')
    except Exception:
        pass
    return 'Unknown'


@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()
    if not verify_signature(body, signature, LINE_CHANNEL_SECRET):
        abort(400)
    data = json.loads(body)
    for event in data.get('events', []):
        if event.get('type') == 'message':
            user_id = event['source'].get('userId', 'unknown')
            ts = datetime.fromtimestamp(event['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
            message = event.get('message', {})
            msg_type = message.get('type', '')
            if msg_type == 'text':
                content = message.get('text', '')
            elif msg_type == 'image':
                content = '[image]'
            elif msg_type == 'sticker':
                content = '[sticker]'
            else:
                content = '[' + msg_type + ']'
            display_name = get_user_profile(user_id)
            entry = {
                'time': ts,
                'user': display_name,
                'user_id': user_id,
                'msg': content,
                'type': msg_type
            }
            messages.append(entry)
            print(json.dumps(entry, ensure_ascii=False))
            db_insert(ts, None, 'message', user_id, display_name, msg_type, content,
                      event.get('timestamp'))
    return 'OK', 200


@app.route('/webhook-xu', methods=['POST'])
def webhook_xu():
    """徐助理 OA webhook — 用來抓取 Wayne 的 userId 跟未來互動訊息"""
    if not LINE_XU_CHANNEL_SECRET:
        return 'XU webhook not configured', 500
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()
    if not verify_signature(body, signature, LINE_XU_CHANNEL_SECRET):
        abort(400)
    data = json.loads(body)
    for event in data.get('events', []):
        user_id = event.get('source', {}).get('userId', 'unknown')
        event_type = event.get('type', '')
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        entry = {
            'time': ts,
            'oa': 'xu_assistant',
            'event_type': event_type,
            'user_id': user_id,
        }
        if event_type == 'message':
            msg = event.get('message', {})
            mt = msg.get('type', '')
            entry['msg_type'] = mt
            if mt == 'text':
                entry['content'] = msg.get('text', '')
            else:
                entry['content'] = '[' + mt + ']'
        messages.append(entry)
        print('[XU]', json.dumps(entry, ensure_ascii=False))
        db_insert(ts, 'xu_assistant', event_type, user_id, None,
                  entry.get('msg_type'), entry.get('content'), event.get('timestamp'))
    return 'OK', 200


@app.route('/push-marketing', methods=['POST'])
def push_marketing():
    """讓 Cowork 排程任務 POST 進來，由本服務透過徐助理 OA 推送給 Wayne"""
    auth = request.headers.get('X-Push-Token', '')
    if not PUSH_AUTH_TOKEN or auth != PUSH_AUTH_TOKEN:
        abort(401)
    if not LINE_XU_ACCESS_TOKEN:
        return 'XU OA not configured', 500

    data = request.get_json(silent=True) or {}
    msgs = data.get('messages')
    if not msgs:
        text = (data.get('text') or '').strip()
        if not text:
            return json.dumps({'error': 'missing messages or text'}), 400, {'Content-Type': 'application/json'}
        msgs = [{'type': 'text', 'text': text}]

    # 預設用 broadcast (徐助理 OA 只有 Wayne 一個好友)
    # 如果有指定 to_user_id，改用 push API
    to_user_id = data.get('to_user_id')
    if to_user_id:
        url = 'https://api.line.me/v2/bot/message/push'
        payload = {'to': to_user_id, 'messages': msgs}
    else:
        url = 'https://api.line.me/v2/bot/message/broadcast'
        payload = {'messages': msgs}

    res = requests.post(
        url,
        headers={
            'Authorization': 'Bearer ' + LINE_XU_ACCESS_TOKEN,
            'Content-Type': 'application/json'
        },
        json=payload,
        timeout=10
    )
    result = {'status': res.status_code, 'response': res.text}
    return json.dumps(result, ensure_ascii=False), res.status_code, {'Content-Type': 'application/json'}


@app.route('/messages', methods=['GET'])
def get_messages():
    limit = int(request.args.get('limit', 1000))
    since = request.args.get('since')
    until = request.args.get('until')
    oa = request.args.get('oa')

    db_result = db_fetch(limit, since, until, oa)
    if db_result is not None:
        result = db_result
    else:
        result = messages
        if oa:
            result = [m for m in result if m.get('oa') == oa]
        if since:
            result = [m for m in result if m['time'] >= since]
        if until:
            result = [m for m in result if m['time'] <= until]
        result = result[-limit:]
    return json.dumps(result, ensure_ascii=False, indent=2), 200, {'Content-Type': 'application/json'}


@app.route('/', methods=['GET'])
def health():
    return 'LINE Webhook Server is running - WirelessLife (monitor + push mode)', 200


init_db()


# Optional desk is isolated: its import/configuration failure cannot break old routes.
try:
    from desk import install as install_desk
    install_desk(app)
except Exception:
    print('[Desk] optional module unavailable; existing routes remain active')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
