"""「AI 回覆依據」(Wayne 11/16): his rules, the learned replies he can stop, the official data read only.
Synthetic data, TEMP tables, rollback; nothing is sent."""
import hashlib
import pytest
from desk import basis
from desk.errors import ActionError
from desk.learning import kept
from test_desk import db, logged
from test_fb import wayne


def learned_row(db, n, q, a):
    d = hashlib.sha256(str(n).encode()).hexdigest()
    db.execute("INSERT INTO desk_learned(digest,source,question,answer,verdict,reason) VALUES(%s,'line',%s,%s,'better','補充')", (d, q, a))
    return d


def test_page_rules_examples_and_docs(db, logged):
    _, client, staff, _ = logged
    boss = wayne(db, staff)
    h = {'X-CSRF-Token': staff['csrf']}
    page = client.get('/desk/knowledge')
    assert page.status_code == 200 and 'AI 回覆依據' in page.get_data(as_text=True)
    # 我的回覆規則: added here, used by every draft, stopped and started with the existing switch
    r = client.post('/desk/api/basis/rules', json=dict(text='保固一律說一年'), headers=h)
    assert r.status_code == 201
    rid = r.get_json()['id']
    assert db.one('SELECT active,channel FROM desk_edit_rules WHERE id=%s', (rid,)) == {'active': True, 'channel': 'all'}
    assert client.post('/desk/api/basis/rules', json=dict(text=' '), headers=h).status_code == 400
    assert client.post('/desk/api/fb/rules/%d' % rid, json=dict(active=False), headers=h).status_code == 200
    assert next(x for x in client.get('/desk/api/basis/rules').get_json()['items'] if x['id'] == rid)['active'] is False
    # 過往核准的回覆: search, stop (no longer used by drafts), start again with the original reason back
    a = learned_row(db, 1, '尾門多少錢', '國產車完工價 18,000')
    learned_row(db, 2, '施工多久', '約 90 分鐘')
    got = client.get('/desk/api/basis/learned?q=尾門').get_json()
    assert [x['id'] for x in got['items']] == [a] and got['active'] == 2
    assert client.post('/desk/api/basis/learned', json=dict(id=a, active=False), headers=h).status_code == 200
    assert all(r['question'] != '尾門多少錢' for r in kept(db))
    off = client.get('/desk/api/basis/learned?stopped=1').get_json()
    assert [x['id'] for x in off['items']] == [a] and off['stopped'] == 1
    assert client.post('/desk/api/basis/learned', json=dict(id=a, active=False), headers=h).status_code == 409
    assert client.post('/desk/api/basis/learned', json=dict(id=a, active=True), headers=h).status_code == 200
    assert db.one('SELECT verdict,reason FROM desk_learned WHERE digest=%s', (a,)) == {'verdict': 'better', 'reason': '補充'}
    # 公司正式資料: six read-only documents; the price table answers one car
    docs = client.get('/desk/api/basis/docs').get_json()['items']
    assert [d['key'] for d in docs] == ['price', 'hours', 'dealers', 'canned', 'faq', 'tone'] and all(d['ok'] for d in docs)
    hours = client.get('/desk/api/basis/doc?key=hours').get_json()
    assert hours['kind'] == 'table' and hours['rows']
    assert client.get('/desk/api/basis/doc?key=dealers').get_json()['rows']
    assert any('CX-30' in l for l in client.get('/desk/api/basis/doc?key=price&q=CX-30').get_json()['lines'])
    assert client.get('/desk/api/basis/doc?key=nope').status_code == 404


def test_only_wayne_changes_the_basis(db, logged):
    _, client, staff, _ = logged
    with pytest.raises(ActionError):
        basis.add_rule(db, staff, dict(text='不准'))
    assert client.get('/desk/knowledge').status_code == 403


def test_price_lookup_finds_the_car_typed():
    wlf = {'cars': [dict(b='BMW', model='X3', year=2015, p=0), dict(b='MAZDA', model='CX-30', year=2021, p=0),
                    dict(b='MAZDA', model='CX-3', year=2020, p=0)],
           'plans': [{'DS': [dict(code='DSM4', price=22000)]}]}
    assert [l.split('（')[0] for l in basis.price(wlf, 'CX-30')] == ['MAZDA CX-30']  # not BMW X3, not CX-3
    assert [l.split('（')[0] for l in basis.price(wlf, 'mazda cx30 2021')] == ['MAZDA CX-30']
    assert basis.price(wlf, '') == [] and basis.price(wlf, '2021') == []
