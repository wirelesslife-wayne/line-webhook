"""Replies sent as 2-3 separate messages (Wayne 11/13). Synthetic data, TEMP tables, rollback; nothing is delivered."""
from unittest.mock import Mock
import pytest
from desk import fb
from desk.db import now
from desk.engine import Engine
from desk.errors import ActionError
from test_desk import db, logged
from test_fb import thread, ready, approval, wayne

TWO = '您好～您的 RAV4 可以升級電動尾門。\n\n👉 完工價 $18,000（含安裝）\n[[下一則]]\n安裝約 3 小時，請問您在哪個縣市呢？'


def test_approved_reply_reaches_the_sender_as_separate_messages(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    fb.approve(db, boss, approval(db, text=TWO))
    item = fb.pending(db)[0]
    assert item['parts'] == ['您好～您的 RAV4 可以升級電動尾門。\n\n👉 完工價 $18,000（含安裝）', '安裝約 3 小時，請問您在哪個縣市呢？']
    assert item['content'] == TWO  # what Wayne approved, unchanged


@pytest.mark.parametrize('text', ['第一則\n[[下一則]]\n\n[[下一則]]\n第三則', '一\n[[下一則]]\n二\n[[下一則]]\n三\n[[下一則]]\n四'])
def test_empty_or_too_many_messages_are_refused(db, logged, text):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    with pytest.raises(ActionError):
        fb.approve(db, boss, approval(db, text=text))


def test_public_comment_stays_one_message(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread('fb_comment', context='合成貼文')])
    ready(db, 'fb_comment')
    with pytest.raises(ActionError, match='公開留言只能回一則'):
        fb.approve(db, boss, approval(db, 'fb_comment', text=TWO))


def drafted(db, channel, text):
    provider = Mock(side_effect=[{}, dict(draft=text, alternatives=[dict(label='精簡', draft='短的\n[[下一則]]\n結尾')])])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel=channel)
    return out, provider.call_args.args[0][0]['content']


def test_ai_splits_long_private_replies_but_not_comments(db):
    out, rules = drafted(db, 'fb_message', TWO)
    assert out['draft'] == TWO and out['alternatives'][0]['draft'] == '短的\n[[下一則]]\n結尾' and '[[下一則]]' in rules
    out, rules = drafted(db, 'fb_comment', TWO)
    assert '[[下一則]]' not in out['draft'] and '[[下一則]]' not in out['alternatives'][0]['draft'] and '分兩則送' not in rules
    out, _ = drafted(db, 'ig_message', '一\n[[下一則]]\n\n[[下一則]]\n')  # an empty part is folded back into one message
    assert out['draft'] == '一'


def test_long_quote_is_split_even_if_the_ai_forgot(db):
    """Wayne 11/13: 「以後長回覆都分兩則送」."""
    one = TWO.replace('\n[[下一則]]\n', '\n👉 產品保固一年\n👉 腳踢感應選配 $2,500\n\n')
    out, _ = drafted(db, 'fb_message', one)
    assert out['draft'].split('[[下一則]]')[1].strip() == '安裝約 3 小時，請問您在哪個縣市呢？'
    short = '您好～週日公休喔！\n\n歡迎平日來門市體驗參觀，滿意再安裝就可以囉！'
    out, _ = drafted(db, 'fb_message', short)
    assert '[[下一則]]' not in out['draft']  # no list: stays one message
    out, _ = drafted(db, 'fb_comment', one)
    assert '[[下一則]]' not in out['draft']
