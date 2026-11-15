"""Wayne 11/15 facts (Grok's bug report, reviewed by ChatGPT and Gemini): rules every draft gets,
and code checks that flag a draft breaking them. Synthetic data, TEMP tables, rollback."""
from desk import engine, lookup
from desk.db import now
from desk.engine import Engine, fact_flags
from test_desk import db
from test_lookup import seed


def draft_with(db, text, said):
    calls = []
    def provider(messages):
        calls.append(messages)
        return {'brand': None, 'model': None, 'products': []} if len(calls) == 1 else {'draft': text}
    out = Engine(db, provider).draft([{'time': '2026-11-15 10:00:00', 'role': 'user', 'text': said, 'new': True}], now(), channel='fb_message')
    return out, calls[1][0]['content']


def test_every_draft_gets_the_facts_and_no_fixed_installment_lines(db):
    from desk.tools.common import WORKSPACE
    rules_file = WORKSPACE / 'AI客服訓練' / 'kb_v2' / '口吻規則.md'
    if rules_file.exists():  # the tone rules as they will be uploaded, not the copy production has today
        db.put_kb('口吻規則.md', rules_file.read_text(encoding='utf-8'))
    _, rules = draft_with(db, '您好', '請問 RAV4 電動尾門多少錢')
    assert '22,800' in rules and 'KICKS' in rules and '11/28' in rules and '4～6 小時' in rules and '另收 10%' in rules
    assert '苗栗以北可安排師傅到府施工' not in rules and '無卡分期只要身分證' not in rules


def test_flags_catch_the_mistakes_grok_found(db):
    out, _ = draft_with(db, '您好～Sienta 可以升級電動尾門，完工價 $18,000', '2021 Sienta 電動尾門多少錢')
    assert any('Sienta' in f for f in out['flags'])
    assert fact_flags('環景介紹：https://reurl.cc/epxjQR', '')
    assert fact_flags('無卡分期也有 0 利率', '')
    assert fact_flags('11/28 門市有營業喔', '11/28投票日 你們也休息吧')
    assert fact_flags('一般轎車也有可升級的車款喔', '一般轎車可以裝嗎')
    # what Wayne wants instead raises nothing
    assert not fact_flags('Sienta 的電動尾門套件目前暫時沒有提供販售及安裝，不好意思', 'Sienta 電動尾門多少錢')
    assert not fact_flags('無卡分期另收 10% 手續費', '可以分期嗎')
    assert not fact_flags('11/28 是投票日，小編跟門市確認後馬上回覆您', '11/28 有開嗎 投票日')


def test_car_short_names_address_and_xiaobian_opening(db):
    """Wayne 11/15: Yc＝Yaris Cross, GS300 三代, a customer asking our address, 「小編整理…」 openings."""
    engine_ = Engine(db, type('P', (), {'public': (), 'models': frozenset()})())
    assert {'YC', 'CC'} <= engine_.provider.models
    _, rules = draft_with(db, '您好', 'Yc2026 電動尾門')
    assert 'Yaris Cross' in rules and '國慶路213巷3號' in rules and '不要再問車款' in rules
    asked = '2026-11-15 10:00 客人：地址請示出？'
    assert any('地址' in f for f in fact_flags('請問您在哪個縣市呢？全台經銷商門市查詢：https://reurl.cc/kpdGgK', asked))
    assert not any('地址' in f for f in fact_flags('板橋門市：新北市板橋區國慶路213巷3號，電話 02-29525859', asked))
    assert any('小編' in f for f in fact_flags('您好～完工價 $18,500，小編整理功能給您參考。\n👉 四鏡頭', ''))
    assert not any('小編' in f for f in fact_flags('小編提供您各項報價：\n👉 電動尾門 $18,000', ''))
    assert not any('小編' in f for f in fact_flags('您好～完工價 $18,500\n小編可以協助介紹鄰近門市', ''))


def test_references_match_the_product_and_the_car(db):
    seed(db)
    db.put_kb('canned_v2.json', [dict(topic='Tesla 電動遮陽簾（一般款／星空款）報價', text='Tesla 電動遮陽簾 一般款 電動 完工價'),
                                 dict(topic='電動尾門銷售回覆', text='電動尾門完工價 $18,000（含安裝）電動')])
    db.execute("INSERT INTO desk_qa VALUES(910010, NOW()-INTERVAL '1 days', '電動尾門保固多久', '電動尾門保固一年')")
    titles = [c['title'] for c in lookup.for_thread(db, [dict(role='user', text='RAV4 電動尾門 電動 多少錢')])['canned']]
    assert titles == ['電動尾門銷售回覆']  # no sunshade quote for a tailgate question on a Toyota
    assert lookup.for_thread(db, [dict(role='user', text='Model Y 電動遮陽簾 電動 多少錢')])['canned'][0]['title'].startswith('Tesla')
    assert lookup.for_thread(db, [dict(role='user', text='WRM-418 後視鏡保固多久')])['history'] == []  # not the tailgate warranty


def test_a_reference_wayne_picked_beats_the_ad_button_ask_the_car_first_rule(db):
    calls = []
    def provider(messages):
        calls.append(messages)
        return {'brand': None, 'model': None, 'products': []} if len(calls) == 1 else {'draft': '您好'}
    Engine(db, provider).draft([{'time': '2026-11-15 16:28:00', 'role': 'user', 'text': '安裝要多久？價格多少？', 'note': '廣告預設問題', 'new': True}],
                               now(), channel='fb_message', instruction='請依照這份參考資料重寫草稿：【制式回覆〈電動尾門〉】國產 18,000／進口 20,000',
                               original_draft='請問您的車款、年份？')
    sent = ''.join(m['content'] for m in calls[1])
    assert '這次指示優先' in sent and '最後再請客人提供車款年份' in sent
    assert '寫一則簡短的真人跟進' not in calls[1][0]['content']  # the ask-the-car-first rule is not given at all then


def test_without_an_instruction_ad_button_leads_still_get_the_short_follow_up(db):
    _, rules = draft_with(db, '您好', '安裝要多久？價格多少？')
    assert '寫一則簡短的真人跟進' in rules
