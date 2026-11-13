"""Official LINE account on the same review page (Wayne 11/13). Synthetic data, TEMP tables, rollback; nothing is sent."""
from unittest.mock import Mock
from desk import fb, learning
from desk.db import now
from desk.engine import Engine
from test_desk import db, logged


def line(messages, key='Utestchat1'):
    return dict(channel='line_message', key=key, name='合成LINE客人', url='',
                context='', messages=messages)


def test_line_conversation_joins_the_desk_and_menu_taps_are_not_questions(db):
    fb.push(db, [line([dict(role='user', text='所有門市', at='2026-11-13T10:00:00+08:00', note='選單'),
                       dict(role='page', text='各地區門市如下…', at='2026-11-13T10:00:01+08:00', note='自動回應')])])
    c = fb.conversation(db, fb.thread_id('line_message', 'Utestchat1'))
    assert c['status'] == 'skip'  # only a rich-menu tap: nobody is waiting
    fb.push(db, [line([dict(role='user', text='所有門市', at='2026-11-13T10:00:00+08:00', note='選單'),
                       dict(role='page', text='各地區門市如下…', at='2026-11-13T10:00:01+08:00', note='自動回應'),
                       dict(role='user', text='RAV4 電動尾門多少錢', at='2026-11-13T10:05:00+08:00')])])
    c = fb.conversation(db, fb.thread_id('line_message', 'Utestchat1'))
    assert c['status'] == 'waiting' and fb.LABELS['line_message'] == 'LINE 官方'


def test_colleague_reply_in_line_back_office_closes_and_is_learned_as_line(db):
    msgs = [dict(role='user', text='RAV4 電動尾門多少錢', at='2026-11-13T10:05:00+08:00')]
    fb.push(db, [line(msgs)])
    msgs.append(dict(role='page', text='您好～RAV4 電動尾門完工價 $18,000（含安裝），安裝約 3 小時，歡迎先到門市體驗參觀！', at='2026-11-13T10:20:00+08:00'))
    fb.push(db, [line(msgs)])
    c = fb.conversation(db, fb.thread_id('line_message', 'Utestchat1'))
    assert c['status'] == 'done'
    assert [x['source'] for x in learning.candidates(db) if 'RAV4' in x['question']] == ['line']


def test_line_drafts_follow_line_rules_and_may_suggest_picture_cards(db):
    provider = Mock(side_effect=[{}, dict(draft='您好～\n\n👉 一\n👉 二\n👉 三\n詳細介紹：https://carandroid.com.tw/電動尾門/\n\n請問您在哪個縣市呢？歡迎先到門市體驗參觀，滿意再安裝就可以囉！',
                                          cards=[1])])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='line_message')
    rules, prompt = provider.call_args.args[0][0]['content'], provider.call_args.args[0][1]['content']
    assert 'LINE 官方帳號的私訊' in rules and '[[下一則]]' in out['draft']
    assert ('常用圖片' in prompt) == bool(db.one('SELECT COUNT(*) AS n FROM desk_media_library WHERE active')['n'])  # Wayne 11/13: LINE sends pictures too


def test_meta_run_and_line_run_each_get_only_their_own_approvals(db, logged):
    from test_fb import thread, wayne
    import uuid
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread(), line([dict(role='user', text='RAV4 電動尾門多少錢', at='2026-11-13T10:05:00+08:00')])])
    engine = Mock(); engine.draft.return_value = {'draft': '您好～完工價 $18,000。', 'flags': []}
    fb.draft_once(db, lambda store: engine)
    for ch, key in (('fb_message', '900001'), ('line_message', 'Utestchat1')):
        tid = fb.thread_id(ch, key)
        fb.approve(db, boss, dict(thread_id=tid, text='您好～完工價 $18,000。', version=fb.conversation(db, tid)['version'],
                                  retry_key=str(uuid.uuid4()), confirmed=True, public_confirmed=False))
    assert [p['channel'] for p in fb.pending(db)] == ['fb_message']
    line_items = fb.pending(db, line=True)
    assert [p['channel'] for p in line_items] == ['line_message']
    assert line_items[0]['url'] == 'https://chat.line.biz/Uec58b69514d4768bb1176cd4b2a22ef6/chat/Utestchat1'


def test_line_chat_ids_written_in_groups_of_four_are_joined_back(db):
    fb.push(db, [line([dict(role='user', text='RAV4 電動尾門多少錢', at='2026-11-13T10:05:00+08:00')], key='Ute-stc-hat1')])
    assert fb.conversation(db, fb.thread_id('line_message', 'Utestchat1'))['key'] == 'Utestchat1'
