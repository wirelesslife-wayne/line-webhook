"""FB Messenger / Instagram private messages through Meta's official API: webhook intake and the Send API.

Wayne 11/13: the hourly browser run is too slow; private messages should arrive and go out in real time.
Off by default. DESK_META_API selects who handles private messages:

  unset / off  the browser schedule does everything; webhooks are verified, acknowledged and ignored.
  test         webhook intake + Send API for conversations that came in through the webhook. In Meta's development
               mode only people with a role on the app reach the webhook, so this is for testing and the App Review
               recording; the browser schedule keeps reading and sending every other conversation.
  on           after App Review: private messages come in and go out only through the API. The browser schedule
               keeps public comments and any approval of a conversation it imported earlier.

A conversation that came through the webhook has key 'psid:<id>' (Messenger) or 'igsid:<id>' (Instagram) and is sent
only by the API; every other conversation is sent only by the browser run. One approval therefore has exactly one sender.
Customer data goes only to Meta (the conversation's own platform); nothing here talks to any other service.
"""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timedelta, timezone
import requests
from . import fb
from .db import now
from .errors import ActionError
from .worker import TAIPEI

PREFIX = {'fb_message': 'psid:', 'ig_message': 'igsid:'}
GRAPH = 'https://graph.facebook.com'
WINDOW = timedelta(hours=24)       # standard messaging window after the customer's last message
HUMAN_AGENT = timedelta(days=7)    # with the approved Human Agent feature
IG_TEXT_LIMIT = 1000               # Instagram's per-message text limit
IG_IMAGE_LIMIT = 8_000_000         # Instagram accepts images up to 8 MB (Messenger 25 MB)
QUIET = 60                         # seconds without a new customer message before drafting
STUCK = timedelta(minutes=10)
# Meta's own automatic messages (away message, greeting, ad answers) seen as echoes; they never answer the customer.
CANNED = ('客服團隊不在線上', '喜歡無限的影音產品', '施工時間約 3 小時，完工後', '該如何為你提供協助', '營業時間如下',
          '非常感謝您對我們產品的興趣')
HOLDING = ('稍等', '幫您查', '幫您確認', '確認一下', '查一下', '晚點回覆', '稍後回覆')


def mode():
    value = os.environ.get('DESK_META_API', '').strip().lower()
    return value if value in ('test', 'on') else 'off'


def is_api(thread):
    return any(thread['channel'] == ch and thread['key'].startswith(p) for ch, p in PREFIX.items())


def live(thread):
    """True when an approval of this conversation goes out through the Send API right away."""
    return mode() != 'off' and is_api(thread)


def check_sendable(thread):
    if is_api(thread) and mode() == 'off':
        raise ActionError('即時發送目前關閉；這則請直接到 Meta 收件匣回覆，回完按「不發送」', 409)


def publish_mode(store):
    """The browser tool runs on Wayne's PC and cannot see Render's settings, so the server writes its mode here."""
    store.execute("""INSERT INTO desk_marks(name,value) VALUES('meta_api',%s)
      ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()""", (mode(),))


def stored_mode(store):
    row = store.one("SELECT value FROM desk_marks WHERE name='meta_api'")
    return row['value'] if row and row['value'] in ('test', 'on') else 'off'


# ---------- webhook ----------

def verify_challenge(args):
    expected = os.environ.get('FB_VERIFY_TOKEN', '')
    token = args.get('hub.verify_token', '')
    if expected and args.get('hub.mode') == 'subscribe' and hmac.compare_digest(token.encode(), expected.encode()):
        return args.get('hub.challenge', '')
    return None


def signed(body, header):
    secret = os.environ.get('FB_APP_SECRET', '')
    if not secret or not isinstance(header, str) or not header.startswith('sha256='):
        return False
    good = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good.encode(), header.encode())


def enqueue(store, payload):
    """Keep the raw delivery first (Meta wants a fast 200); a background thread turns it into conversation events."""
    store.execute('INSERT INTO desk_meta_inbox(payload) VALUES(%s)', (json.dumps(payload, ensure_ascii=False),))


def run_later(fn):
    threading.Thread(target=fn, daemon=True, name='desk-meta').start()


RUN = run_later  # tests run it inline


def stamp(ms):
    try:
        at = datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        at = now()
    return at.astimezone(TAIPEI).replace(microsecond=0).isoformat()


def marker(attachments):
    names = dict(image='[圖片]', video='[影片]', audio='[語音]', file='[檔案]', story_mention='[限時動態提及]',
                 share='[分享貼文]', ig_reel='[Reels]', reel='[Reels]', template='[卡片]', fallback='[連結]')
    return ' '.join(names.get(a.get('type'), '[附件]') for a in attachments if isinstance(a, dict))


def ad_context(*sources):
    for s in sources:
        ref = s.get('referral') if isinstance(s, dict) else None
        if isinstance(ref, dict):
            ads = ref.get('ads_context_data') or {}
            title = ads.get('ad_title') or ''
            if title:
                return f'客人從廣告「{title[:200]}」進來'
            if ref.get('source') == 'ADS':
                return '客人從廣告進來（Meta 沒有提供廣告標題）'
    return ''


def page_note(text):
    flat = fb.squash(text)
    if any(fb.squash(c) in flat for c in CANNED):
        return '自動回覆'
    if len(flat) <= 25 and any(h in flat for h in HOLDING):
        return '暫回'
    return ''


def events(payload):
    """One webhook delivery as plain events, oldest first. Unknown shapes are skipped, never raised."""
    if not isinstance(payload, dict):
        return []
    channel = {'page': 'fb_message', 'instagram': 'ig_message'}.get(payload.get('object'))
    if not channel:
        return []
    app_id = os.environ.get('FB_APP_ID', '')
    out = []
    for entry in payload.get('entry') or []:
        if not isinstance(entry, dict):
            continue
        # 'standby': delivered while another app (Meta's inbox) holds the conversation; it is the same message.
        for m in (entry.get('messaging') or []) + (entry.get('standby') or []):
            if not isinstance(m, dict):
                continue
            sender, recipient = (m.get('sender') or {}).get('id'), (m.get('recipient') or {}).get('id')
            msg, postback = m.get('message'), m.get('postback')
            base = dict(channel=channel, at=stamp(m.get('timestamp')), context=ad_context(m, msg, postback))
            if isinstance(msg, dict):
                if msg.get('is_deleted') or msg.get('is_unsupported'):
                    continue
                echo = bool(msg.get('is_echo'))
                user = recipient if echo else sender
                atts = [a for a in msg.get('attachments') or [] if isinstance(a, dict)]
                text = msg.get('text') or ''
                sticker = msg.get('sticker_id') or any((a.get('payload') or {}).get('sticker_id') for a in atts)
                note = ''
                if sticker and not text:
                    continue                 # a like or sticker alone does not reopen a conversation
                if atts and not text:
                    text = marker(atts)      # a photo of the car is a new message that needs an answer
                elif atts:
                    text += ' ' + marker(atts)
                if echo:
                    own = str(msg.get('metadata', '')).startswith('wl-desk:') or bool(app_id and str(msg.get('app_id', '')) == app_id)
                    note = note or page_note(text)
                    out.append(dict(base, kind='message', user=user, role='page', text=text, note=note, mid=msg.get('mid', ''), own=own))
                else:
                    if msg.get('quick_reply'):
                        note = '廣告預設問題'  # a button the customer pressed, not words they typed
                    out.append(dict(base, kind='message', user=user, role='user', text=text, note=note, mid=msg.get('mid', ''), own=False))
            elif isinstance(postback, dict):
                out.append(dict(base, kind='message', user=sender, role='user', text=postback.get('title') or postback.get('payload') or '',
                                note='廣告預設問題', mid=postback.get('mid', ''), own=False))
            elif base['context'] and sender:
                out.append(dict(base, kind='context', user=sender))
    return [e for e in out if e.get('user')]


def process(store, client=None):
    """Turn stored webhook deliveries into conversation events, one at a time, in arrival order."""
    with store.lock('desk-meta-inbox') as acquired:
        if not acquired:
            return 0
        done = 0
        client = client or Client()
        for row in store.all('SELECT id,payload FROM desk_meta_inbox WHERE done_at IS NULL AND tries<5 ORDER BY id LIMIT 100'):
            try:
                payload = row['payload'] if isinstance(row['payload'], dict) else json.loads(row['payload'] or 'null')
                for ev in events(payload):
                    apply(store, ev, client)
                store.execute('UPDATE desk_meta_inbox SET done_at=NOW(),payload=NULL,error=%s WHERE id=%s', ('', row['id']))
                done += 1
            except Exception as exc:
                store.execute('UPDATE desk_meta_inbox SET tries=tries+1,error=%s WHERE id=%s', (type(exc).__name__, row['id']))
        return done


def inbox_url(channel):
    page = os.environ.get('FB_PAGE_ID', '')
    folder = 'instagram_direct' if channel == 'ig_message' else 'messenger'
    return f'https://business.facebook.com/latest/inbox/{folder}' + (f'?asset_id={page}' if page.isdigit() else '')


def placeholder(channel, user):
    return ('IG 客人 ' if channel == 'ig_message' else 'FB 客人 ') + user[-4:]


def own_send(store, tid, text):
    """An echo of what the desk itself just sent (the event is already recorded by the send)."""
    flat = fb.squash(text)
    if not flat:
        return False
    for a in store.all("""SELECT content FROM desk_fb_attempts WHERE thread_id=%s AND status IN ('approved','sent')
      AND updated_at > NOW()-INTERVAL '1 hour'""", (tid,)):
        if any(fb.squash(p) == flat for p in fb.parts(a['content'])):
            return True
    return False


def apply(store, ev, client):
    channel, user = ev['channel'], str(ev['user'])
    key = PREFIX[channel] + user
    tid = fb.thread_id(channel, key)
    mark = f'meta_ref:{tid}'
    if ev['kind'] == 'context':
        store.execute("""INSERT INTO desk_marks(name,value) VALUES(%s,%s)
          ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()""", (mark, ev['context']))
        return
    if ev['own'] or (ev['role'] == 'page' and own_send(store, tid, ev['text'])):
        return
    c = fb.conversation(store, tid)
    name = c['name'] if c else ''
    if not name or name == placeholder(channel, user):
        name = client.profile(user, channel) or placeholder(channel, user)
    context = (c['context'] if c else '') or ev['context']
    if not context:
        ref = store.one('SELECT value FROM desk_marks WHERE name=%s', (mark,))
        context = ref['value'] if ref else ''
    old = store.all('SELECT role,content,at,note FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (tid,)) if c else []
    snapshot = [dict(role=e['role'], text=e['content'], at=e['at'], note=e['note']) for e in old]
    snapshot.append(dict(role=ev['role'], text=ev['text'][:10000], at=ev['at'], note=ev['note']))
    fb.push(store, [dict(channel=channel, key=key, name=name[:200], url=inbox_url(channel), context=context[:10000],
                         messages=snapshot[-2000:])])


# ---------- Send API ----------

class Client:
    """Meta Graph API with the Page access token. Never puts the token or exception text into results."""

    def __init__(self):
        self.token = os.environ.get('FB_PAGE_ACCESS_TOKEN', '')
        self.page = os.environ.get('FB_PAGE_ID', '')
        self.version = os.environ.get('DESK_META_GRAPH_VERSION', 'v27.0')

    def profile(self, user, channel):
        if not self.token:
            return ''
        fields = 'name,username' if channel == 'ig_message' else 'name'
        try:
            r = requests.get(f'{GRAPH}/{self.version}/{user}', params=dict(fields=fields, access_token=self.token),
                             timeout=(5, 10), allow_redirects=False)
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            return ''
        return str(data.get('name') or data.get('username') or '')

    def send(self, recipient, message, tag=False):
        if not self.token or not self.page.isdigit():
            return dict(ok=False, unknown=False, reason='粉專權杖或粉專編號沒有設定，請通知管理者')
        body = dict(recipient=dict(id=recipient), message=message, access_token=self.token)
        body.update(dict(messaging_type='MESSAGE_TAG', tag='HUMAN_AGENT') if tag else dict(messaging_type='RESPONSE'))
        try:
            r = requests.post(f'{GRAPH}/{self.version}/{self.page}/messages', json=body, timeout=(5, 20), allow_redirects=False)
        except requests.Timeout:
            return dict(ok=False, unknown=True, reason='Meta 沒有在時間內回應')
        except Exception:
            return dict(ok=False, unknown=False, reason='連不上 Meta')
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get('message_id'):
            return dict(ok=True, mid=data['message_id'])
        if r.status_code >= 500:
            return dict(ok=False, unknown=True, reason=f'Meta 伺服器錯誤 {r.status_code}')
        return dict(ok=False, unknown=False, reason=explain(data.get('error') or {}))


def explain(error):
    code, sub = error.get('code'), error.get('error_subcode')
    if sub in (2018278, 2534022):
        return '已超過可回覆的時間（客人最後一則訊息後 24 小時），Meta 不讓 API 送；請到 Meta 收件匣直接回'
    if sub in (2534037, 2018300):
        return '這段對話目前由 Meta 的其他工具（例如 Meta 的 AI 回覆）接管，請到 Meta 收件匣直接回'
    if sub == 2534041:
        return 'IG 帳號的「允許存取訊息」被關掉了，請通知管理者'
    if code in (10, 200, 3):
        return 'Meta 不允許這個動作（權限還沒通過審核，或客人不是測試帳號），請通知管理者'
    if code == 190:
        return '粉專權杖失效，請通知管理者更新 FB_PAGE_ACCESS_TOKEN'
    if code == 551 or sub == 1545041:
        return '這位客人目前無法接收訊息'
    if code in (4, 32, 613):
        return 'Meta 暫時限制發送次數，請稍後再核准'
    return 'Meta 拒絕：' + str(error.get('message') or '未知原因')[:200]


def public_base():
    for name in ('DESK_PUBLIC_URL', 'DESK_MEDIA_BASE_URL', 'RENDER_EXTERNAL_URL'):
        value = os.environ.get(name, '').rstrip('/')
        if value.startswith('https://'):
            return value
    return ''


def dispatch(store, attempt_id):
    """Right after Wayne approves: send in the background so the page answers at once."""
    RUN(lambda: deliver(store, attempt_id))


def deliver(store, attempt_id, client=None):
    """Send one approved reply. Claimed once, never retried automatically: a doubtful send goes back to Wayne."""
    with store.cursor() as cur:
        cur.execute("""UPDATE desk_fb_attempts SET delivery='sending',delivery_at=NOW()
          WHERE retry_key=%s AND status='approved' AND delivery IS NULL RETURNING *""", (attempt_id,))
        a = cur.fetchone()
        if not a:
            return None
        cur.execute('SELECT * FROM desk_fb_threads WHERE id=%s', (a['thread_id'],))
        c = cur.fetchone()
        cur.execute("SELECT MAX(event_at) AS at FROM desk_fb_events WHERE thread_id=%s AND role='user'", (c['id'],))
        last_user = cur.fetchone()['at']
        cur.execute("""SELECT id,content_type,size FROM desk_fb_media WHERE attempt_key=%s AND removed_at IS NULL
          AND data IS NOT NULL ORDER BY uploaded_at,id""", (attempt_id,))
        files = cur.fetchall()
    if not live(c):
        return finish(store, attempt_id, 'failed', '這則不是即時發送的對話，請重新載入', [])
    if c['version'] != a['source_version'] or c['status'] != 'approved':
        with store.cursor() as cur:
            cur.execute("UPDATE desk_fb_attempts SET status='expired',reason='對話已更新，請重新擬稿',delivery='done',updated_at=NOW() WHERE retry_key=%s", (attempt_id,))
            if c['status'] not in ('done', 'skip'):
                cur.execute("UPDATE desk_fb_threads SET status='waiting',draft=NULL,drafted_version=NULL,draft_token=NULL WHERE id=%s", (c['id'],))
        return 'expired'
    age = now() - last_user if last_user else WINDOW
    tag = False
    if age >= WINDOW:
        # Human Agent (7 days) is confirmed for Messenger only; Meta's pages disagree about Instagram.
        if os.environ.get('DESK_META_HUMAN_AGENT') == '1' and age < HUMAN_AGENT and c['channel'] == 'fb_message':
            tag = True
        else:
            return finish(store, attempt_id, 'failed', '已超過可回覆的時間（客人最後一則訊息後 24 小時），Meta 不讓 API 送；請到 Meta 收件匣直接回', [])
    texts = fb.parts(a['content'])
    if c['channel'] == 'ig_message' and any(len(t) > IG_TEXT_LIMIT for t in texts):
        return finish(store, attempt_id, 'failed', '有一則超過 IG 的 1,000 字上限，請再分成更多則', [])
    if c['channel'] == 'ig_message' and any(f['content_type'] != 'video/mp4' and f['size'] > IG_IMAGE_LIMIT for f in files):
        return finish(store, attempt_id, 'failed', 'IG 的圖片上限是 8MB，請換一張小一點的圖', [])
    base = public_base()
    if files and not base:
        return finish(store, attempt_id, 'failed', '附件的公開網址沒有設定，文字也沒有送出；請通知管理者', [])
    recipient = c['key'].split(':', 1)[1]
    meta = {} if c['channel'] == 'ig_message' else dict(metadata='wl-desk:' + attempt_id)
    # Pictures go just before the last text, so the question stays at the bottom (same order as the browser run).
    steps = [(f'第 {i} 則文字', dict(text=t, **meta)) for i, t in enumerate(texts[:-1], 1)]
    steps += [(f'附件 {i}', dict(attachment=dict(type='video' if f['content_type'] == 'video/mp4' else 'image',
                                                payload=dict(url=f"{base}/desk/meta/media/{f['id']}"))))
              for i, f in enumerate(files, 1)]
    steps.append((f'第 {len(texts)} 則文字', dict(text=texts[-1], **meta)))
    client = client or Client()
    done, mids = [], []
    for label, message in steps:
        result = client.send(recipient, message, tag)
        if result.get('ok'):
            done.append(label)
            mids.append(result.get('mid', ''))
            continue
        if result.get('unknown'):
            why = '不確定有沒有送出（' + result['reason'] + '），請先到 Meta 看過這則再決定，不要直接重新核准'
        else:
            why = result['reason']
        if done:
            why = f"{'、'.join(done)}已送出，{label}沒送出：{why}；請 Wayne 只補送沒送出的部分"
        elif not result.get('unknown'):
            why = '沒有送出：' + why
        return finish(store, attempt_id, 'failed', why, mids)
    return finish(store, attempt_id, 'sent', '', mids)


def finish(store, attempt_id, outcome, reason, mids):
    try:
        fb.record_result(store, attempt_id, outcome, reason[:1000], via='api')
    except ActionError:
        # Cancelled or expired while sending: keep the reason on the record without touching newer state.
        store.execute('UPDATE desk_fb_attempts SET reason=%s,updated_at=NOW() WHERE retry_key=%s', (reason[:1000], attempt_id))
    store.execute("UPDATE desk_fb_attempts SET delivery='done',message_ids=%s WHERE retry_key=%s",
                  (json.dumps(mids), attempt_id))
    return outcome


def sweep(store, client=None):
    """Safety net in the drafter loop: approvals whose immediate send never started, and sends cut off by a restart."""
    if mode() == 'off':
        return
    for a in store.all("""SELECT a.retry_key FROM desk_fb_attempts a JOIN desk_fb_threads t ON t.id=a.thread_id
      WHERE a.status='approved' AND a.delivery='sending' AND a.delivery_at < %s""", (now() - STUCK,)):
        key = str(a['retry_key'])
        finish(store, key, 'failed', '不確定有沒有送出（發送中斷），請先到 Meta 看過這則再決定，不要直接重新核准', [])
    for a in store.all("""SELECT a.retry_key FROM desk_fb_attempts a JOIN desk_fb_threads t ON t.id=a.thread_id
      WHERE a.status='approved' AND a.delivery IS NULL AND a.approved_at < NOW()-INTERVAL '20 seconds'
      AND (t.key LIKE 'psid:%%' OR t.key LIKE 'igsid:%%') ORDER BY a.approved_at LIMIT 10"""):
        deliver(store, str(a['retry_key']), client)


def public_media(store, media_id):
    """Files of an approved or sent reply, for Meta to fetch by their unguessable 128-bit id."""
    from .media import TOKEN
    if not isinstance(media_id, str) or not TOKEN.fullmatch(media_id):
        return None
    return store.one("""SELECT m.data,m.content_type FROM desk_fb_media m JOIN desk_fb_attempts a ON a.retry_key=m.attempt_key
      WHERE m.id=%s AND m.data IS NOT NULL AND m.removed_at IS NULL AND a.status IN ('approved','sent')""", (media_id,))
