"""Wayne 11/16 11:03: FB/IG customers who only pressed ad buttons, every one answered by the ad's automatic reply,
are closed by themselves (no draft, no review); 待我們確認 card buttons and 10-second undo.
Synthetic data, TEMP tables, rollback; the AI is a Mock, nothing is sent."""
from unittest.mock import Mock
from desk import fb
from desk.db import now
from test_desk import db, logged
from test_fb import thread, wayne

ASK = '您好，請告訴我們該如何為你提供協助。'
ANSWER = 'WRM-418 完工價 NT$18,500，施工時間約 4～5 小時。'
AWAY = '抱歉，現在我們的客服團隊不在線上，無法回覆您的問題。我們會在最短時間內回覆您。'


def ev(role, text, note=''):
    return dict(role=role, content=text, note=note)


def button(text='多少錢？安裝要多久？'):
    return ev('user', text, '廣告預設問題')


def auto(text=ANSWER):
    return ev('page', text, '自動回覆')


def test_rule_cases():
    assert fb.ad_only([auto(ASK), button(), auto()]) == 'buttons'
    # five presses at once, then five answers (吳杰晞); the same button twice, answered twice (張樺松)
    assert fb.ad_only([button('a'), button('b'), button('c'), auto('1'), auto('2'), auto('3')]) == 'buttons'
    assert fb.ad_only([button('a'), button('b'), button('a'), auto('1'), auto('1'), auto('2')]) == 'buttons'
    assert fb.ad_only([button('a'), button('b'), auto('1')]) == ''  # one press left unanswered
    assert fb.ad_only([button(), auto(AWAY)]) == ''  # the after-hours message answers nothing
    assert fb.ad_only([button(), auto('稍後將由專人與您聯繫')]) == ''  # promises a person
    assert fb.ad_only([button(), auto(), ev('user', '', '[貼圖] 👍')]) == 'thumb'  # Sunny Chung
    assert fb.ad_only([ev('user', '', '[貼圖] 👍'), button(), auto()]) == ''  # the sticker came first
    assert fb.ad_only([button(), auto(), ev('user', '', '[貼圖]')]) == ''  # some other sticker
    assert fb.ad_only([button(), auto(), ev('user', '', '[圖片]')]) == ''  # a photo may be the car
    assert fb.ad_only([button(), auto(), ev('user', '', '[語音]')]) == ''
    assert fb.ad_only([button(), auto(), ev('user', 'cc')]) == ''  # typed
    assert fb.ad_only([button(), auto(), ev('page', '您好，請問車款年份？')]) == ''  # a colleague wrote
    assert fb.ad_only([button()]) == ''  # the automatic answer is not in yet
    assert fb.ad_only([ev('user', '多少錢')]) == ''
    # the broadcast read back between the presses (張木榮、葉紹忠 11/16) is not an unanswered press
    promo = ev('page', 'WRM-418 只錄前後，真的夠嗎？', '群發')
    assert fb.ad_only([promo, button(), promo, button(), auto(), auto()]) == 'buttons'
    assert fb.ad_only([promo, button(), promo]) == ''  # still no automatic answer


def msgs(*extra):
    return [dict(role='page', text=ASK, at='下午7:00', note='自動回覆'),
            dict(role='user', text='多少錢？安裝要多久？', at='下午7:01', note='廣告預設問題'),
            dict(role='page', text=ANSWER, at='下午7:01', note='自動回覆')] + list(extra)


def test_closed_by_itself_then_back_when_the_customer_writes(db):
    fb.push(db, [thread(key='950001', messages=msgs())])
    t = db.one("SELECT id FROM desk_fb_threads WHERE key='950001'")['id']
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～照片收到了', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    c = fb.conversation(db, t)
    assert not engine.draft.called and c['status'] == 'skip' and c['auto_at'] and c['unstar'] == 'todo'
    assert c['note'].startswith('🤖 自動處理')
    assert [x['thread_id'] for x in fb.recheck(db)] == [t] and fb.recheck(db)[0]['pressed'] == ['多少錢？安裝要多久？']
    assert next(x for x in fb.to_unstar(db) if x['thread_id'] == t)['auto'] is True
    assert fb.status(db)['awaiting_review'] == 0  # nothing for Wayne to look at
    # a photo afterwards brings it back (no text, only the browser's note)
    fb.push(db, [thread(key='950001', messages=msgs(dict(role='user', text='', at='下午8:00', note='[圖片]')))])
    c = fb.conversation(db, t)
    assert c['status'] == 'waiting' and '客人有新訊息' in c['note']
    fb.draft_once(db, lambda store: engine)
    assert engine.draft.called  # a real draft now


def test_thumb_after_the_buttons_is_closed_too(db):
    fb.push(db, [thread(key='950002', messages=msgs(dict(role='user', text='', at='下午7:05', note='[貼圖] 👍')))])
    engine = Mock()
    fb.draft_once(db, lambda store: engine)
    c = db.one("SELECT * FROM desk_fb_threads WHERE key='950002'")
    assert not engine.draft.called and c['status'] == 'skip' and '👍' in c['note']


def test_not_closed_with_a_hold_or_on_line(db):
    fb.push(db, [thread(key='950003', messages=msgs())])
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='查庫存' WHERE key='950003'")
    fb.push(db, [thread(channel='line_message', key='U950004', messages=msgs())])
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='950003'")['status'] == 'ready'
    assert engine.draft.call_count == 2


def test_reopen_drafts_a_follow_up_and_never_closes_again(db, logged):
    _, client, staff, _ = logged
    wayne(db, staff)
    fb.push(db, [thread(key='950005', messages=msgs())])
    t = db.one("SELECT id FROM desk_fb_threads WHERE key='950005'")['id']
    fb.draft_once(db, lambda store: Mock())
    done = client.get('/desk/api/fb/list?tab=done&auto=1').get_json()
    assert [i['id'] for i in done['items']] == [t] and done['items'][0]['auto'] == 'buttons'
    assert done['auto_total'] == 1 and done['auto_today'] == 1
    r = client.post('/desk/api/fb/reopen', json=dict(thread_id=t), headers={'X-CSRF-Token': staff['csrf']})
    assert r.status_code == 200
    c = fb.conversation(db, t)
    assert c['status'] == 'waiting' and c['no_auto'] and c['unstar'] == '' and c['pending_instruction']
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～想再請問您的車款年份？', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert engine.draft.called and fb.conversation(db, t)['status'] == 'ready'
    # the next morning's redraft has no instruction: still not closed by itself
    db.execute("UPDATE desk_fb_threads SET status='waiting',pending_instruction='' WHERE id=%s", (t,))
    fb.draft_once(db, lambda store: engine)
    assert fb.conversation(db, t)['status'] == 'ready'
    assert client.post('/desk/api/fb/reopen', json=dict(thread_id=t), headers={'X-CSRF-Token': staff['csrf']}).status_code == 409


def test_close_and_undo_the_hold(db, logged):
    _, client, staff, _ = logged
    wayne(db, staff)
    fb.push(db, [thread(key='950006', messages=[dict(role='user', text='10/31 約好來裝', at='2026-11-01T09:00:00+08:00')])])
    t = db.one("SELECT id FROM desk_fb_threads WHERE key='950006'")['id']
    db.execute("""UPDATE desk_fb_threads SET status='ready',hold='ours',hold_note='約定 10/31 已過',hold_by='',
      hold_at=NOW()-INTERVAL '1 day',note='x' WHERE id=%s""", (t,))
    h = {'X-CSRF-Token': staff['csrf']}
    item = next(i for i in client.get('/desk/api/fb/list?tab=hold').get_json()['items'] if i['id'] == t)
    assert item['hold_by'] == '' and item['hold_days'] == 1 and item['hold_ends']
    r = client.post('/desk/api/fb/hold', json=dict(thread_id=t, hold=''), headers=h).get_json()
    c = fb.conversation(db, t)
    assert c['hold'] == '' and c['status'] == 'done' and r['undo']['hold'] == 'ours'
    assert client.post('/desk/api/fb/hold', json=dict(thread_id=t, hold='', restore=r['undo']), headers=h).status_code == 200
    c = fb.conversation(db, t)
    assert (c['hold'], c['hold_note'], c['hold_by'], c['status'], c['note']) == ('ours', '約定 10/31 已過', '', 'ready', 'x')
    # an approved reply still goes out: closing the hold does not touch it
    db.execute("UPDATE desk_fb_threads SET status='approved' WHERE id=%s", (t,))
    client.post('/desk/api/fb/hold', json=dict(thread_id=t, hold=''), headers=h)
    assert fb.conversation(db, t)['status'] == 'approved'


def test_ai_time_out_is_retried_three_times_on_fb_only(db):
    """Wayne 11/16 12:31 (Alvis Kao): one OpenAI time-out left the customer with no draft for good."""
    from desk.provider import Transient
    fb.push(db, [thread(key='950007', messages=[dict(role='user', text='2026年Toyota Cc汽油版', at='上午11:20')])])
    t = db.one("SELECT id FROM desk_fb_threads WHERE key='950007'")['id']
    engine = Mock()
    engine.draft.side_effect = Transient('AI 擬稿失敗，用量待核對，請手動回覆')
    for n in (1, 2, 3):
        fb.draft_once(db, lambda store: engine)
        c = fb.conversation(db, t)
        assert c['status'] == 'blocked' and c['draft']['tries'] == n and c['draft']['retry'] is (n < 3)
        db.execute("UPDATE desk_fb_threads SET updated_at=NOW()-INTERVAL '31 minutes' WHERE id=%s", (t,))
    fb.draft_once(db, lambda store: engine)
    assert engine.draft.call_count == 3  # the third failure is not retried again
    assert '連續 3 次' in fb.conversation(db, t)['draft']['staff_note']
    # OpenAI answers on a retry -> a normal draft
    db.execute("UPDATE desk_fb_threads SET status='waiting',draft=%s WHERE id=%s", ('{"tries": 1}', t))
    engine.draft.side_effect = None
    engine.draft.return_value = {'draft': '您好～', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert fb.conversation(db, t)['status'] == 'ready'


def test_tools_apply_the_schema_only_when_it_changed(db, monkeypatch):
    """Wayne 11/16: every tool start ran ALTER TABLE on the live tables; a long read made it freeze the desk."""
    from desk.tools import common
    assert common.ensure_schema(db) is True
    assert db.one("SELECT value FROM desk_marks WHERE name='schema'")['value']
    assert common.ensure_schema(db) is False  # unchanged: nothing is run, no lock is asked for
    import desk.db
    monkeypatch.setattr(desk.db, 'SCHEMA', desk.db.SCHEMA + ' -- changed')
    assert common.ensure_schema(db) is True


def test_two_cars_in_one_question_still_get_a_draft():
    """王柏昇 11/16: 「Toyota Granvia 跟 Volkswagen T6.1」 came back as lists and drafting crashed."""
    from desk.engine import one_car
    ex = one_car({'brand': ['Toyota', 'Volkswagen'], 'model': ['Granvia', 'T6.1'], 'year': [], 'series': ['B42 MAX', 'S14'],
                  'location': None, 'products': '電子後視鏡'})
    assert (ex['brand'], ex['model'], ex['year'], ex['series'], ex['products']) == ('Toyota', 'Granvia', None, 'B42 MAX', ['電子後視鏡'])
    assert one_car({'brand': 'Toyota', 'model': 'RAV4', 'products': ['電動尾門']}) == {'brand': 'Toyota', 'model': 'RAV4', 'products': ['電動尾門']}
    assert one_car(['not', 'a', 'dict']) == {}


def test_voided_approval_is_not_treated_as_sent(db, logged):
    """Wayne 11/16 13:47 (Alvis Kao): the approved quote was voided by a newer message and never sent;
    the redraft must say it all again. FB/IG only."""
    from test_fb import ready, approval, tid
    from desk.engine import Engine
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    first = dict(role='user', text='2026年CC', at='2026-11-16T10:18:00+08:00')
    fb.push(db, [thread(messages=[first])])
    ready(db, text='您好～您的 2026 年 Toyota Corolla Cross 可以升級電動尾門。\n👉 完工價 $18,000（含安裝）')
    fb.approve(db, boss, approval(db, text='您好～您的 2026 年 Toyota Corolla Cross 可以升級電動尾門。\n👉 完工價 $18,000（含安裝）'))
    fb.push(db, [thread(messages=[first, dict(role='user', text='2026年Toyota Cc汽油版', at='2026-11-16T11:20:00+08:00')])])
    c = fb.conversation(db, tid())
    assert c['status'] == 'waiting' and '$18,000' in fb.voided_reply(db, c)
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert '$18,000' in engine.draft.call_args.kwargs['unsent']
    # the real prompt tells the AI the customer never got it
    provider = Mock(side_effect=[{}, dict(draft='您好～')])
    Engine(db, provider).draft(fb.messages(db, tid(), True), now(), channel='fb_message', unsent=fb.voided_reply(db, c))
    prompt = provider.call_args.args[0][1]['content']
    assert '客人從來沒有收到' in prompt and '$18,000' in prompt
    # once a reply really goes out, the voided one is history
    data = approval(db, text='您好～汽油版也可以裝，完工價 $18,000。')
    fb.approve(db, boss, data)
    fb.record_result(db, fb.pending(db)[0]['attempt_id'], 'sent')
    assert fb.voided_reply(db, fb.conversation(db, tid())) == ''
    # LINE is left exactly as it was
    assert fb.voided_reply(db, dict(c, channel='line_message')) == ''


# Wayne 11/16 16:30: LINE showed 24 to review against far fewer in the LINE inbox.
def test_line_asked_back_rule():
    ask = ev('page', '您好，請問有什麼地方需要為您服務的嗎？(happy face)', '自動回覆')  # a colleague's saved reply
    call = ev('user', '呼叫客服')
    night = ev('page', '請您先留言，一看到您的訊息就會馬上回覆您喔', '自動（夜間AI或系統）')
    assert fb.asked_back([call, night, ev('page', '[貼圖]', '暫回'), ask])  # 黃寶珠
    assert fb.asked_back([ev('user', 'TEL：0981-104-442', '選單電話'), ask])  # 林廼智Jason
    assert not fb.asked_back([call, night])  # only the system's night message
    assert not fb.asked_back([call, ask, ev('user', '我想問尾門')])  # the customer answered: ours again
    assert not fb.asked_back([call, ev('page', '請問有什麼需要服務的嗎？', '自動回應')])  # LINE's own auto-response
    assert not fb.asked_back([ask])  # no customer message at all


def test_line_no_reply_and_asked_back_close_by_themselves(db):
    fb.push(db, [thread(channel='line_message', key='U950010', messages=[
        dict(role='user', text='TEL：02-29525859', at='下午3:00', note='選單電話')])])
    fb.push(db, [thread(channel='line_message', key='U950011', messages=[
        dict(role='user', text='呼叫客服', at='下午3:00'),
        dict(role='page', text='[貼圖]', at='下午3:05', note='暫回'),
        dict(role='page', text='請問有什麼地方需要為您服務的嗎？', at='下午3:05', note='自動回覆')])])
    engine = Mock()
    engine.draft.return_value = {'draft': fb.NO_REPLY, 'flags': [], 'staff_note': '只點門市電話'}
    fb.draft_once(db, lambda store: engine)
    a = db.one("SELECT * FROM desk_fb_threads WHERE key='U950010'")
    b = db.one("SELECT * FROM desk_fb_threads WHERE key='U950011'")
    assert engine.draft.call_count == 1  # the asked-back one needs no draft
    assert a['status'] == 'skip' and a['auto_at'] and a['draft']['auto'] == 'noreply' and a['unstar'] == ''
    assert b['status'] == 'skip' and b['draft']['auto'] == 'asked'
    assert fb.status(db)['awaiting_review'] == 0
    # the customer writes again: back to review, like the ad-button ones
    fb.push(db, [thread(channel='line_message', key='U950011', messages=[
        dict(role='user', text='呼叫客服', at='下午3:00'),
        dict(role='page', text='[貼圖]', at='下午3:05', note='暫回'),
        dict(role='page', text='請問有什麼地方需要為您服務的嗎？', at='下午3:05', note='自動回覆'),
        dict(role='user', text='想問安卓機價格', at='下午3:20')])])
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='U950011'")['status'] == 'waiting'


def test_line_ready_draft_closes_when_a_colleague_asks_back(db):
    first = [dict(role='user', text='呼叫客服', at='下午3:00')]
    fb.push(db, [thread(channel='line_message', key='U950012', messages=first)])
    engine = Mock()
    engine.draft.return_value = {'draft': '您好～請問想了解哪項產品呢？', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='U950012'")['status'] == 'ready'
    fb.push(db, [thread(channel='line_message', key='U950012', messages=first + [
        dict(role='page', text='請問有什麼地方需要為您服務的嗎？', at='下午3:30', note='自動回覆')])])
    c = db.one("SELECT * FROM desk_fb_threads WHERE key='U950012'")
    assert c['status'] == 'skip' and c['draft']['auto'] == 'asked'


def test_line_kept_when_wayne_marked_it(db):
    fb.push(db, [thread(channel='line_message', key='U950013', messages=[
        dict(role='user', text='TEL：02-29525859', at='下午3:00', note='選單電話')])])
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_by='wayne' WHERE key='U950013'")
    engine = Mock()
    engine.draft.return_value = {'draft': fb.NO_REPLY, 'flags': []}
    fb.draft_once(db, lambda store: engine)
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='U950013'")['status'] == 'ready'


def test_ai_hold_clears_when_a_colleague_answers_but_wayne_hold_stays(db):
    ask = [dict(role='user', text='尾門打不開', at='下午3:00')]
    for key in ('U950020', 'U950021'):
        fb.push(db, [thread(channel='line_message', key=key, messages=ask)])
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='請售後確認' WHERE key='U950020'")
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='Wayne 要看',hold_by='wayne' WHERE key='U950021'")
    for key in ('U950020', 'U950021'):
        fb.push(db, [thread(channel='line_message', key=key, messages=ask + [
            dict(role='page', text='您好，請先拍一段操作影片給我們看看', at='下午3:10')])])
    a = db.one("SELECT status,hold FROM desk_fb_threads WHERE key='U950020'")
    b = db.one("SELECT status,hold FROM desk_fb_threads WHERE key='U950021'")
    assert a == {'status': 'done', 'hold': ''} and b == {'status': 'done', 'hold': 'ours'}
    # a sticker alone is not an answer: the 📌 stays
    fb.push(db, [thread(channel='line_message', key='U950022', messages=ask)])
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='請售後確認' WHERE key='U950022'")
    fb.push(db, [thread(channel='line_message', key='U950022', messages=ask + [dict(role='page', text='[貼圖]', at='下午3:10', note='暫回')])])
    assert db.one("SELECT hold FROM desk_fb_threads WHERE key='U950022'")['hold'] == 'ours'


# Wayne 11/16 17:50: a colleague answered and the customer only said thanks -> closed by itself, nothing sent.
def test_line_thanks_only_rule():
    q, ans = ev('user', '安裝要多久'), ev('page', '安裝約 90 分鐘，門市有休息室可以等候。')
    assert fb.thanks_only([q, ans, ev('user', '好的 謝謝')])
    assert fb.thanks_only([q, ans, ev('user', '[貼圖]')]) and fb.thanks_only([q, ans, ev('user', '謝謝您🙏')])
    assert not fb.thanks_only([q, ans, ev('user', '好的'), ev('user', '那價格呢')])  # a question follows
    assert not fb.thanks_only([q, ev('page', '明天 13:00 方便嗎？'), ev('user', '好的')])  # 「好的」 answers our question
    assert not fb.thanks_only([q, ev('page', '這邊確認後再回覆您'), ev('user', '謝謝')])  # we promised to come back
    assert not fb.thanks_only([q, ans, ev('page', '稍等，幫您查庫存', '暫回'), ev('user', '好的')])
    assert not fb.thanks_only([q, ans, ev('user', '可以')])  # agrees to something: someone books it
    assert not fb.thanks_only([q, ans, ev('user', '謝謝 再找時間現場去詳談')])  # more than a thank-you
    assert not fb.thanks_only([q, ans, ev('user', '[圖片]')]) and not fb.thanks_only([ev('user', '好的')])
    assert not fb.thanks_only([q, ans, ev('user', '未購買_客服', '選單')])  # a menu tap asks for service


def test_line_thanks_closes_without_a_draft_unless_pinned(db):
    talk = [dict(role='user', text='安裝要多久', at='下午3:00'),
            dict(role='page', text='安裝約 90 分鐘，門市有休息室可以等候。', at='下午3:05'),
            dict(role='user', text='好的 謝謝', at='下午3:06')]
    fb.push(db, [thread(channel='line_message', key='U950030', messages=talk)])
    fb.push(db, [thread(channel='line_message', key='U950031', messages=talk)])
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='請售後確認' WHERE key='U950031'")
    fb.push(db, [thread(channel='fb_message', key='950032', messages=talk)])  # FB/IG: not this rule
    engine = Mock()
    engine.draft.return_value = {'draft': '不客氣～', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    a = db.one("SELECT status,draft FROM desk_fb_threads WHERE key='U950030'")
    assert a['status'] == 'skip' and a['draft']['auto'] == 'thanks'
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='U950031'")['status'] == 'ready'  # 📌: Wayne still sees it
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='950032'")['status'] == 'ready'
    assert engine.draft.call_count == 2


# Wayne 11/16 18:30: 「立即同步」 and send-right-after-approve share one lock per inbox with the scheduled rounds.
def test_run_lock_lets_one_round_in(db):
    assert fb.run_lock(db, 'line', 'sched')['ok'] and not fb.run_lock(db, 'line', 'sync-1')['ok']
    assert fb.run_busy(db, 'line') and not fb.run_busy(db, 'fb')
    assert fb.run_lock(db, 'fb', 'sync-1')['ok']  # the other inbox is separate
    assert not fb.run_unlock(db, 'line', 'sync-1')['ok']  # only the holder frees it
    assert fb.run_unlock(db, 'line', 'sched')['ok'] and fb.run_lock(db, 'line', 'sync-1')['ok']
    db.execute("UPDATE desk_marks SET at=NOW()-INTERVAL '10 minutes' WHERE name='run_lock_line'")
    assert not fb.run_busy(db, 'line') and fb.run_lock(db, 'line', 'sched')['ok']  # a dead round frees itself


def test_sync_button_queues_once_and_waits_between_presses(db, logged):
    import json
    import pytest
    from desk.errors import ActionError
    staff = wayne(db, logged[2])
    assert fb.sync_state(db)['state'] == 'idle'
    fb.request_sync(db, staff)
    s = fb.sync_state(db)
    assert s['state'] == 'queued' and s['request']
    with pytest.raises(ActionError):
        fb.request_sync(db, staff)  # already queued
    # the PC picks it up, then finishes
    db.execute("INSERT INTO desk_marks(name,value) VALUES('sync_state',%s)", (json.dumps(dict(state='running', request=s['request'])),))
    assert fb.sync_state(db)['state'] == 'running'
    db.execute("UPDATE desk_marks SET value=%s WHERE name='sync_state'", (json.dumps(dict(state='idle', request=s['request'], text='送出 2 則、新進 1 則')),))
    done = fb.sync_state(db)
    assert done['state'] == 'idle' and done['text'] == '送出 2 則、新進 1 則' and done['wait'] > 0
    with pytest.raises(ActionError):
        fb.request_sync(db, staff)  # pressed again too soon
    db.execute("UPDATE desk_marks SET at=NOW()-INTERVAL '4 minutes' WHERE name='sync_request'")
    fb.request_sync(db, staff)
    assert fb.sync_state(db)['state'] == 'queued'


def test_closed_by_a_colleague_queues_the_star_removal(db):
    ask = [dict(role='user', text='尾門多少錢', at='下午3:00')]
    fb.push(db, [thread(key='950040', messages=ask)])
    fb.push(db, [thread(key='950040', messages=ask + [dict(role='page', text='您好，國產車完工價 18,000', at='下午3:10')])])
    c = db.one("SELECT status,unstar FROM desk_fb_threads WHERE key='950040'")
    assert c == {'status': 'done', 'unstar': 'todo'}
    assert [x['auto'] for x in fb.to_unstar(db) if x['key'] == '950040'] == [False]
    fb.push(db, [thread(channel='line_message', key='U950041', messages=ask)])
    fb.push(db, [thread(channel='line_message', key='U950041', messages=ask + [dict(role='page', text='您好，國產車完工價 18,000', at='下午3:10')])])
    assert db.one("SELECT unstar FROM desk_fb_threads WHERE key='U950041'")['unstar'] == ''  # LINE has no star


def test_fb_ig_no_reply_closes_by_itself_and_drops_the_star(db):
    """Wayne 11/17 (A): 「建議不回覆」 FB/IG comments and messages leave 待審核 like LINE's; nothing is sent."""
    fb.push(db, [thread(channel='ig_comment', key='950050#1', messages=[dict(role='user', text='我便秘都用這罐', at='下午8:00')])])
    fb.push(db, [thread(channel='fb_comment', key='950051#1', messages=[dict(role='user', text='WD40 不可以噴皮膚', at='下午8:00')])])
    engine = Mock()
    engine.draft.side_effect = [{'draft': fb.NO_REPLY, 'flags': [], 'staff_note': '玩笑留言'},
                                {'draft': '您好～謝謝您的提醒', 'flags': []}]
    fb.draft_once(db, lambda store: engine)
    a = db.one("SELECT status,unstar,draft FROM desk_fb_threads WHERE key='950050#1'")
    b = db.one("SELECT status FROM desk_fb_threads WHERE key='950051#1'")
    assert a['status'] == 'skip' and a['unstar'] == 'todo' and a['draft']['auto'] == 'noreply'
    assert b['status'] == 'ready'
