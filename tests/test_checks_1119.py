"""Key-fact chips above the draft (Wayne 11/19, discussed with ChatGPT and Gemini). No database, no network."""
from desk import checks


class FakeStore:
    def kb(self, names):
        return {
            'canned_v2.json': [dict(topic='電動尾門銷售回覆', text='電動尾門國產車完工價 $18,000（含安裝），施工約 3 小時，保固一年。'),
                               dict(topic='安卓機銷售回覆', text='安卓機完工價 $19,000 起'),
                               dict(topic='Tucson L產品報價制式回覆', text='日鈦 SV-72：23,000')],
            'wlf-data.json': {'cars': [dict(b='TOYOTA', model='RAV4'), dict(b='HONDA', model='CR-V'), dict(b='FIAT', model='500'),
                                       dict(b='TOYOTA', model='COROLLA CROSS'), dict(b='TOYOTA', model='COROLLA')]},
            '經銷商清單.txt': '無限科技－板橋店｜新北市板橋區國慶路213巷3號｜02-29525859',
            'install_hours.json': {'items': [{'item': '電動尾門', 'hours': '約 3 小時'}]},
        }

    def all(self, sql, args=()):
        return []


def chips(text, said='我的車是 2020 RAV4，想裝電動尾門', draft=None):
    checks._KB['at'] = 0
    thread = dict(context='', draft=draft or {}, events=[dict(role='user', content=said)])
    return {(c['kind'], c['label']): c for c in checks.chips(FakeStore(), thread, text)}


def test_right_draft_is_all_green_and_phone_digits_are_not_prices():
    out = chips('您好～2020 年 RAV4 可以加裝電動尾門，完工價 $18,000，施工約 3 小時，保固一年。電話 02-29525859')
    assert {k: v['state'] for k, v in out.items()} == {('車款', 'RAV4'): 'ok', ('年份', '2020 年'): 'ok', ('金額', '$18,000'): 'ok',
                                                        ('電話', '02-29525859'): 'ok', ('工時', '3 小時'): 'ok', ('保固', '保固一年'): 'ok'}


def test_wrong_car_year_price_and_warranty_are_caught():
    out = chips('您好～2019 年 CR-V 可以加裝電動尾門，完工價 $19,000，保固三年。')
    assert out[('車款', 'CR-V')]['state'] == 'bad' and 'RAV4' in out[('車款', 'CR-V')]['why']
    assert out[('年份', '2019 年')]['state'] == 'bad'
    # the tailgate's own price is 18,000: 19,000 is a conflict (ChatGPT 11/19), not a maybe
    assert out[('金額', '$19,000')]['state'] == 'bad' and '18,000' in out[('金額', '$19,000')]['why']
    assert out[('保固', '保固三年')]['state'] == 'bad' and '一年' in out[('保固', '保固三年')]['why']


def test_guesses_are_yellow_and_short_names_need_edges():
    out = chips('您好～RAV4 2021 年款可以裝，完工價 $2,500。', said='尾門多少錢')
    assert out[('車款', 'RAV4')]['state'] == 'warn' and out[('年份', '2021 年')]['state'] == 'warn'
    assert not any(k[0] == '車款' and k[1] == '500' for k in out)   # 500 in $2,500 is not a FIAT
    assert checks.years_in('您好～2020 年') == {2020} and checks.years_in('適用 2013～2020 年') == set()
    assert checks.cars_in('Toyota Corolla Cross GR', ['COROLLA CROSS', 'COROLLA']) == ['COROLLA CROSS']


def test_install_time_only_not_features():
    out = chips('支援 10 小時停車監控，施工約 3 小時')
    assert ('工時', '3 小時') in out and ('工時', '10 小時') not in out


def test_amount_with_no_source_is_only_yellow():
    out = chips('您好～這款完工價 $7,777，歡迎來店', said='多少錢')
    assert out[('金額', '$7,777')]['state'] == 'warn'


def test_server_refuses_red_draft_without_the_tick(monkeypatch):
    import uuid
    import pytest
    from desk import fb, links, knowledge
    from desk.errors import ActionError
    monkeypatch.setattr(knowledge, 'require_reviewer', lambda staff: None)
    monkeypatch.setattr(links, 'audit', lambda store, text: [])
    monkeypatch.setattr(checks, 'for_thread', lambda store, tid, text: [dict(kind='車款', label='CR-V', state='bad', why='客人說的是 RAV4')])
    data = dict(thread_id='fb:x', text='您好～CR-V 可以裝', version=1, confirmed=True, retry_key=str(uuid.uuid4()))
    with pytest.raises(ActionError) as e:
        fb.approve(object(), {'id': 1}, data)
    assert '紅燈' in str(e.value)
