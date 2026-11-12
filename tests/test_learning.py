"""Wayne 11/12: learn from replies typed directly in FB/IG/LINE, but keep only the ones better than what the desk has.
TEMP tables only; the AI judge is a mock, so nothing is spent."""
from datetime import timedelta
from unittest.mock import Mock
from desk import fb, learning
from desk.db import now
from desk.engine import Engine
from test_desk import db, logged
from test_fb import thread, tid, wayne, ready, approval


def conversation():
    return thread(messages=[
        dict(role='page', text='您好！請告訴我們該如何為你提供協助。', at='1', note='自動回覆'),
        dict(role='user', text='CR-V 電動尾門多少錢？', at='2'),
        dict(role='page', text='稍等幫您查', at='3', note='暫回'),
        dict(role='page', text='您好～CR-V 電動尾門完工價 $18,000，含安裝，約 3 小時，可用原車遙控器開關。', at='4'),
    ])


def line_qa(db, question='週六有營業嗎', answer='週六 10:00～18:00 有營業，隔週六公休，建議先預約比較不用等喔！', days=1, source=1):
    db.execute('INSERT INTO desk_qa(source_id,asked_at,question,answer) VALUES(%s,%s,%s,%s)',
               (source, now() - timedelta(days=days), question, answer))


def judge(results):
    provider = Mock(return_value=dict(results=results))
    return provider, (lambda store: Engine(store, provider))


def test_direct_replies_are_judged_once_and_only_better_ones_kept(db):
    fb.push(db, [conversation()])
    line_qa(db)
    line_qa(db, '多少錢', '好的', source=2)                          # too short to teach anything
    line_qa(db, '舊問題', '這是一個很久以前的同事回覆內容，不再審核了喔', days=30, source=3)  # outside the LINE window
    batch = learning.candidates(db)
    assert [c['source'] for c in batch] == ['fb', 'line']
    assert batch[0]['question'] == 'CR-V 電動尾門多少錢？' and '$18,000' in batch[0]['answer']  # holding reply skipped
    provider, factory = judge([dict(n=1, verdict='better', reason='完整報價'), dict(n=2, verdict='no', reason='範本已有')])
    assert learning.judge_once(db, factory) == 2
    rows = {r['source']: r for r in db.all('SELECT * FROM desk_learned')}
    assert rows['fb']['verdict'] == 'better' and '$18,000' in rows['fb']['answer']
    assert rows['line']['verdict'] == 'no' and rows['line']['answer'] == ''  # not better: nothing kept but the marker
    assert learning.candidates(db) == []  # never judged (or paid for) twice
    # The kept reply now guides drafts for similar questions.
    drafter = Mock(side_effect=[{}, dict(draft='您好～')])
    Engine(db, drafter).draft([{'time': 'now', 'role': 'user', 'text': 'CR-V 電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    system = drafter.call_args.args[0][0]['content']
    assert '直接在 FB 回的回覆（審過，比原本範本好）' in system and '$18,000，含安裝' in system


def test_the_desks_own_sends_and_unanswered_items_are_not_kept(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    out = fb.approve(db, boss, approval(db, text='您好～電動尾門完工價 $18,000，含安裝，約 3 小時。'))
    fb.record_result(db, out['attempt_id'], 'sent')
    fb.push(db, [thread(messages=[dict(role='user', text='請問電動尾門多少錢？', at='2026-11-11T10:00:00+08:00'),
                                  dict(role='page', text='您好～電動尾門完工價 $18,000，含安裝，約 3 小時。', at='x')])])
    assert learning.candidates(db) == []  # already learned as Wayne's approved reply
    line_qa(db)
    provider, factory = judge([])  # the judge skipped it: try again next time instead of marking it
    assert learning.judge_once(db, factory) == 1
    assert db.one('SELECT COUNT(*) AS n FROM desk_learned')['n'] == 0 and len(learning.candidates(db)) == 1
