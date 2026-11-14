"""A link must open the product its label names (Wayne 11/14: 「尾門介紹」 opened the surround-view page and the
other way round; short links all look alike). Synthetic data, TEMP tables, rollback."""
from unittest.mock import Mock
import pytest
from desk import fb, links, videos
from desk.db import now
from desk.engine import Engine
from desk.errors import ActionError
from test_desk import db, logged
from test_fb import thread, wayne, ready, approval

TAILGATE, SURROUND, SHOPS = 'https://reurl.test/tail', 'https://reurl.test/360', 'https://reurl.test/shops'
VW = 'https://reurl.test/vw'


def known(db):
    for url, title in ((TAILGATE, '電動尾門推薦｜免鑰匙自動升降｜無限科技'), (SURROUND, '360°環景系統 | 無限科技'), (SHOPS, '經銷通路 | 無限科技'),
                       (VW, 'YouTube：福斯全車系環景改裝大揭密！'), ('https://reurl.test/parts', '週邊配件 | 無限科技')):
        db.execute("INSERT INTO desk_links(url,target,title,state) VALUES(%s,%s,%s,'ok')", (url, url, title))


def verdicts(db, text):
    return [l['verdict'] for l in links.audit(db, text, get=Mock(side_effect=AssertionError('already looked up')))]


def test_a_label_is_the_words_in_front_of_the_link_or_the_line_above():
    text = f'👉 尾門介紹：{TAILGATE}\n安裝影片：\n\n{VW}\n[[下一則]]\n{SHOPS}'
    assert links.labels(text) == {TAILGATE: '尾門介紹', VW: '安裝影片', SHOPS: ''}
    assert links.about('A系列安卓主機 | 整合360環景系統') == ['360 環景', '安卓主機'] and links.about('Z系列安卓盒') == ['安卓盒']
    assert links.about('四錄產品介紹') == ['電子後視鏡'] and links.about('產品介紹') == []


def test_swapped_links_are_caught_and_right_ones_pass(db):
    known(db)
    assert verdicts(db, f'尾門介紹：{SURROUND}\n環景介紹：{TAILGATE}') == ['mismatch', 'mismatch']
    assert verdicts(db, f'尾門介紹：{TAILGATE}\n環景介紹：{SURROUND}') == ['ok', 'ok']
    # a label that names no product: the page must be about something the reply talks about
    assert verdicts(db, f'您好～RAV4 可以加裝電動尾門。\n產品介紹：{SURROUND}') == ['mismatch']
    assert verdicts(db, f'您好～RAV4 可以加裝電動尾門。\n產品介紹：{TAILGATE}\n您可以點選下方連結查看離您最近的據點：\n{SHOPS}') == ['ok', 'ok']
    assert verdicts(db, f'您好～RAV4 可以加裝電動尾門。\n選配：https://reurl.test/parts') == ['ok']  # a page about no single product
    wrong = [l for l in links.audit(db, f'尾門介紹：{SURROUND}') if l['verdict'] == 'mismatch'][0]
    assert '「尾門介紹」' in links.complaint(wrong) and '360°環景系統' in links.complaint(wrong)


def test_approval_is_refused_until_he_says_the_links_are_right(db, logged):
    known(db)
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    text = f'您好～尾門＋環景一起裝。\n尾門介紹：{SURROUND}\n環景介紹：{TAILGATE}'
    with pytest.raises(ActionError) as e:
        fb.approve(db, boss, approval(db, text=text))
    assert e.value.status == 400 and '尾門介紹' in str(e.value) and db.one('SELECT COUNT(*) AS n FROM desk_fb_attempts')['n'] == 0
    assert fb.approve(db, boss, approval(db, text=text, links_confirmed=True))['status'] == 'approved'  # his call in the end


def test_the_draft_is_flagged_when_its_own_links_do_not_match(db):
    known(db)
    provider = Mock(side_effect=[dict(brand='TOYOTA', model='RAV4', products=['電動尾門']), dict(draft=f'您好～可以加裝電動尾門。\n產品介紹：{SURROUND}')])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    assert any('連結和前面寫的產品對不上' in f and SURROUND in f for f in out['flags'])


def test_another_makes_video_is_not_given_and_the_generic_one_is_allowed(db):
    """Wayne 11/14: without a video of the customer's car, the product's generic video; never another make's."""
    known(db)
    assert videos.other_make('福斯全車系環景改裝大揭密！', 'LEXUS') == 'VOLKSWAGEN'
    assert videos.other_make('福斯全車系環景改裝大揭密！', 'Volkswagen') == '' and videos.other_make('福斯全車系環景', None) == ''
    assert videos.other_make('【A34安卓環景一體機】CarPlay/聲控', 'LEXUS') == ''
    draft = f'您好～ES300 可以加裝環景。\n產品影片：{VW}\n歡迎先到門市體驗！'
    provider = Mock(side_effect=[dict(brand='LEXUS', model='ES', products=['環景']), dict(draft=draft)])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': 'ES300 環景多少', 'new': True}], now(), channel='fb_message')
    assert VW not in out['draft'] and any('VOLKSWAGEN' in f for f in out['flags'])
    system = provider.call_args.args[0][0]['content']
    assert '通用影片' in system and '產品影片：網址' in system


def test_saved_replies_from_the_line_back_office_join_as_reference(db):
    db.put_kb('line_saved_replies.json', [dict(topic='生產', text='台灣設計、台灣組裝，主機經過檢驗認證合格，可安心使用'),
                                          dict(topic='刷卡', text='信用卡會收5%手續費', skip=True)])
    provider = Mock(side_effect=[dict(brand=None, model=None, products=[]), dict(draft='您好～')])
    Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '請問主機是台灣組裝的嗎，有經過檢驗認證嗎', 'new': True}], now(), channel='fb_message')
    user = provider.call_args.args[0][1]['content']
    assert 'LINE 後台同事常用短句' in user and '台灣組裝' in user and '5%手續費' not in user


def test_list_counts_each_source_and_can_show_newest_first(db, logged):
    """Wayne 11/14: numbers on the source buttons (only where there is something), oldest first unless he switches."""
    from test_fb import thread as t
    _, client, _, _ = logged
    fb.push(db, [t(key='900011', messages=[dict(role='user', text='尾門多少', at='2026-11-11T09:00:00+08:00')]),
                 t(key='900012', messages=[dict(role='user', text='環景多少', at='2026-11-11T11:00:00+08:00')]),
                 t('ig_comment', key='900013#a', messages=[dict(role='user', text='台中有店嗎', at='2026-11-11T10:00:00+08:00')])])
    old = client.get('/desk/api/fb/list').get_json()
    assert old['per_channel'] == {'fb_message': 2, 'ig_comment': 1} and old['order'] == 'old'
    assert [i['messages'][-1]['text'] for i in old['items']] == ['尾門多少', '台中有店嗎', '環景多少']
    new = client.get('/desk/api/fb/list?order=new').get_json()
    assert [i['messages'][-1]['text'] for i in new['items']] == ['環景多少', '台中有店嗎', '尾門多少']
    assert client.get('/desk/api/fb/list?order=x').status_code == 400
    assert client.get('/desk/api/fb/list?tab=done').get_json()['per_channel'] == {}
