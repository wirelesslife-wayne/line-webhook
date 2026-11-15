"""待處理 (Wayne 11/15): 'ours' stays until he clears it; 'customer' clears itself when the customer writes.
Synthetic data, TEMP tables, rollback."""
from desk import fb
from desk.fb import hold_from
from test_desk import db, logged
import uuid
from desk.fb import hold_after_send
from test_fb import thread, wayne, ready, tid


def test_ai_note_sets_the_kind_of_hold():
    assert hold_from({'staff_note': '請對帳'}) == 'ours'
    assert hold_from({'staff_note': '要確認營業日'}) == 'ours'
    assert hold_from({'to_human': True, 'staff_note': ''}) == 'ours'
    assert hold_from({'staff_note': '等客人回車款年份'}) == 'customer'
    assert hold_from({'staff_note': ''}) == ''


def test_hold_tab_and_buttons(db, logged):
    _, client, staff, _ = logged
    wayne(db, staff)
    fb.push(db, [thread(key='940001', messages=[dict(role='user', text='一般轎車可以裝尾門嗎', at='2026-11-15T09:00:00+08:00')])])
    tid = db.one("SELECT id FROM desk_fb_threads WHERE key='940001'")['id']
    csrf = staff['csrf'] if isinstance(staff, dict) and 'csrf' in staff else None
    headers = {'X-CSRF-Token': csrf} if csrf else {}
    assert client.post('/desk/api/fb/hold', json=dict(thread_id=tid, hold='customer'), headers=headers).status_code == 200
    items = client.get('/desk/api/fb/list?tab=hold').get_json()['items']
    assert [i['id'] for i in items] == [tid] and items[0]['hold'] == 'customer'
    # the customer writes again: back to review, no longer on hold
    fb.push(db, [thread(key='940001', messages=[dict(role='user', text='一般轎車可以裝尾門嗎', at='2026-11-15T09:00:00+08:00'),
                                                dict(role='user', text='2020 Altis', at='2026-11-15T10:00:00+08:00')])])
    assert db.one('SELECT hold FROM desk_fb_threads WHERE id=%s', (tid,))['hold'] == ''
    # ours stays even after the customer writes
    client.post('/desk/api/fb/hold', json=dict(thread_id=tid, hold='ours', note='問廠商'), headers=headers)
    fb.push(db, [thread(key='940001', messages=[dict(role='user', text='一般轎車可以裝尾門嗎', at='2026-11-15T09:00:00+08:00'),
                                                dict(role='user', text='2020 Altis', at='2026-11-15T10:00:00+08:00'),
                                                dict(role='user', text='有消息嗎', at='2026-11-15T11:00:00+08:00')])])
    assert db.one('SELECT hold,hold_note FROM desk_fb_threads WHERE id=%s', (tid,)) == {'hold': 'ours', 'hold_note': '問廠商'}
    assert client.get('/desk/api/fb/list?tab=todo').get_json()['hold'] == 1
    client.post('/desk/api/fb/hold', json=dict(thread_id=tid, hold=''), headers=headers)
    assert client.get('/desk/api/fb/list?tab=hold').get_json()['items'] == []


def test_what_was_sent_decides_the_next_step():
    assert hold_after_send('您好～完工價 $18,500。\n請問您在哪個縣市呢？') == 'customer'
    assert hold_after_send('您好～完工價 $18,500。[[下一則]]請問您的車款和年份是？') == 'customer'
    assert hold_after_send('收到，我們確認後再回覆您，謝謝') == 'ours'
    assert hold_after_send('需要先確認車款是否支援，請問年份？門市確認後馬上回覆您') == 'ours'  # both owe: ours
    assert hold_after_send('您好～完工價 $18,000（含安裝），有任何問題歡迎再詢問～') == ''


def approve_and_send(db, boss, key, text):
    t = db.one('SELECT id,version FROM desk_fb_threads WHERE key=%s', (key,))
    out = fb.approve(db, boss, dict(thread_id=t['id'], text=text, version=t['version'], retry_key=str(uuid.uuid4()),
                                    confirmed=True, public_confirmed=False))
    return out['attempt_id']


def test_sending_updates_the_hold_but_never_wayne_s_own_mark(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    for key in ('950001', '950002', '950003'):
        fb.push(db, [thread(key=key, messages=[dict(role='user', text='電動尾門多少錢？', at='2026-11-15T09:00:00+08:00')])])
    ready(db); ready(db); ready(db)
    db.execute("UPDATE desk_fb_threads SET hold='ours',hold_note='AI：要轉真人' WHERE key IN ('950001','950002')")
    fb.set_hold(db, boss, dict(thread_id=db.one("SELECT id FROM desk_fb_threads WHERE key='950003'")['id'], hold='ours', note='問廠商'))
    a1 = approve_and_send(db, boss, '950001', '完工價 $18,000，請問您的車款年份？')
    a2 = approve_and_send(db, boss, '950002', '完工價 $18,000（含安裝），歡迎來門市看實機。')
    a3 = approve_and_send(db, boss, '950003', '完工價 $18,000（含安裝）。')
    for a in (a1, a2, a3):
        fb.record_result(db, a, 'sent')
    got = {r['key']: r['hold'] for r in db.all("SELECT key,hold FROM desk_fb_threads WHERE key LIKE '95000%%'")}
    assert got == {'950001': 'customer', '950002': '', '950003': 'ours'}


def test_approved_replies_go_out_in_the_order_wayne_approved_them(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    keys = ['960009', '960001', '960005']  # thread ids sort differently from approval order
    for key in keys:
        fb.push(db, [thread(key=key, messages=[dict(role='user', text='價格？', at='2026-11-15T09:00:00+08:00')])])
    for _ in keys:
        ready(db)
    for n, key in enumerate(keys):
        approve_and_send(db, boss, key, '完工價 $18,000。')
        db.execute("""UPDATE desk_fb_attempts SET approved_at=NOW()+%s*INTERVAL '1 minute'
          WHERE thread_id=(SELECT id FROM desk_fb_threads WHERE key=%s)""", (n, key))
    order = [p['key'] for p in fb.pending(db)]
    assert order == keys


def test_hold_tab_counts_both_kinds_and_search_finds_a_name_everywhere(db, logged):
    _, client, staff, _ = logged
    wayne(db, staff)
    fb.push(db, [thread(key='970001', messages=[dict(role='user', text='價格？', at='2026-11-15T09:00:00+08:00')])])
    db.execute("UPDATE desk_fb_threads SET hold='customer',hold_at=NOW(),status='done',name='王大明' WHERE key='970001'")
    d = client.get('/desk/api/fb/list?tab=hold').get_json()
    assert d['hold'] == 1 and d['hold_customer'] == 1 and d['hold_ours'] == 0
    found = client.get('/desk/api/fb/list?tab=search&q=' + '大明').get_json()
    assert [i['name'] for i in found['items']] == ['王大明'] and found['items'][0]['status'] == 'done'
    assert client.get('/desk/api/fb/list?tab=search&q=%25').get_json()['items'] == []  # a % is a letter, not a wildcard
    assert client.get('/desk/api/fb/list?tab=search&q=').status_code == 400


def test_the_ai_s_do_not_reply_marker_can_never_be_sent(db, logged):
    """Wayne 11/15 15:27: off-topic comments get no answer; the AI says so in the draft and approving it is refused."""
    import pytest
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread(key='980001', messages=[dict(role='user', text='這支架哪裡買', at='2026-11-15T09:00:00+08:00')])])
    ready(db)
    with pytest.raises(fb.ActionError):
        approve_and_send(db, boss, '980001', fb.NO_REPLY)
    assert db.one("SELECT status FROM desk_fb_threads WHERE key='980001'")['status'] == 'ready'


def test_waiting_for_the_customer_closes_itself_after_three_days(db):
    """Wayne 11/15 22:06: ⏳ with no answer for 3 days is closed; nothing is sent, a queued reply is left alone."""
    for key, status in (('940101', 'ready'), ('940102', 'done'), ('940103', 'approved'), ('940104', 'ready')):
        fb.push(db, [thread(key=key, messages=[dict(role='user', text='尾門多少', at='2026-11-10T09:00:00+08:00')])])
        db.execute("UPDATE desk_fb_threads SET status=%s,hold='customer',hold_at=NOW()-INTERVAL '4 days' WHERE key=%s", (status, key))
    db.execute("UPDATE desk_fb_threads SET hold_at=NOW()-INTERVAL '2 days' WHERE key='940104'")   # not yet 3 days
    assert fb.close_stale_waits(db) == 2
    got = {r['key']: (r['status'], r['hold']) for r in db.all("SELECT key,status,hold FROM desk_fb_threads WHERE key LIKE '9401%%'")}
    assert got == {'940101': ('done', ''), '940102': ('done', ''), '940103': ('approved', 'customer'), '940104': ('ready', 'customer')}
    assert db.one("SELECT COUNT(*) AS n FROM desk_fb_attempts WHERE thread_id IN (SELECT id FROM desk_fb_threads WHERE key LIKE '9401%%')")['n'] == 0


def test_skip_asks_the_meta_run_to_remove_the_star(db, logged):
    """Wayne 11/15 23:50: 不發送 -> the next Meta run removes 持續追蹤; LINE has no star and is left out."""
    from desk.tools import fb_browser
    import json
    from test_fb import ready
    _, client, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread(key='940201')])
    ready(db)
    t = db.one("SELECT id,version FROM desk_fb_threads WHERE key='940201'")
    assert fb.action(db, boss, dict(thread_id=t['id'], version=t['version']), 'skip') == {'ok': True}
    todo = fb.to_unstar(db)
    assert [x['thread_id'] for x in todo] == [t['id']] and todo[0]['name'] == '合成測試客人'
    assert fb.unstarred(db, t['id']) == {'ok': True}
    assert fb.to_unstar(db) == []
    db.execute("UPDATE desk_fb_threads SET channel='line_message', unstar='' WHERE id=%s", (t['id'],))
    assert fb.to_unstar(db) == []


def test_list_says_how_many_older_messages_and_history_returns_them_all(db, logged):
    """Wayne 11/15 23:50: the list carries the last 12; 顯示更早的對話 loads every message."""
    _, client, staff, _ = logged
    msgs = [dict(role='user', text=f'第 {n} 則', at=f'2026-11-15T{9 + n // 60:02d}:{n % 60:02d}:00+08:00') for n in range(15)]
    fb.push(db, [thread(key='940301', messages=msgs)])
    t = db.one("SELECT id FROM desk_fb_threads WHERE key='940301'")['id']
    item = next(i for i in client.get('/desk/api/fb/list?tab=todo').get_json()['items'] if i['id'] == t)
    assert len(item['messages']) == 12 and item['older'] == 3
    full = client.get('/desk/api/fb/history?thread_id=' + t).get_json()['messages']
    assert len(full) == 15 and full[0]['text'].startswith('第 0 則')
