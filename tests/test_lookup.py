"""參考資料 above a draft and the 查資料 panel; the finished tab says who closed it and when (Wayne 11/14).
Synthetic data, TEMP tables, rollback."""
from desk import fb, lookup
from test_desk import db, logged
from test_fb import thread, wayne


def seed(db):
    lookup._QA.clear()
    db.put_kb('canned_v2.json', [dict(topic='電動尾門銷售回覆', text='電動尾門完工價 $18,000（含安裝）'),
                                 dict(topic='電子後視鏡', text='S14 雙錄 $9,600')])
    db.put_kb('line_saved_replies.json', [dict(topic='刷卡安裝5%', text='刷卡加 5%', skip='作廢'),
                                          dict(topic='尾門腳踢', text='腳踢加購 $2,500')])
    db.execute("INSERT INTO desk_qa VALUES(910001, NOW()-INTERVAL '3 days', 'RAV4 可以裝電動尾門嗎', '可以，完工價 $18,000')")
    db.execute("INSERT INTO desk_qa VALUES(910002, NOW()-INTERVAL '3 days', '台中有店嗎', '有經銷商門市')")


def test_references_match_what_the_customer_asked(db):
    seed(db)
    msgs = [dict(role='user', text='請問 RAV4 電動尾門多少錢'), dict(role='assistant', text='您好')]
    got = lookup.for_thread(db, msgs)
    assert got['canned'][0]['title'] == '電動尾門銷售回覆'
    assert [h['text'] for h in got['history']] == ['可以，完工價 $18,000'] and got['history'][0]['date'].count('/') == 2
    assert not any('5%' in c['text'] for c in lookup.canned_rows(db))  # a saved reply Wayne voided never shows
    assert lookup.search(db, '', 'history', 8) == [] and lookup.for_thread(db, [])['canned'] == []


def test_lookup_routes(db, logged):
    seed(db)
    _, client, _, _ = logged
    fb.push(db, [thread(key='920001', messages=[dict(role='user', text='RAV4 電動尾門多少錢', at='2026-11-11T09:00:00+08:00')])])
    tid = db.one("SELECT id FROM desk_fb_threads WHERE key='920001'")['id']
    refs = client.get('/desk/api/fb/references?thread_id=' + tid).get_json()
    assert refs['canned'] and refs['history'] and refs['query'].startswith('RAV4')
    found = client.get('/desk/api/fb/lookup?kind=canned&q=腳踢尾門').get_json()['items']
    assert found[0]['title'] == 'LINE 預設訊息：尾門腳踢'
    assert client.get('/desk/api/fb/lookup?kind=all&q=x').status_code == 400


def test_finished_tab_lists_last_closed_first_with_the_time(db, logged):
    _, client, _, _ = logged
    fb.push(db, [thread(key='930001', messages=[dict(role='user', text='a', at='2026-11-10T09:00:00+08:00')]),
                 thread(key='930002', messages=[dict(role='user', text='b', at='2026-11-11T09:00:00+08:00')])])
    db.execute("UPDATE desk_fb_threads SET status='skip',note='不回覆',updated_at=NOW()-INTERVAL '1 hour' WHERE key='930002'")
    db.execute("UPDATE desk_fb_threads SET status='skip',note='不回覆',updated_at=NOW() WHERE key='930001'")
    items = client.get('/desk/api/fb/list?tab=done').get_json()['items']
    assert [i['key'] for i in items] == ['930001', '930002'] and all(len(i['closed_at']) == 11 for i in items)


def test_knowledge_page_uses_the_review_page_top(db, logged):
    """Wayne 11/14: it still said 「LINE 對話集中處理」 and linked back to the retired LINE workbench."""
    _, client, staff, _ = logged
    wayne(db, staff)
    page = client.get('/desk/knowledge')
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'href="/desk/fb">客服審稿' in html and '回對話工作台' not in html and 'LINE 對話集中處理' not in html
