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

# 記憶體暫存（伺服器重啟會清空，後續可升級到資料庫）
messages = []

def verify_signature(body: bytes, signature: str) -> bool:
        hash_val = hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'), body, hashlib.sha256).digest()
        return base64.b64encode(hash_val).decode('utf-8') == signature

def get_user_profile(user_id: str) -> str:
        url = f'https://api.line.me/v2/bot/profile/{user_id}'
        headers = {'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'}
        try:
                    res = requests.get(url, headers=headers, timeout=5)
                    if res.ok:
                                    return res.json().get('displayName', '未知用戶')
        except Exception:
                    pass
                return '未知用戶'

def reply_message(reply_token: str, text: str):
        url = 'https://api.line.me/v2/bot/message/reply'
    headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'
    }
    data = {'replyToken': reply_token, 'messages': [{'type': 'text', 'text': text}]}
    try:
                requests.post(url, headers=headers, json=data, timeout=5)
except Exception as e:
        print(f'回覆失敗：{e}')

def get_auto_reply(text: str):
        if any(k in text for k in ['營業時間', '幾點', '開門', '休息', '上班']):
                    return '您好！我們營業時間為週一至週六 10:00-19:00，週日公休。如需預約安裝歡迎私訊，謝謝！'
                if any(k in text for k in ['地址', '在哪', '門市', '位置', '怎麼去']):
                            return '您好！門市地址請洽 LINE 客服確認，建議先預約再前往，避免等候！'
                        if any(k in text for k in ['多少錢', '報價', '費用', '價格', '價錢', '$', '多少']):
                                    return '您好！價格依車型和配備不同，麻煩提供車款（品牌+車型+年份）及想安裝的項目，我們馬上為您報價！'
                                if any(k in text for k in ['預約', '約時間', '安排', '什麼時候可以']):
                                            return '您好！預約請提供：姓名、聯絡電話、車款、安裝項目、方便的日期時段，我們將盡快安排，謝謝！'
                                        if any(k in text for k in ['保固', '保修', '壞掉', '故障']):
                                                    return '您好！關於保固與維修，請提供您的車款與購買資訊，技術人員會盡快為您處理。如需緊急協助歡迎來電！'
                                                return None

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
                                reply_token = event.get('replyToken', '')
                                ts = datetime.fromtimestamp(event['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                                message = event.get('message', {})
                                if message.get('type') == 'text':
                                                    content = message.get('text', '')
                                                    display_name = get_user_profile(user_id)
                                                    auto_reply = get_auto_reply(content)
                                                    entry = {'time': ts, 'user': display_name, 'msg': content, 'replied': bool(auto_reply)}
                                                    messages.append(entry)
                                                    print(json.dumps(entry, ensure_ascii=False))
                                                    if auto_reply:
                                                                            reply_message(reply_token, auto_reply)
                                                            return 'OK', 200

                    @app.route('/messages', methods=['GET'])
def get_messages():
        return json.dumps(messages[-50:], ensure_ascii=False, indent=2), 200, {'Content-Type': 'application/json'}

@app.route('/', methods=['GET'])
def health():
        return '✅ LINE Webhook Server is running - WirelessLife 無限科技', 200

if __name__ == '__main__':
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
