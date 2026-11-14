"""待處理 (Wayne 11/15): 'ours' stays until he clears it; 'customer' clears itself when the customer writes.
Synthetic data, TEMP tables, rollback."""
from desk import fb
from desk.fb import hold_from
from test_desk import db, logged
from test_fb import thread, wayne


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
