"""Wayne 11/18: 知識庫衝突題 — he picks today's right answer for topics we used to answer differently."""
import json
from desk.tools import load_kb_conflicts
from test_desk import db, logged  # noqa: F401
from test_fb import wayne


def source(tmp_path):
    p = tmp_path / 'pass2.jsonl'
    groups = [dict(group='電子後視鏡×報價', n=5, conflicts=[dict(topic='S14 後鏡頭畫質', suggest='以廣告為準', options=[
        dict(say='後 1080P', sources=['li12', 'wa3'], latest='2026-05-01'),
        dict(say='STARVIS 2', sources=['官方facts', 'di7'], latest='2026-11-17')])]),
        dict(group='不分產品×付款', n=50, conflicts=[dict(topic='刷卡是否同價', options=[
            dict(say='同價', sources=[], latest=''), dict(say='加 3%', sources=['li1'], latest='2025-12-31')]),
            dict(topic='只有一個說法', options=[dict(say='x')])])]
    p.write_text('\n'.join(json.dumps(g, ensure_ascii=False) for g in groups), encoding='utf-8')
    return str(p)


def post(client, staff, path, data):
    return client.post('/desk/api/conflicts/' + path, json=data, headers={'X-CSRF-Token': staff['csrf']})


def test_load_once_most_asked_first_and_wayne_decides(db, logged, tmp_path, monkeypatch):
    app, client, staff, _ = logged
    monkeypatch.setattr(load_kb_conflicts, 'store', lambda: db)
    monkeypatch.setattr(load_kb_conflicts, 'ensure_schema', lambda d: None)
    path = source(tmp_path)
    assert load_kb_conflicts.main([path]) == 0
    assert load_kb_conflicts.main([path]) == 0  # a rerun adds nothing
    assert db.one('SELECT COUNT(*) AS n FROM desk_kb_conflicts')['n'] == 2  # the one-option topic is no conflict
    assert app.test_client().get('/desk/api/conflicts').status_code in (302, 401)
    assert client.get('/desk/api/conflicts').status_code == 403  # only Wayne
    staff = wayne(db, staff)
    d = client.get('/desk/api/conflicts').get_json()
    assert [x['topic'] for x in d['items']] == ['刷卡是否同價', 'S14 後鏡頭畫質'] and d['open'] == 2
    s14 = d['items'][1]
    assert s14['options'][1]['named'] == ['公司正式規則'] and s14['options'][1]['replies'] == 1  # past ids only counted
    assert client.get('/desk/conflicts').status_code == 200
    assert client.post('/desk/api/conflicts/decide', json=dict(id=s14['id'], choice=1)).status_code == 403  # csrf
    assert post(client, staff, 'decide', dict(id=s14['id'], choice=2)).status_code == 400
    assert post(client, staff, 'decide', dict(id=s14['id'], choice=-1, answer=' ')).status_code == 400
    assert post(client, staff, 'decide', dict(id=999999, choice=0)).status_code == 404
    out = post(client, staff, 'decide', dict(id=s14['id'], choice=1)).get_json()
    assert out['decided'] == 1 and out['open'] == 1
    card = d['items'][0]['id']
    assert post(client, staff, 'decide', dict(id=card, choice=None)).get_json()['later'] == 1
    assert post(client, staff, 'decide', dict(id=card, choice=-1, answer='刷卡與現金同價')).get_json()['decided'] == 2
    done = client.get('/desk/api/conflicts?tab=decided').get_json()['items']
    assert {(x['choice'], x['answer']) for x in done} == {(1, ''), (-1, '刷卡與現金同價')}
    assert post(client, staff, 'reopen', dict(id=card)).get_json()['open'] == 1
    assert post(client, staff, 'reopen', dict(id=card)).status_code == 409
    assert client.get('/desk/api/conflicts?tab=nope').status_code == 400


def test_decided_answers_reach_the_drafting_ai_and_a_new_answer_replaces_the_old(db, logged, tmp_path, monkeypatch):
    """Wayne 11/18: 「你確定 AI 會照著寫？」 — the decided answer must be in what the drafting AI is told, at top priority."""
    from unittest.mock import Mock
    from desk.db import now
    from desk.engine import Engine
    _, client, staff, _ = logged
    monkeypatch.setattr(load_kb_conflicts, 'store', lambda: db)
    monkeypatch.setattr(load_kb_conflicts, 'ensure_schema', lambda d: None)
    load_kb_conflicts.main([source(tmp_path)])
    staff = wayne(db, staff)
    ids = {r['topic']: r['id'] for r in db.all('SELECT id,topic FROM desk_kb_conflicts')}

    def system_for(text):
        provider = Mock(side_effect=[{}, dict(draft='您好～')])
        Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': text, 'new': True}], now(), channel='fb_message')
        return provider.call_args.args[0][0]['content']

    assert 'Wayne 決定的統一說法' not in system_for('S14 後鏡頭畫質如何')  # nothing decided yet
    post(client, staff, 'decide', dict(id=ids['S14 後鏡頭畫質'], choice=1))
    post(client, staff, 'decide', dict(id=ids['刷卡是否同價'], choice=-1, answer='刷卡與現金同價'))
    s14 = system_for('S14 後鏡頭畫質如何')
    assert 'Wayne 決定的統一說法' in s14 and 'S14 後鏡頭畫質：STARVIS 2' in s14 and '後 1080P' not in s14
    assert '刷卡是否同價：刷卡與現金同價' in s14  # 不分產品: every draft
    tailgate = system_for('電動尾門多少')
    assert 'S14 後鏡頭畫質' not in tailgate and '刷卡與現金同價' in tailgate  # another product's answer stays out
    post(client, staff, 'decide', dict(id=ids['S14 後鏡頭畫質'], choice=0))  # he changes his mind
    s14 = system_for('S14 後鏡頭畫質如何')
    assert 'S14 後鏡頭畫質：後 1080P' in s14 and 'STARVIS 2' not in s14
    post(client, staff, 'reopen', dict(id=ids['S14 後鏡頭畫質']))
    assert 'S14 後鏡頭畫質' not in system_for('S14 後鏡頭畫質如何')


def test_store_question_shows_the_official_list_and_an_override_needs_his_ok(db, logged, monkeypatch):
    """Wayne 11/18 picked 「A」: ChatGPT, Gemini and Claude warned a pick could silently override the official dealer list."""
    from unittest.mock import Mock
    from psycopg2.extras import Json
    from desk.db import now
    from desk.engine import Engine
    from desk import kb_conflicts
    _, client, staff, _ = logged
    staff = wayne(db, staff)
    official = [s for s in __import__('desk.dealers', fromlist=['stores']).stores(db.kb(['經銷商清單.txt'])['經銷商清單.txt'])
                if s['county'] == '台中']
    assert official  # the real list has 台中 stores
    first = kb_conflicts.SUFFIX.sub('', official[0]['name'])
    db.execute("""INSERT INTO desk_kb_conflicts(digest,grp,topic,options,rank) VALUES('t1','不分產品×門市地點與營業時間',
      '台中現行經銷商門市名單',%s,1)""", (Json([dict(say=f'台中可以到{first}。', sources=['li1'], latest='2026-11-17'),
                                         dict(say='台中主要經銷商為某某汽車音響，0900-000-111。', sources=[], latest='2026-04-01')]),))
    cid = db.one("SELECT id FROM desk_kb_conflicts WHERE digest='t1'")['id']
    item = client.get('/desk/api/conflicts').get_json()['items'][0]
    assert item['official'] and official[0]['phone'] in item['official'][0]['text']  # the official list is shown
    assert item['options'][0]['warn'] == [] and any('0900-000-111' in w for w in item['options'][1]['warn'])
    out = post(client, staff, 'decide', dict(id=cid, choice=1))
    assert out.status_code == 409 and '0900-000-111' in out.get_json()['error']  # asks first
    assert db.one('SELECT status FROM desk_kb_conflicts WHERE id=%s', (cid,))['status'] == 'open'

    def system_for(text):
        provider = Mock(side_effect=[{}, dict(draft='您好～')])
        Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': text, 'new': True}], now(), channel='fb_message')
        return provider.call_args.args[0][0]['content']

    # 「選這個，但以正式資料為準」: kept, no override mark, the prompt says the official list wins
    assert post(client, staff, 'decide', dict(id=cid, choice=1, override=False)).get_json()['override'] is False
    s = system_for('台中哪裡可以裝')
    assert '某某汽車音響，0900-000-111。' + kb_conflicts.OFFICIAL_WINS in s and kb_conflicts.OVERRIDE + '的是' in s
    assert not db.all("SELECT 1 FROM desk_marks WHERE name LIKE 'kbr:%%'")
    # 「改用這個，蓋過正式資料」: marked in the prompt and reported so the official file gets fixed
    post(client, staff, 'reopen', dict(id=cid))
    assert post(client, staff, 'decide', dict(id=cid, choice=1, override=True)).get_json()['override'] is True
    assert '某某汽車音響，0900-000-111。' + kb_conflicts.OVERRIDE in system_for('台中哪裡可以裝')
    report = db.one("SELECT value FROM desk_marks WHERE name LIKE 'kbr:%%'")['value']
    assert '台中現行經銷商門市名單' in report and '"dealers"' in report
    # 「加進正式清單」 (ChatGPT 11/18): the official stores are still listed next to the added one
    post(client, staff, 'reopen', dict(id=cid))
    assert post(client, staff, 'decide', dict(id=cid, choice=1, override='add')).get_json()['scope'] == 'add'
    assert '某某汽車音響，0900-000-111。' + kb_conflicts.ADDED in system_for('台中哪裡可以裝')
    assert client.get('/desk/api/conflicts?tab=decided').get_json()['items'][0]['scope'] == 'add'
    # his own words go through the same check (Gemini 11/18)
    post(client, staff, 'reopen', dict(id=cid))
    assert post(client, staff, 'decide', dict(id=cid, choice=-1, answer='台中請洽 0900-222-333')).status_code == 409
    assert post(client, staff, 'decide', dict(id=cid, choice=-1, answer='台中請洽 0900-222-333', override=False)).get_json()['scope'] == 'official'
    assert '台中請洽 0900-222-333' + kb_conflicts.OFFICIAL_WINS in system_for('台中哪裡可以裝')
    # an option that matches the official list needs no confirmation
    post(client, staff, 'reopen', dict(id=cid))
    out = post(client, staff, 'decide', dict(id=cid, choice=0)).get_json()
    assert out['override'] is False and out['scope'] == ''


def test_amounts_found_anywhere_in_official_data_or_their_sums_are_not_flagged():
    from desk import kb_conflicts as k
    known = {'9600', '3000', '320', '490', '1190'}
    assert k.differs('price', '只報 S14 9,600 加專用後鏡頭 3,000，共 12,600 元', [], known) == []
    assert k.differs('price', 'VIP 1 年 320、永久 1,190', [], known) == []
    assert k.differs('price', '尾門 22,500 元', [], known) == ['金額／數字 22,500 在正式資料裡找不到']
    assert k.differs('', '尾門 22,500 元', [], known) == []  # not a fact question
    assert k.kind('颱風營業是否依停班停課') == '' and k.kind('台中現行經銷商門市名單') == 'dealer'
