"""Meta webhook + Send API path (desk.meta). Synthetic payloads, TEMP tables, rollback, a fake Meta client; nothing leaves."""
import hashlib
import hmac
import json
import time
import uuid
import pytest
from desk import fb, meta
from desk.errors import ActionError
from desk.tools import fb_browser
from test_desk import db, logged
from test_fb import picture, wayne, ready

SECRET = 'test-app-secret-only'
PSID = '24000000000000001'
PAGE = '100000000000001'


class FakeMeta:
    def __init__(self, fail_at=None, result=None):
        self.sent, self.fail_at, self.result = [], fail_at, result

    def profile(self, user, channel):
        return '合成客人' if channel == 'fb_message' else 'synthetic_ig'

    def send(self, recipient, message, tag=False):
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            return self.result
        self.sent.append((recipient, message, tag))
        return dict(ok=True, mid=f'm_{len(self.sent)}')


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('DESK_META_API', 'test')
    monkeypatch.setenv('FB_APP_SECRET', SECRET)
    monkeypatch.setenv('FB_VERIFY_TOKEN', 'verify-me')
    monkeypatch.setenv('FB_PAGE_ID', PAGE)
    monkeypatch.setenv('DESK_PUBLIC_URL', 'https://desk.example.test')
    monkeypatch.delenv('DESK_META_HUMAN_AGENT', raising=False)
    fake = FakeMeta()
    monkeypatch.setattr(meta, 'Client', lambda: fake)
    monkeypatch.setattr(meta, 'RUN', lambda fn: fn())
    return fake


def ms(seconds_ago=0):
    return int((time.time() - seconds_ago) * 1000)


def delivery(*messaging, obj='page'):
    return {'object': obj, 'entry': [{'id': PAGE, 'time': ms(), 'messaging': list(messaging)}]}


def incoming(text='請問電動尾門多少錢？', ago=0, user=PSID, **extra):
    msg = dict(mid=f'in.{uuid.uuid4().hex}', text=text, **extra)
    return {'sender': {'id': user}, 'recipient': {'id': PAGE}, 'timestamp': ms(ago), 'message': msg}


def echo(text, user=PSID, **extra):
    msg = dict(mid=f'out.{uuid.uuid4().hex}', text=text, is_echo=True, **extra)
    return {'sender': {'id': PAGE}, 'recipient': {'id': user}, 'timestamp': ms(), 'message': msg}


def sign(body):
    return 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def hook(client, payload, good=True):
    body = json.dumps(payload).encode()
    return client.post('/desk/meta/webhook', data=body, content_type='application/json',
                       headers={'X-Hub-Signature-256': sign(body) if good else 'sha256=' + '0' * 64})


def tid(user=PSID, channel='fb_message'):
    return fb.thread_id(channel, meta.PREFIX[channel] + user)


def receive(db, *messaging, obj='page'):
    meta.enqueue(db, delivery(*messaging, obj=obj))
    return meta.process(db, FakeMeta())


def approve(db, staff, text='您好～完工價 $18,000。', media_ids=None, user=PSID):
    c = fb.conversation(db, tid(user))
    return fb.approve(db, wayne(db, staff), dict(thread_id=c['id'], text=text, version=c['version'], retry_key=str(uuid.uuid4()),
                                                 confirmed=True, public_confirmed=False, media_ids=media_ids or []))


def test_signature_and_challenge(monkeypatch):
    monkeypatch.setenv('FB_APP_SECRET', SECRET)
    monkeypatch.setenv('FB_VERIFY_TOKEN', 'verify-me')
    body = b'{"object":"page"}'
    assert meta.signed(body, sign(body))
    assert not meta.signed(body, sign(b'{}')) and not meta.signed(body, None) and not meta.signed(body, 'sha1=abc')
    assert meta.verify_challenge({'hub.mode': 'subscribe', 'hub.verify_token': 'verify-me', 'hub.challenge': '42'}) == '42'
    assert meta.verify_challenge({'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '42'}) is None
    monkeypatch.delenv('FB_APP_SECRET')
    assert not meta.signed(body, sign(body))   # no secret configured: nothing is trusted


def test_mode_defaults_off(monkeypatch):
    monkeypatch.delenv('DESK_META_API', raising=False)
    assert meta.mode() == 'off'
    monkeypatch.setenv('DESK_META_API', 'yes')
    assert meta.mode() == 'off'
    monkeypatch.setenv('DESK_META_API', 'ON')
    assert meta.mode() == 'on'


def test_events_read_every_shape(monkeypatch):
    monkeypatch.setenv('FB_APP_ID', '777')
    ads = {'referral': {'source': 'ADS', 'ads_context_data': {'ad_title': '電動尾門限時優惠'}}}
    out = meta.events(delivery(
        incoming('多少錢', **ads),
        incoming('', attachments=[{'type': 'image', 'payload': {'url': 'https://x.test/a.jpg'}}]),
        incoming('', sticker_id=369239263222822, attachments=[{'type': 'image', 'payload': {'sticker_id': 369239263222822}}]),
        incoming('哪裡可以安裝？', quick_reply={'payload': 'Q1'}),
        echo('您好，完工價 18,000', metadata='wl-desk:abc'),
        echo('您好，完工價 18,000', app_id=777),
        echo('同事直接回的內容'),
        echo('感謝您的訊息！我們的客服團隊不在線上，會盡快回覆'),
        echo('稍等一下喔'),
        {'sender': {'id': PSID}, 'recipient': {'id': PAGE}, 'timestamp': ms(), 'postback': {'title': '預約安裝', 'payload': 'BOOK'}},
        {'sender': {'id': PSID}, 'recipient': {'id': PAGE}, 'timestamp': ms(), 'referral': ads['referral']},
        {'sender': {'id': PSID}, 'recipient': {'id': PAGE}, 'timestamp': ms(), 'read': {'watermark': 1}},
        incoming('收回', is_deleted=True),
    ))
    got = [(e['kind'], e.get('role'), e.get('text'), e.get('note'), e.get('own')) for e in out]
    assert got == [
        ('message', 'user', '多少錢', '', False),
        ('message', 'user', '[圖片]', '', False),               # a photo needs an answer
        ('message', 'user', '哪裡可以安裝？', '廣告預設問題', False),
        ('message', 'page', '您好，完工價 18,000', '', True),
        ('message', 'page', '您好，完工價 18,000', '', True),
        ('message', 'page', '同事直接回的內容', '', False),
        ('message', 'page', '感謝您的訊息！我們的客服團隊不在線上，會盡快回覆', '自動回覆', False),
        ('message', 'page', '稍等一下喔', '暫回', False),
        ('message', 'user', '預約安裝', '廣告預設問題', False),
        ('context', None, None, None, None),
    ]
    assert out[0]['context'] == '客人從廣告「電動尾門限時優惠」進來' and all(e['user'] == PSID for e in out)
    ig = meta.events(delivery(incoming('IG 問價', user='17841400000000001'), obj='instagram'))
    assert ig[0]['channel'] == 'ig_message' and ig[0]['user'] == '17841400000000001'
    assert meta.events({'object': 'whatsapp_business_account', 'entry': []}) == [] and meta.events('junk') == []


def test_webhook_is_ignored_while_off_and_rejects_unsigned(db, logged, monkeypatch, api):
    app = logged[0]
    client = app.test_client()        # no login cookie: Meta never has one
    r = client.get('/desk/meta/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=abc')
    assert r.status_code == 200 and r.get_data(as_text=True) == 'abc'
    assert client.get('/desk/meta/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=abc').status_code == 403
    assert hook(client, delivery(incoming()), good=False).status_code == 403
    monkeypatch.setenv('DESK_META_API', 'off')
    assert hook(client, delivery(incoming())).status_code == 200
    assert db.one('SELECT COUNT(*) AS n FROM desk_meta_inbox')['n'] == 0 and fb.conversation(db, tid()) is None
    monkeypatch.setenv('DESK_META_API', 'test')
    assert hook(client, delivery(incoming())).status_code == 200
    c = fb.conversation(db, tid())
    assert (c['name'], c['status'], c['version'], c['key']) == ('合成客人', 'waiting', 1, 'psid:' + PSID)
    assert db.one('SELECT payload,done_at FROM desk_meta_inbox')['payload'] is None   # raw copy dropped once processed


def test_messages_append_and_follow_the_push_rules(db, api):
    ads = {'referral': {'source': 'ADS', 'ads_context_data': {'ad_title': '360 環景'}}}
    receive(db, incoming('360 環景多少？', **ads))
    receive(db, incoming('車是 RAV4 2022'))
    c = fb.conversation(db, tid())
    assert (c['version'], c['context']) == (2, '客人從廣告「360 環景」進來')
    assert [m['text'] for m in fb.messages(db, tid())] == ['360 環景多少？', '車是 RAV4 2022']
    receive(db, incoming('', sticker_id=1, attachments=[{'type': 'image', 'payload': {'sticker_id': 1}}]))
    assert fb.conversation(db, tid())['version'] == 2 and len(fb.messages(db, tid())) == 2   # a sticker is not a new question
    receive(db, echo('感謝您的訊息！我們的客服團隊不在線上'))
    assert fb.conversation(db, tid())['status'] == 'waiting'            # Meta's away message answers nothing
    receive(db, echo('您好～RAV4 2022 的 360 環景完工價 $18,000，歡迎來門市看實機！'))
    c = fb.conversation(db, tid())
    assert (c['status'], c['note']) == ('done', '同事已回覆')           # colleague answered in Meta
    receive(db, incoming('那週六可以裝嗎？'))
    assert fb.conversation(db, tid())['status'] == 'waiting'
    assert len(fb.messages(db, tid())) == 5


def test_first_contact_from_ad_buttons_only_is_skipped(db, api):
    receive(db, incoming('多少錢？安裝要多久？', quick_reply={'payload': 'ad'}))
    assert fb.conversation(db, tid())['status'] == 'skip'


def test_webhook_conversations_wait_a_quiet_minute_before_drafting(db, api):
    from unittest.mock import Mock
    receive(db, incoming())
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert fb.conversation(db, tid())['status'] == 'waiting' and not engine.draft.called
    db.execute("UPDATE desk_fb_threads SET last_at=NOW()-INTERVAL '61 seconds' WHERE id=%s", (tid(),))
    fb.draft_once(db, lambda store: engine)
    assert fb.conversation(db, tid())['status'] == 'ready'


def test_approval_goes_out_at_once_in_parts_with_files_before_the_last(db, logged, api):
    from desk import fb_media, learning
    _, client, staff, _ = logged
    receive(db, incoming())
    db.execute("UPDATE desk_fb_threads SET last_at=NOW()-INTERVAL '2 minutes' WHERE id=%s", (tid(),))
    ready(db)
    boss = wayne(db, staff)
    pic = fb_media.upload(db, boss, tid(), picture())
    c = fb.conversation(db, tid())
    data = dict(thread_id=c['id'], text='第一段說明\n[[下一則]]\n請問您在哪個縣市？', version=c['version'], retry_key=str(uuid.uuid4()),
                confirmed=True, public_confirmed=False, media_ids=[pic['id']])
    r = client.post('/desk/api/fb/send', json=data, headers={'X-CSRF-Token': staff['csrf']})
    assert r.status_code == 200 and r.get_json()['live'] is True
    assert [m.get('text') or m['attachment']['type'] for _, m, _ in api.sent] == ['第一段說明', 'image', '請問您在哪個縣市？']
    assert api.sent[1][1]['attachment']['payload']['url'] == f"https://desk.example.test/desk/meta/media/{pic['id']}"
    assert all(rcpt == PSID and not tag for rcpt, _, tag in api.sent) and api.sent[0][1]['metadata'].startswith('wl-desk:')
    c = fb.conversation(db, tid())
    a = db.one('SELECT * FROM desk_fb_attempts WHERE retry_key=%s', (data['retry_key'],))
    assert (c['status'], c['note'], a['status'], a['delivery'], a['message_ids']) == ('done', '已即時送出', 'sent', 'done', ['m_1', 'm_2', 'm_3'])
    assert fb.messages(db, tid())[-1]['origin'] == 'api'
    # Meta fetches the picture without logging in; an unbound or unknown id stays hidden.
    anon = logged[0].test_client()
    assert anon.get(f"/desk/meta/media/{pic['id']}").data[:8] == b'\x89PNG\r\n\x1a\n'
    assert anon.get('/desk/meta/media/' + '0' * 32).status_code == 404
    # The echoes of our own parts change nothing and are not learned as a colleague's reply.
    receive(db, echo('第一段說明'), echo('請問您在哪個縣市？'))
    assert len(fb.messages(db, tid())) == 2 and fb.conversation(db, tid())['status'] == 'done'
    assert not [x for x in learning.candidates(db) if '縣市' in x['answer']]
    # A second click on the same confirmation sends nothing more.
    assert client.post('/desk/api/fb/send', json=data, headers={'X-CSRF-Token': staff['csrf']}).get_json()['duplicate'] is True
    assert len(api.sent) == 3


@pytest.mark.parametrize('result,reason', [
    (dict(ok=False, unknown=False, reason='這位客人目前無法接收訊息'), '送出失敗：沒有送出：這位客人目前無法接收訊息'),
    (dict(ok=False, unknown=True, reason='Meta 沒有在時間內回應'), '送出失敗：不確定有沒有送出（Meta 沒有在時間內回應），請先到 Meta 看過這則再決定，不要直接重新核准'),
])
def test_failed_send_goes_back_to_review(db, logged, api, result, reason):
    _, _, staff, _ = logged
    receive(db, incoming())
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    out = approve(db, staff)
    assert meta.deliver(db, out['attempt_id'], FakeMeta(fail_at=0, result=result)) == 'failed'
    c = fb.conversation(db, tid())
    assert (c['status'], c['note']) == ('ready', reason)
    assert meta.deliver(db, out['attempt_id'], FakeMeta()) is None      # never sent twice


def test_partial_send_says_what_went_out(db, logged, api):
    _, _, staff, _ = logged
    receive(db, incoming())
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    out = approve(db, staff, text='一\n[[下一則]]\n二\n[[下一則]]\n三')
    fake = FakeMeta(fail_at=2, result=dict(ok=False, unknown=False, reason='Meta 拒絕：x'))
    meta.deliver(db, out['attempt_id'], fake)
    assert fb.conversation(db, tid())['note'] == '送出失敗：第 1 則文字、第 2 則文字已送出，第 3 則文字沒送出：Meta 拒絕：x；請 Wayne 只補送沒送出的部分'


def test_customer_writing_again_before_send_expires_the_approval(db, logged, api):
    _, _, staff, _ = logged
    receive(db, incoming())
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    out = approve(db, staff)
    receive(db, incoming('等等，我想改問電動椅'))
    fake = FakeMeta()
    assert meta.deliver(db, out['attempt_id'], fake) is None and fake.sent == []
    assert fb.conversation(db, tid())['status'] == 'waiting'


def test_24_hour_window(db, logged, api, monkeypatch):
    _, _, staff, _ = logged
    receive(db, incoming(ago=25 * 3600))
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    fake = FakeMeta()
    out = approve(db, staff)
    meta.deliver(db, out['attempt_id'], fake)
    assert fake.sent == [] and '24 小時' in fb.conversation(db, tid())['note']
    monkeypatch.setenv('DESK_META_HUMAN_AGENT', '1')                   # approved Human Agent feature: 7 days
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    out = approve(db, staff)
    meta.deliver(db, out['attempt_id'], fake)
    assert fake.sent and fake.sent[0][2] is True


def test_off_mode_refuses_webhook_conversations_and_browser_never_gets_them(db, logged, api, monkeypatch):
    _, client, staff, _ = logged
    receive(db, incoming())
    db.execute("UPDATE desk_fb_threads SET status='ready' WHERE id=%s", (tid(),))
    monkeypatch.setenv('DESK_META_API', 'off')
    c = fb.conversation(db, tid())
    wayne(db, staff)
    r = client.post('/desk/api/fb/send', json=dict(thread_id=c['id'], text='好', version=c['version'], retry_key=str(uuid.uuid4()),
                    confirmed=True, public_confirmed=False), headers={'X-CSRF-Token': staff['csrf']})
    assert r.status_code == 409 and '即時發送目前關閉' in r.get_json()['error']
    monkeypatch.setenv('DESK_META_API', 'test')
    approve(db, staff)
    assert fb.pending(db) == [] and fb.awaiting(db) == []                 # one sender per approval


def test_sweep_sends_missed_approvals_and_reports_cut_off_sends(db, logged, api):
    _, _, staff, _ = logged
    receive(db, incoming())
    receive(db, incoming('第二位客人', user='24000000000000002'))
    db.execute("UPDATE desk_fb_threads SET status='ready'")
    first = approve(db, staff)
    second = approve(db, staff, user='24000000000000002')
    db.execute("UPDATE desk_fb_attempts SET approved_at=NOW()-INTERVAL '1 minute'")
    db.execute("UPDATE desk_fb_attempts SET delivery='sending',delivery_at=NOW()-INTERVAL '11 minutes' WHERE retry_key=%s", (second['attempt_id'],))
    fake = FakeMeta()
    meta.sweep(db, fake)
    assert len(fake.sent) == 1 and fb.conversation(db, tid())['status'] == 'done'
    assert fb.conversation(db, tid('24000000000000002'))['note'].startswith('送出失敗：不確定有沒有送出（發送中斷）')


def test_browser_push_skips_private_messages_once_the_api_is_on(db, tmp_path, capsys):
    meta.publish_mode(db)
    db.execute("UPDATE desk_marks SET value='on' WHERE name='meta_api'")
    rows = [dict(channel='fb_message', key='1', name='甲', url='', context='', messages=[dict(role='user', text='私訊', at='10:00')]),
            dict(channel='fb_comment', key='2#c', name='乙', url='', context='貼文', messages=[dict(role='user', text='留言', at='10:00')])]
    path = tmp_path / 'run.json'
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    assert fb_browser.main(['push', str(path)], lambda: db) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out['threads'], out['skipped_private']) == (1, 1)
    assert fb_browser.main(['status'], lambda: db) == 0
    assert json.loads(capsys.readouterr().out)['meta_api'] == 'on'


def test_policy_pages_are_public(logged):
    client = logged[0].test_client()
    for path, words in (('/desk/privacy', ['隱私權政策', 'Privacy Policy', 'OpenAI', '@infinite5000']),
                        ('/desk/data-deletion', ['資料刪除', 'Data Deletion', '30 天']),
                        ('/desk/terms', ['服務條款', 'Terms of Service'])):
        r = client.get(path)
        assert r.status_code == 200 and all(w in r.get_data(as_text=True) for w in words)
