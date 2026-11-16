"""Wayne 11/16 11:03: FB/IG customers who only pressed ad buttons, every one answered by the ad's automatic reply,
are closed by themselves (no draft, no review); 待我們確認 card buttons and 10-second undo.
Synthetic data, TEMP tables, rollback; the AI is a Mock, nothing is sent."""
from unittest.mock import Mock
from desk import fb
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
