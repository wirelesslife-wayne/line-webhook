import os
import json
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort
from datetime import datetime

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')

messages = []


def verify_signature(body, signature):
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    return base64.b64encode(hash_val).decode('utf-8') == signature


def get_user_profile(user_id):
    url = 'https://api.line.me/v2/bot/profile/' + user_id
    headers = {'Authorization': 'Bearer ' + LINE_CHANNEL_ACCESS_TOKEN}
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
    if not verify_signature(body, signature):
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


@app.route('/messages', methods=['GET'])
def get_messages():
    return json.dumps(messages[-100:], ensure_ascii=False, indent=2), 200, {'Content-Type': 'application/json'}


@app.route('/', methods=['GET'])
def health():
    return 'LINE Webhook Server is running - WirelessLife (monitor mode)', 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
