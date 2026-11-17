"""Wayne 11/18: drafts for the new S14 / HR-V tailgate / 360 ads."""
from desk.engine import Engine
from desk.fb import merged_context
from desk.privacy import redact


def test_glc_with_a_space_is_a_car_not_a_plate():
    models = frozenset({'GLC', 'CC', 'NX'})
    assert 'G LC 300' in redact('G LC 300，可安裝嗎？完工價多少？', models=models)
    assert 'GLC 300' in redact('GLC 300 可以裝嗎', models=models)
    assert '[車牌]' in redact('車是 ABC 1234', models=models)
    assert '[車牌]' in redact('我的車 G LC 300', models=frozenset({'CC'}))  # unknown model stays masked


def test_every_ad_the_customer_came_from_is_kept():
    a, b = '客人從廣告「尾門」進來', '客人從廣告「S14」進來'
    assert merged_context('', a) == a
    assert merged_context(a, '') == a
    assert merged_context(a, a) == a
    assert merged_context(a, b) == a + '\n' + b
    assert merged_context(a + '\n' + b, b) == a + '\n' + b
    assert merged_context(a, '客人從廣告「尾門」進來（摘要寫法不同）') == a
    assert merged_context('合成貼文：電動尾門', '電動尾門貼文（這次摘要寫法不同）') == '合成貼文：電動尾門'
    assert len(merged_context('x' * 9990, '廣告「新」' + 'y' * 50)) == 10000


def engine_with_table():
    e = Engine.__new__(Engine)
    e.WLF = {'cars': [dict(b='TOYOTA', model='COROLLA CROSS', year=2025, p=0)],
             'plans': [{'A': [dict(code='A34', price=22000)]}]}
    return e


def test_tailgate_question_gets_no_head_unit_prices():
    e = engine_with_table()
    car = dict(brand='TOYOTA', model='COROLLA CROSS', year=2025)
    assert e.price_lookup(dict(car, products=['電動尾門', '腳踢感應'])) == []
    assert e.price_lookup(dict(car, products=['電子後視鏡'], series='WRM-S14')) == []
    assert e.price_lookup(dict(car, products=['安卓主機']))
    assert e.price_lookup(dict(car, products=['360環景系統']))
    assert e.price_lookup(dict(car, products=[]))  # nothing named yet: still give the table
