"""Browser-fed FB/IG review flow. Synthetic data, TEMP tables, rollback; nothing is delivered."""
import json
from pathlib import Path
from unittest.mock import Mock
import uuid
import pytest
from desk import fb
from desk.db import now
from desk.engine import Engine
from desk.errors import ActionError
from desk.tools import fb_browser
from test_desk import db, logged

KEY = '900001'


def thread(channel='fb_message', messages=None, key=KEY, context=''):
    return dict(channel=channel, key=key, name='合成測試客人', url='https://business.facebook.com/latest/inbox/all?selected_item_id=' + key,
                context=context, messages=messages or [dict(role='user', text='請問電動尾門多少錢？', at='2026-11-11T10:00:00+08:00')])


def tid(channel='fb_message', key=KEY):
    return fb.thread_id(channel, key)


def wayne(db, staff):
    db.execute('UPDATE desk_users SET username=%s WHERE id=%s', ('Wayne', staff['id']))
    return dict(staff, username='Wayne')


def ready(db, channel='fb_message', text='您好～完工價 $18,000。\n歡迎來門市看實機。'):
    engine = Mock()
    engine.draft.return_value = {'draft': text, 'flags': []}
    fb.draft_once(db, lambda store: engine)
    return engine


def approval(db, channel='fb_message', text='您好～完工價 $18,000。', **changes):
    data = dict(thread_id=tid(channel), text=text, version=fb.conversation(db, tid(channel))['version'],
                retry_key=str(uuid.uuid4()), confirmed=True, public_confirmed=channel.endswith('_comment'))
    data.update(changes)
    return data


def post(client, staff, path, data):
    return client.post('/desk/api/fb/' + path, json=data, headers={'X-CSRF-Token': staff['csrf']})


def test_push_dedupes_and_counts(db):
    assert fb.push(db, [thread()]) == dict(threads=1, new_customer_messages=1, auto_closed=0)
    assert fb.push(db, [thread()]) == dict(threads=1, new_customer_messages=0, auto_closed=0)
    c = fb.conversation(db, tid())
    assert (c['status'], c['version']) == ('waiting', 1)
    assert fb.status(db) == dict(waiting_draft=1, awaiting_review=0, draft_failed=0, approved_unsent=0, send_failed=0)
    ready(db)
    assert fb.status(db)['awaiting_review'] == 1


@pytest.mark.parametrize('bad', [
    [dict(thread(), channel='line')],
    [dict(thread(), name='')],
    [dict(thread(), url='javascript:alert(1)')],
    [dict(thread(), messages=[dict(role='bot', text='x', at='now')])],
    [dict(thread(), messages=[dict(role='user', text='x')])],
    {'not': 'a list'},
])
def test_push_rejects_malformed_input(db, bad):
    with pytest.raises(ActionError):
        fb.push(db, bad)
    assert db.one('SELECT COUNT(*) AS n FROM desk_fb_threads')['n'] == 0


def test_approve_pending_result_is_idempotent(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    data = approval(db)
    out = fb.approve(db, boss, data)
    assert out['status'] == 'approved' and fb.approve(db, boss, data)['duplicate'] is True
    queue = fb.pending(db)
    assert len(queue) == 1 and queue[0]['content'] == data['text'] and queue[0]['is_public'] is False
    assert queue[0]['last_customer_text'] == '請問電動尾門多少錢？' and queue[0]['name'] == '合成測試客人'
    assert fb.status(db)['approved_unsent'] == 1
    assert fb.record_result(db, out['attempt_id'], 'sent')['status'] == 'sent'
    assert fb.record_result(db, out['attempt_id'], 'sent')['duplicate'] is True
    assert fb.conversation(db, tid())['status'] == 'done' and fb.pending(db) == []
    assert db.one("SELECT COUNT(*) AS n FROM desk_fb_events WHERE origin='browser'")['n'] == 1
    with pytest.raises(ActionError):
        fb.record_result(db, out['attempt_id'], 'failed', '太晚回報')


def test_failed_send_returns_to_review(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    out = fb.approve(db, boss, approval(db))
    fb.record_result(db, out['attempt_id'], 'failed', '對象或內容對不上')
    c = fb.conversation(db, tid())
    assert c['status'] == 'ready' and '對象或內容對不上' in c['note']
    assert fb.status(db)['send_failed'] == 1 and fb.pending(db) == []


def test_send_skipped_because_someone_already_answered_closes_the_thread(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    out = fb.approve(db, boss, approval(db))
    fb.record_result(db, out['attempt_id'], 'failed', '已經有人回覆過，沒有重複送出')
    c = fb.conversation(db, tid())
    assert (c['status'], c['note']) == ('done', '同事已回覆')
    assert fb.status(db)['send_failed'] == 0 and fb.status(db)['awaiting_review'] == 0


def test_new_customer_message_expires_approval(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    fb.approve(db, boss, approval(db))
    newer = thread(messages=[dict(role='user', text='請問電動尾門多少錢？', at='2026-11-11T10:00:00+08:00'),
                             dict(role='user', text='我的車是 2020 RAV4', at='2026-11-11T10:30:00+08:00')])
    assert fb.push(db, [newer])['new_customer_messages'] == 1
    assert fb.pending(db) == []
    assert db.one('SELECT status FROM desk_fb_attempts')['status'] == 'expired'
    assert fb.conversation(db, tid())['status'] == 'waiting'


def test_colleague_reply_closes_and_cancels(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    fb.approve(db, boss, approval(db))
    replied = thread(messages=[dict(role='user', text='請問電動尾門多少錢？', at='2026-11-11T10:00:00+08:00'),
                               dict(role='page', text='您好，已私訊您囉', at='2026-11-11T10:20:00+08:00')])
    assert fb.push(db, [replied])['auto_closed'] == 1
    c = fb.conversation(db, tid())
    assert (c['status'], c['note']) == ('done', '同事已回覆')
    assert db.one('SELECT status FROM desk_fb_attempts')['status'] == 'cancelled' and fb.pending(db) == []


def test_public_comment_needs_explicit_confirmation(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread('fb_comment', context='合成貼文：電動尾門升級')])
    ready(db, 'fb_comment')
    with pytest.raises(ActionError, match='公開留言'):
        fb.approve(db, boss, approval(db, 'fb_comment', public_confirmed=False))
    fb.approve(db, boss, approval(db, 'fb_comment'))
    assert fb.pending(db)[0]['is_public'] is True


def test_only_reviewer_decides(db, logged):
    _, _, staff, _ = logged
    fb.push(db, [thread()])
    ready(db)
    version = fb.conversation(db, tid())['version']
    for call in (lambda: fb.approve(db, staff, approval(db)),
                 lambda: fb.action(db, staff, dict(thread_id=tid(), version=version), 'skip')):
        with pytest.raises(ActionError) as error:
            call()
        assert error.value.status == 403
    assert fb.conversation(db, tid())['status'] == 'ready'


def test_skip_cancel_and_stale_version(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    with pytest.raises(ActionError):
        fb.approve(db, boss, approval(db, version=99))
    fb.approve(db, boss, approval(db))
    version = fb.conversation(db, tid())['version']
    fb.action(db, boss, dict(thread_id=tid(), version=version), 'cancel')
    assert fb.conversation(db, tid())['status'] == 'ready' and fb.pending(db) == []
    fb.action(db, boss, dict(thread_id=tid(), version=version), 'skip')
    assert fb.conversation(db, tid())['status'] == 'skip'
    again = thread(messages=[dict(role='user', text='還在嗎？', at='2026-11-11T12:00:00+08:00')])
    fb.push(db, [again])
    assert fb.conversation(db, tid())['status'] == 'waiting'


def test_revise_remembers_instruction_and_redrafts_in_background(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    version = fb.conversation(db, tid())['version']
    out = fb.revise(db, boss, dict(thread_id=tid(), version=version, instruction='不要問縣市', original_draft='舊草稿', remember=True))
    assert out['status'] == 'waiting' and out['remembered'] is True and out['rule_id']
    assert fb.conversation(db, tid())['status'] == 'waiting'
    engine = ready(db, text='新草稿')
    kwargs = engine.draft.call_args.kwargs
    assert kwargs['instruction'] == '不要問縣市' and kwargs['original_draft'] == '舊草稿'
    c = fb.conversation(db, tid())
    assert c['status'] == 'ready' and c['draft']['draft'] == '新草稿' and c['pending_instruction'] == ''
    rule = db.one('SELECT * FROM desk_edit_rules')
    assert rule['instruction'] == '不要問縣市' and rule['revised_draft'] == '新草稿' and rule['active'] is True
    assert rule['conversation_summary'] == '請問電動尾門多少錢？'


def test_revise_without_remember_or_by_staff_stores_no_rule(db, logged):
    _, _, staff, _ = logged
    fb.push(db, [thread()])
    ready(db)
    version = fb.conversation(db, tid())['version']
    assert fb.revise(db, staff, dict(thread_id=tid(), version=version, instruction='短一點'))['remembered'] is False
    ready(db)
    boss = wayne(db, staff)
    assert fb.revise(db, boss, dict(thread_id=tid(), version=version, instruction='這則價格寫 18,000', remember=False))['remembered'] is False
    assert db.one('SELECT COUNT(*) AS n FROM desk_edit_rules')['n'] == 0
    with pytest.raises(ActionError):
        fb.revise(db, boss, dict(thread_id=tid(), version=version, instruction=''))


def test_engine_applies_edit_rules_to_every_channel(db, logged):
    _, _, staff, _ = logged
    db.execute("""INSERT INTO desk_edit_rules(instruction,channel,conversation_summary,original_draft,created_by)
      VALUES('不要問縣市','fb_message','合成問題','',%s),('已停用的指示','fb_message','','',%s)""", (staff['id'], staff['id']))
    db.execute("UPDATE desk_edit_rules SET active=FALSE WHERE instruction='已停用的指示'")
    timeline = [{'time': 'now', 'role': 'user', 'text': '主機', 'new': True}]
    for channel in ('line', 'fb_message', 'ig_comment'):
        provider = Mock(side_effect=[{}, dict(draft='您好～\n歡迎來門市看實機。')])
        Engine(db, provider).draft(timeline, now(), channel=channel)
        system = provider.call_args.args[0][0]['content']
        assert 'Wayne 修改指示' in system and '不要問縣市' in system and '已停用的指示' not in system
        assert ('本次是社群公開留言' in system) == channel.endswith('_comment')
        assert ('不算真人回覆過' in system) == (channel != 'line')


def test_engine_passes_post_context_and_flags_long_comment(db):
    provider = Mock(side_effect=[{}, dict(draft='\n'.join('第%d行' % i for i in range(12)))])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '多少錢', 'new': True}], now(),
                                     channel='fb_comment', context='合成貼文摘要', instruction='短一點', original_draft='原本的草稿')
    prompt = provider.call_args.args[0][1]['content']
    assert '合成貼文摘要' in prompt and '短一點' in prompt and '原本的草稿' in prompt
    assert any('偏長' in flag for flag in out['flags'])
    # The ad text (not only the conversation) decides the product for the price lookup: titles say nothing.
    assert '合成貼文摘要' in provider.call_args_list[0].args[0][1]['content']


def test_routes_need_login_csrf_and_reviewer(db, logged):
    app, client, staff, _ = logged
    fb.push(db, [thread(), thread('fb_comment', key='900002', context='合成貼文')])
    ready(db)
    assert app.test_client().get('/desk/api/fb/list').status_code in (302, 401)
    listing = client.get('/desk/api/fb/list').get_json()
    assert listing['todo'] == 2 and {i['label'] for i in listing['items']} == {'FB 私訊', 'FB 留言'}
    assert client.get('/desk/api/fb/list?channel=fb_comment').get_json()['items'][0]['is_public'] is True
    assert client.get('/desk/api/fb/list?tab=nope').status_code == 400
    assert client.post('/desk/api/fb/send', json=approval(db)).status_code == 403
    assert post(client, staff, 'send', approval(db)).status_code == 403
    wayne(db, staff)
    assert post(client, staff, 'send', approval(db)).get_json()['status'] == 'approved'
    assert client.get('/desk/api/fb/list?tab=approved').get_json()['approved'] == 1
    version = fb.conversation(db, tid())['version']
    assert post(client, staff, 'cancel', dict(thread_id=tid(), version=version)).status_code == 200
    out = post(client, staff, 'redraft', dict(thread_id=tid(), version=version, instruction='語氣輕鬆一點', remember=True)).get_json()
    assert out['remembered'] is True
    rule = db.one('SELECT id FROM desk_edit_rules')['id']
    assert post(client, staff, 'rules/%d' % rule, dict(active=False)).status_code == 200
    assert db.one('SELECT active FROM desk_edit_rules')['active'] is False
    assert post(client, staff, 'rules/999999', dict(active=True)).status_code == 404
    page = client.get('/desk/fb')
    assert page.status_code == 200 and '語氣輕鬆一點' in page.get_data(as_text=True)


def test_tool_reads_and_writes_utf8_json(db, tmp_path, capsys):
    source = tmp_path / 'batch.json'
    source.write_text(json.dumps([thread()], ensure_ascii=False), encoding='utf-8')
    assert fb_browser.main(['push', str(source)], lambda: db) == 0
    assert json.loads(capsys.readouterr().out)['new_customer_messages'] == 1
    assert fb_browser.main(['status'], lambda: db) == 0
    assert json.loads(capsys.readouterr().out)['waiting_draft'] == 1
    assert fb_browser.main(['pending'], lambda: db) == 0
    assert json.loads(capsys.readouterr().out) == []
    source.write_text('{broken', encoding='utf-8')
    assert fb_browser.main(['push', str(source)], lambda: db) == 1
    assert '格式不正確' in json.loads(capsys.readouterr().out)['error']
    assert fb_browser.main(['result', 'not-a-uuid', 'sent'], lambda: db) == 1


def test_only_the_meta_module_calls_meta():
    """11/13: the official API path lives in desk/meta.py (and the local setup tool); nothing else talks to Meta."""
    root = Path(fb.__file__).parent
    allowed = {root / 'meta.py', root / 'tools' / 'meta_setup.py'}
    for path in list(root.rglob('*.py')) + list(root.rglob('*.js')) + list(root.rglob('*.html')):
        if path in allowed:
            continue
        text = path.read_text(encoding='utf-8')
        assert 'graph.facebook.com' not in text and 'FB_PAGE_ACCESS_TOKEN' not in text, path.name


FAQ = [dict(role='page', text='您好！請告訴我們該如何為你提供協助。', at='上午7:35', note='自動問候'),
       dict(role='user', text='安裝要多久？價格多少？', at='上午7:35', note='廣告預設問題'),
       dict(role='page', text='施工時間約 3 小時，完工後會進行完整功能測試。', at='上午7:35', note='自動回覆'),
       dict(role='user', text='TOYOTA cc 2026年車', at='上午7:40')]


def test_rereading_the_same_conversation_changes_nothing(db):
    """11/11: every hourly re-read wrote the time and line breaks differently, so old messages came back as new."""
    fb.push(db, [thread('fb_comment', FAQ, context='合成貼文：電動尾門')])
    ready(db, 'fb_comment')
    before = fb.conversation(db, tid('fb_comment'))
    again = [dict(m, at='今天 ' + m['at']) for m in FAQ[:3]] + [
        dict(role='user', text='TOYOTA cc', at='今天 上午7:40'), dict(role='user', text='2026年車', at='今天 上午7:40')]
    for batch in (again, again[2:], FAQ):
        out = fb.push(db, [thread('fb_comment', batch, context='電動尾門貼文（這次摘要寫法不同）')])
        assert out == dict(threads=1, new_customer_messages=0, auto_closed=0)
    after = fb.conversation(db, tid('fb_comment'))
    assert (after['status'], after['version'], after['draft']) == ('ready', before['version'], before['draft'])
    assert after['context'] == '合成貼文：電動尾門'
    assert [m['text'].split()[0] for m in fb.messages(db, tid('fb_comment'))].count('安裝要多久？價格多少？') == 1
    assert fb.messages(db, tid('fb_comment'))[-1]['new'] is True


def test_real_new_message_after_reread_is_new_once(db):
    fb.push(db, [thread(messages=FAQ)])
    ready(db)
    more = [dict(m, at='今天 ' + m['at']) for m in FAQ] + [dict(role='user', text='我在高雄', at='下午5:11')]
    assert fb.push(db, [thread(messages=more)])['new_customer_messages'] == 1
    c = fb.conversation(db, tid())
    assert (c['status'], c['version'], c['draft']) == ('waiting', 2, None)
    assert fb.push(db, [thread(messages=more[1:])])['new_customer_messages'] == 0
    texts = [m['text'] for m in fb.messages(db, tid())]
    assert len(texts) == 5 and texts[-1] == '我在高雄'


def test_refused_ai_request_is_retried_later(db):
    """11/11: the OpenAI account ran out of credit; drafts must come back by themselves once it is topped up."""
    from desk.provider import Unavailable
    fb.push(db, [thread()])
    down = Mock()
    down.draft.side_effect = Unavailable('AI 服務拒絕了這次請求')
    fb.draft_once(db, lambda store: down)
    c = fb.conversation(db, tid())
    assert c['status'] == 'blocked' and c['draft']['retry'] is True
    assert fb.status(db)['draft_failed'] == 1 and fb.status(db)['awaiting_review'] == 0
    ready(db)
    assert fb.conversation(db, tid())['status'] == 'blocked'  # not before 30 minutes
    db.execute("UPDATE desk_fb_threads SET updated_at=NOW()-INTERVAL '31 minutes'")
    ready(db)
    c = fb.conversation(db, tid())
    assert c['status'] == 'ready' and 'retry' not in c['draft']


def test_ad_lead_gets_the_product_reply_without_naming_the_product(db):
    """11/11: tailgate-ad leads who only typed their car ("cc 2026") got drafts with no price."""
    provider = Mock(side_effect=[dict(brand='Toyota', model=None, products=[]), dict(draft='您好～')])
    timeline = [{'time': 'now', 'role': 'user', 'text': '安裝要多久？價格多少？', 'new': False},
                {'time': 'now', 'role': 'assistant', 'text': '電動尾門施工時間約 3 小時。', 'new': False},
                {'time': 'now', 'role': 'user', 'text': 'cc 2026', 'new': True}]
    Engine(db, provider).draft(timeline, now(), channel='fb_message')
    assert '電動尾門銷售回覆' in provider.call_args.args[0][1]['content']
    engine = Engine(db, Mock())
    named = [c['topic'] for c in engine.canned_for('想裝 360 環景', {}, '電動尾門施工時間約 3 小時')]
    assert '原廠螢幕增加環景系統銷售回覆' in named and '電動尾門銷售回覆' not in named


def test_open_lists_only_unanswered_conversations(db, capsys):
    """Wayne 11/11: a colleague may answer in the Meta inbox while a draft waits; the browser run must re-check."""
    fb.push(db, [thread(), thread('fb_comment', key='900002', context='合成貼文')])
    ready(db)
    assert fb_browser.main(['open'], lambda: db) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {(r['channel'], r['key'], r['last_customer_text'], r['is_public']) for r in listed} == {
        ('fb_message', KEY, '請問電動尾門多少錢？', False), ('fb_comment', '900002', '請問電動尾門多少錢？', True)}
    answered = thread(messages=[dict(role='user', text='請問電動尾門多少錢？', at='上午10:00'),
                                dict(role='page', text='您好，已私訊您囉', at='上午10:20')])
    assert fb.push(db, [answered])['auto_closed'] == 1
    assert [r['key'] for r in fb.awaiting(db)] == ['900002']


def test_auto_replies_and_holding_replies_do_not_close(db):
    """11/12 test: the Page's away message and promotion arrive right after every evening message."""
    away = [dict(role='user', text='請問 CR-V 電動尾門多少？', at='下午9:10'),
            dict(role='page', text='抱歉，現在我們的客服團隊不在線上…', at='下午9:10', note='自動回覆'),
            dict(role='page', text='喜歡無限的影音產品嗎？加入 LINE…', at='下午9:10', note='自動回覆')]
    assert fb.push(db, [thread(messages=away)]) == dict(threads=1, new_customer_messages=1, auto_closed=0)
    assert fb.conversation(db, tid())['status'] == 'waiting'
    assert [m['new'] for m in fb.messages(db, tid())] == [True, False, False]
    broadcast = away + [dict(role='page', text='如果您平常常需要抱小孩、搬東西…', at='上午10:00', note='群發')]
    assert fb.push(db, [thread(messages=broadcast)])['auto_closed'] == 0
    assert fb.conversation(db, tid())['status'] == 'waiting'
    holding = broadcast + [dict(role='page', text='稍等，幫您確認一下', at='上午10:20', note='暫回')]
    assert fb.push(db, [thread(messages=holding)])['auto_closed'] == 0
    assert fb.conversation(db, tid())['status'] == 'waiting'
    answered = holding + [dict(role='page', text='CR-V 完工價 18,000 元', at='上午10:40')]
    assert fb.push(db, [thread(messages=answered)])['auto_closed'] == 1
    assert fb.conversation(db, tid())['status'] == 'done'


def test_remember_is_off_unless_asked(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    version = fb.conversation(db, tid())['version']
    assert fb.revise(db, boss, dict(thread_id=tid(), version=version, instruction='這則寫短一點'))['remembered'] is False
    assert db.one('SELECT COUNT(*) AS n FROM desk_edit_rules')['n'] == 0


def test_approved_replies_become_examples_for_later_drafts(db, logged):
    """Wayne 11/12: it should learn from what I actually approve, without me doing anything extra."""
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db, text='AI 原本的草稿')
    fb.approve(db, boss, approval(db, text='您好～電動尾門完工價 $18,000。\n👉 適用車型：https://example.test/tailgate'))
    row = db.one('SELECT question,ai_draft FROM desk_fb_attempts')
    assert row['question'] == '請問電動尾門多少錢？' and row['ai_draft'] == 'AI 原本的草稿'
    provider = Mock(side_effect=[{}, dict(draft='您好～')])
    Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    system = provider.call_args.args[0][0]['content']
    assert 'Wayne 親自核准送出的回覆範例（最高優先' in system and 'https://example.test/tailgate' in system
    assert '被 Wayne 改掉了' in system and 'AI 原本的草稿' in system  # what he changed, not only the result
    provider = Mock(side_effect=[{}, dict(draft='您好～')])
    Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '週日有開嗎', 'new': True}], now(), channel='line')
    assert 'https://example.test/tailgate' not in provider.call_args.args[0][0]['content']


def picture(name='實機照.png', size=(8, 8)):
    from io import BytesIO
    from PIL import Image
    from werkzeug.datastructures import FileStorage
    data = BytesIO()
    Image.new('RGB', size, 'navy').save(data, 'PNG')
    data.seek(0)
    return FileStorage(stream=data, filename=name, content_type='image/png')


def test_attachments_travel_with_the_approved_private_reply(db, logged, tmp_path, capsys):
    """Wayne 11/12: let me attach photos and videos to FB/IG replies."""
    from desk import fb_media
    _, _, staff, _ = logged
    fb.push(db, [thread(), thread('fb_comment', key='900002', context='合成貼文'), thread('ig_comment', key='900003', context='合成貼文')])
    ready(db)
    with pytest.raises(ActionError) as denied:
        fb_media.upload(db, staff, tid(), picture())
    assert denied.value.status == 403
    boss = wayne(db, staff)
    # Wayne 11/13: comment replies too. A Facebook comment takes one photo or video, an Instagram comment none.
    with pytest.raises(ActionError, match='Instagram 留言不能附'):
        fb_media.upload(db, boss, tid('ig_comment', '900003'), picture())
    shown = fb_media.upload(db, boss, tid('fb_comment', '900002'), picture())
    with pytest.raises(ActionError, match='只能附 1 個'):
        fb_media.upload(db, boss, tid('fb_comment', '900002'), picture('第二張.png'))
    def comment(ids):
        return dict(thread_id=tid('fb_comment', '900002'), version=fb.conversation(db, tid('fb_comment', '900002'))['version'], text='您好～完工價 $18,000。',
                    retry_key=str(uuid.uuid4()), confirmed=True, public_confirmed=True, media_ids=ids)
    with pytest.raises(ActionError, match='附件清單'):
        fb.approve(db, boss, comment([shown['id'], 'f' * 32]))
    sent = fb.approve(db, boss, comment([shown['id']]))
    assert [m['id'] for m in fb_media.attached(db, sent['attempt_id'])] == [shown['id']]
    assert len(fb_media.export(db, sent['attempt_id'], tmp_path / 'comment')) == 1  # the browser run gets the file for the comment
    one = fb_media.upload(db, boss, tid(), picture())
    two = fb_media.upload(db, boss, tid(), picture('第二張.png'))
    assert one['type'] == 'image' and sorted(m['name'] for m in fb_media.listing(db, tid())) == sorted(['實機照.png', '第二張.png'])  # same transaction: same upload time
    fb_media.remove(db, boss, two['id'], tid())
    with pytest.raises(ActionError):
        fb.approve(db, boss, approval(db, media_ids=[two['id']]))          # removed file cannot be sent
    out = fb.approve(db, boss, approval(db, media_ids=[one['id']]))
    assert fb_media.listing(db, tid()) == [] and [m['id'] for m in fb_media.attached(db, out['attempt_id'])] == [one['id']]
    with pytest.raises(ActionError):
        fb_media.remove(db, boss, one['id'], tid())                          # already approved
    paths = fb_media.export(db, out['attempt_id'], tmp_path)
    assert len(paths) == 1 and paths[0].endswith('.png') and open(paths[0], 'rb').read(8) == b'\x89PNG\r\n\x1a\n'
    version = fb.conversation(db, tid())['version']
    fb.action(db, boss, dict(thread_id=tid(), version=version), 'cancel')
    assert [m['id'] for m in fb_media.listing(db, tid())] == [one['id']]      # cancelling frees the file for the next approval


def test_attachment_limits_and_bad_files(db, logged):
    from io import BytesIO
    from werkzeug.datastructures import FileStorage
    from desk import fb_media
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    for bad in (FileStorage(stream=BytesIO(b'not a picture'), filename='x.png', content_type='image/png'),
                FileStorage(stream=BytesIO(b'MZ'), filename='x.exe', content_type='application/octet-stream'),
                FileStorage(stream=BytesIO(b'fake'), filename='x.mp4', content_type='video/mp4')):
        with pytest.raises(ActionError):
            fb_media.upload(db, boss, tid(), bad)
    for n in range(3):
        fb_media.upload(db, boss, tid(), picture('%d.png' % n))
    with pytest.raises(ActionError, match='最多'):
        fb_media.upload(db, boss, tid(), picture('4.png'))
    assert fb_media.read(db, 'x' * 32) is None


def test_customer_who_only_pressed_ad_buttons_is_not_waiting(db):
    """Wayne 11/12 (王英寬): pressing the ad's preset questions is answered by the ad itself."""
    pressed = [dict(role='user', text='多少錢？安裝要多久？', at='上午3:00', note='廣告預設問題'),
               dict(role='page', text='WRM-418 完工價 NT$18,500，施工時間約 4～5 小時。', at='上午3:00', note='自動回覆')]
    assert fb.push(db, [thread(messages=pressed)])['new_customer_messages'] == 0
    c = fb.conversation(db, tid())
    assert c['status'] == 'skip' and '廣告預設問題' in c['note']
    assert fb.push(db, [thread(messages=pressed + [dict(role='user', text='我的車是 2022 RAV4', at='上午9:10')])])['new_customer_messages'] == 1
    assert fb.conversation(db, tid())['status'] == 'waiting'


class FakePage:
    def __init__(self, status, text='', data=None):
        self.status_code, self.text, self._data = status, text, data or {}
    def json(self):
        return self._data


def test_short_links_are_opened_and_named_before_sending(db, logged):
    """Wayne 11/12: a wrong short link would be embarrassing; show where each one really goes."""
    from desk import links
    pages = {
        'https://reurl.cc/good01': FakePage(200, '<input type="hidden" id="target" name="target" value="https://youtube.com/shorts/Nanz5A4XkX0?si=x">'),
        'https://reurl.cc/dead01': FakePage(404),
        'https://carandroid.com.tw/a/': FakePage(200, '<title>電動尾門推薦｜無限科技</title>'),
    }
    def get(url):
        if url.startswith('https://www.youtube.com/oembed'):
            return FakePage(200, data={'title': 'TOYOTA Corolla cross【電動尾門】'})
        return pages[url]
    out = links.check(db, '安裝實例：https://reurl.cc/good01\n介紹：https://carandroid.com.tw/a/', get)
    assert [(l['state'], l['title']) for l in out] == [('ok', 'YouTube：TOYOTA Corolla cross【電動尾門】'), ('ok', '電動尾門推薦｜無限科技')]
    assert links.check(db, '壞的：https://reurl.cc/dead01', get)[0]['state'] == 'broken'
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    with pytest.raises(ActionError, match='連結打不開'):
        fb.approve(db, boss, approval(db, text='您好～請看 https://reurl.cc/dead01'))
    assert fb.approve(db, boss, approval(db, text='您好～請看 https://reurl.cc/good01'))['status'] == 'approved'


def test_draft_with_an_unknown_link_is_flagged(db):
    provider = Mock(side_effect=[{}, dict(draft='您好～請看 https://made.up/link')])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少', 'new': True}], now(), channel='fb_message')
    assert any('資料裡沒有的連結' in f for f in out['flags'])


def test_push_keeps_a_screenshot_of_the_real_conversation(db, logged, tmp_path, capsys):
    """Wayne 11/12: show the Meta screen next to the transcription so he can trust it."""
    _, client, staff, _ = logged
    shot = tmp_path / 'shot.png'
    picture('shot.png').save(str(shot))
    source = tmp_path / 'batch.json'
    source.write_text(json.dumps([dict(thread(), shot=str(shot)), dict(thread(key='900009'), shot=str(tmp_path / 'missing.png'))],
                                 ensure_ascii=False), encoding='utf-8')
    assert fb_browser.main(['push', str(source)], lambda: db) == 0
    assert json.loads(capsys.readouterr().out)['screenshots'] == 1
    bad = tmp_path / 'bad.png'
    bad.write_bytes(b'not an image')
    from desk import fb_shots
    assert fb_shots.save(db, tid(), bad.read_bytes()) is False
    r = client.get('/desk/api/fb/shot', query_string={'thread_id': tid()})
    assert r.status_code == 200 and r.mimetype == 'image/png'
    assert client.get('/desk/api/fb/shot', query_string={'thread_id': tid(key='900009')}).status_code == 404
    listed = client.get('/desk/api/fb/list').get_json()['items']
    assert [bool(i['shot']) for i in sorted(listed, key=lambda i: i['key'])] == [True, False]


def test_ad_context_arriving_later_redrafts_the_wrong_product_draft(db):
    # Wayne 11/12: the ad banner was missed, so the AI guessed the product; once the ad is known, draft again.
    fb.push(db, [thread()])
    ready(db)
    assert fb.conversation(db, tid())['status'] == 'ready'
    ad = '客人從廣告「原車沒有電尾門？也能輕鬆升級」進來'
    fb.push(db, [thread(context=ad)])
    c = fb.conversation(db, tid())
    assert (c['status'], c['context'], c['draft']) == ('waiting', ad, None)
    ready(db)
    fb.push(db, [thread(context=ad)])
    assert fb.conversation(db, tid())['status'] == 'ready'
