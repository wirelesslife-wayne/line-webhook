import os
import json
import hmac
import hashlib
import base64
import requests
import psycopg2
from flask import Flask, request, abort
from datetime import datetime

app = Flask(__name__)

# 環境變數（部署到 Render 後在後台設定）
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── 資料庫初始化 ───────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP,
            user_id VARCHAR(100),
            display_name VARCHAR(200),
            message_type VARCHAR(50),
            content TEXT,
            replied BOOLEAN DEFAULT FALSE,
            reply_content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()
    print("✅ 資料庫初始化完成")

# ─── LINE API 工具函式 ──────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    """驗證 LINE 的 Webhook 簽章，確保請求合法"""
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_val).decode('utf-8')
    return expected == signature

def get_user_profile(user_id: str) -> str:
    """從 LINE API 取得用戶顯示名稱"""
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
    """回覆訊息給用戶"""
    url = 'https://api.line.me/v2/bot/message/reply'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'
    }
    data = {
        'replyToken': reply_token,
        'messages': [{'type': 'text', 'text': text}]
    }
    try:
        requests.post(url, headers=headers, json=data, timeout=5)
    except Exception as e:
        print(f"❌ 回覆失敗：{e}")

def save_message(user_id, display_name, msg_type, content, timestamp, replied=False, reply_content=None):
    """儲存訊息到資料庫"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO messages (timestamp, user_id, display_name, message_type, content, replied, reply_content)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (timestamp, user_id, display_name, msg_type, content, replied, reply_content))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ 儲存訊息失敗：{e}")

# ─── 自動回覆邏輯 ───────────────────────────────────────────
def get_auto_reply(text: str):
    """
    根據關鍵字判斷是否自動回覆。
    回傳回覆內容字串，或 None 表示不自動回覆（交給人工）。
    """
    text_lower = text.lower().strip()

    # 營業時間
    if any(k in text for k in ['營業時間', '幾點', '開門', '休息', '上班']):
        return (
            "您好！我們的營業時間如下：\n\n"
            "📍 週一至週六：10:00 - 19:00\n"
            "📍 週日：公休\n\n"
            "如需預約安裝，歡迎私訊或致電，謝謝！😊"
        )

    # 地址 / 門市
    if any(k in text for k in ['地址', '在哪', '門市', '位置', '怎麼去', '板橋', '台中', '高雄']):
        return (
            "您好！我們各門市資訊如下：\n\n"
            "🏪 板橋店：新北市板橋區（詳細地址請洽客服）\n"
            "🏪 其他門市：請洽 LINE 客服確認\n\n"
            "建議先預約再前往，避免等候！"
        )

    # 報價 / 價格
    if any(k in text for k in ['多少錢', '報價', '費用', '價格', '價錢', '$', '多少']):
        return (
            "您好！價格依車型和配備有所不同，\n"
            "麻煩提供以下資訊，我們馬上為您報價：\n\n"
            "1️⃣ 車款（品牌 + 車型 + 年份）\n"
            "2️⃣ 想安裝的項目\n\n"
            "感謝您的詢問！😊"
        )

    # 預約
    if any(k in text for k in ['預約', '約時間', '安排', '什麼時候可以']):
        return (
            "您好！預約安裝請提供：\n\n"
            "1️⃣ 姓名\n"
            "2️⃣ 聯絡電話\n"
            "3️⃣ 車款\n"
            "4️⃣ 希望安裝項目\n"
            "5️⃣ 方便的日期與時段\n\n"
            "我們將盡快安排，謝謝！"
        )

    # 保固
    if any(k in text for k in ['保固', '保修', '壞掉', '故障', '問題']):
        return (
            "您好！關於產品保固與維修，\n"
            "請提供您的車款與購買資訊，\n"
            "我們的技術人員會盡快為您處理。\n\n"
            "如需緊急協助，歡迎直接來電！"
        )

    return None  # 無自動回覆，交給人工

# ─── Webhook 主入口 ─────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data()

    # 驗證簽章
    if not verify_signature(body, signature):
        print("❌ 簽章驗證失敗")
        abort(400)

    data = json.loads(body)
    events = data.get('events', [])

    for event in events:
        event_type = event.get('type')
        source = event.get('source', {})
        user_id = source.get('userId', 'unknown')
        ts = datetime.fromtimestamp(event.get('timestamp', 0) / 1000)

        if event_type == 'message':
            reply_token = event.get('replyToken', '')
            message = event.get('message', {})
            msg_type = message.get('type', 'unknown')

            # 取得用戶名稱
            display_name = get_user_profile(user_id)

            if msg_type == 'text':
                content = message.get('text', '')
                print(f"📨 [{ts}] {display_name}：{content}")

                # 判斷是否自動回覆
                auto_reply = get_auto_reply(content)

                if auto_reply:
                    reply_message(reply_token, auto_reply)
                    save_message(user_id, display_name, msg_type, content, ts,
                                 replied=True, reply_content=auto_reply)
                    print(f"✅ 已自動回覆 {display_name}")
                else:
                    save_message(user_id, display_name, msg_type, content, ts,
                                 replied=False)
                    print(f"⚠️  {display_name} 的訊息需要人工回覆")

            elif msg_type in ('image', 'video', 'audio', 'file'):
                content = f"[{msg_type} 檔案]"
                save_message(user_id, display_name, msg_type, content, ts)

            elif msg_type == 'sticker':
                save_message(user_id, display_name, 'sticker', '[貼圖]', ts)

        elif event_type == 'follow':
            display_name = get_user_profile(user_id)
            save_message(user_id, display_name, 'follow', '用戶加入好友', ts)
            print(f"🎉 新好友：{display_name}")

        elif event_type == 'unfollow':
            save_message(user_id, 'unknown', 'unfollow', '用戶封鎖/取消追蹤', ts)

    return 'OK', 200

# ─── 查詢近期訊息 API ───────────────────────────────────────
@app.route('/messages', methods=['GET'])
def get_messages():
    """查看最近 50 則未回覆訊息（內部使用）"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            SELECT timestamp, display_name, content, replied, reply_content
            FROM messages
            WHERE message_type = 'text'
            ORDER BY timestamp DESC
            LIMIT 50
        ''')
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for row in rows:
            result.append({
                'timestamp': str(row[0]),
                'display_name': row[1],
                'content': row[2],
                'replied': row[3],
                'reply_content': row[4]
            })
        return json.dumps(result, ensure_ascii=False, indent=2), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return str(e), 500

# ─── 健康檢查 ───────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health():
    return '✅ LINE Webhook Server is running - WirelessLife 無限科技', 200

# ─── 啟動 ───────────────────────────────────────────────────
if __name__ == '__main__':
    if DATABASE_URL:
        init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
