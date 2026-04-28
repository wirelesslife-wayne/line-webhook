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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
