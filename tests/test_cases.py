"""Install-case links per car model and the 2-3 draft versions (Wayne 11/12). Synthetic data, TEMP tables, rollback."""
from unittest.mock import Mock
from desk import cases, fb
from desk.db import now
from desk.engine import Engine
from test_desk import db

CC_VIDEO = 'https://reurl.test/cc-tailgate'
RAV4_PAGE = 'https://site.test/toyota-rav4-install-cases/'


def page(title, body, slug='x-install-cases'):
    return dict(slug=slug, link='https://site.test/' + slug + '/', title=dict(rendered=title), content=dict(rendered=body))


def seed(db):
    db.execute("INSERT INTO desk_links(url,target,title,state) VALUES(%s,'https://youtube.test/x','YouTube：TOYOTA Corolla cross【電動尾門】','ok')", (CC_VIDEO,))
    db.execute("""INSERT INTO desk_cases(url,brand,model,products) VALUES
      (%s,'TOYOTA','RAV4','2013–2018：A系列安卓環景一體機'),
      ('https://site.test/toyota-corolla-cross-install-cases/','TOYOTA','COROLLA CROSS','電動尾門')""", (RAV4_PAGE,))


def tailgate(db, model, draft, **more):
    provider = Mock(side_effect=[dict(brand='TOYOTA', model=model, products=['電動尾門']), dict(draft=draft, **more)])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    return out, provider


def test_site_page_is_read_as_model_and_installed_products():
    body = ('<p>TOYOTA → RAV4</p><h3>2013–2018 安裝案例 30+</h3><p>實裝產品： A系列安卓環景一體機 WRM-S10四錄電子後視鏡</p>'
            '<a>查看完整實裝案例 →</a><script>var x="實裝產品：假的";</script>')
    c = cases.parse(page('RAV4 安裝案例目錄', body))
    assert c['model'] == 'RAV4' and c['brand'] == 'TOYOTA'
    assert c['products'] == '2013–2018：A系列安卓環景一體機 WRM-S10四錄電子後視鏡'
    assert cases.parse(page('TOYOTA 安裝實例', '<p>選車款</p>')) is None
    assert cases.same_car('RAV4', 'RAV4') and cases.same_car('V60', 'V60/S60') and not cases.same_car('RAV4', 'COROLLA CROSS')


def test_refresh_keeps_the_old_index_when_the_site_is_down(db):
    seed(db)
    assert cases.refresh(db, get=lambda u: Mock(status_code=503)) == 0
    assert db.one('SELECT COUNT(*) AS n FROM desk_cases')['n'] == 2
    pages = [page('CR-V 安裝案例目錄', '<p>HONDA → CR-V</p><p>實裝產品： 電動尾門</p>查看完整', 'honda-crv-install-cases'), page('關於我們', '', 'about')]
    assert cases.refresh(db, get=lambda u: Mock(status_code=200, json=lambda: pages)) == 1
    assert [r['model'] for r in db.all('SELECT model FROM desk_cases')] == ['CR-V']


def test_another_cars_case_video_is_taken_out_of_the_draft(db, monkeypatch):
    """Wayne 11/12: a RAV4 customer got the Corolla Cross tailgate video as 「安裝實例」."""
    seed(db)
    draft = f'您好～RAV4 可以升級電動尾門。\n\n詳細介紹：https://carandroid.com.tw/電動尾門/\n安裝實例：\n{CC_VIDEO}\n\n歡迎來門市看實機～'
    out, provider = tailgate(db, 'RAV4', draft)
    assert CC_VIDEO not in out['draft'] and '安裝實例' not in out['draft'] and '詳細介紹' in out['draft']
    assert any('Corolla Cross' in f.title() or 'COROLLA CROSS' in f for f in out['flags'])
    assert 'install-cases' not in provider.call_args.args[0][1]['content']  # Wayne 11/13: site case pages are off for now
    monkeypatch.setattr('desk.engine.SITE_CASES', True)  # once they are switched back on: only this car's pages
    prompt = tailgate(db, 'RAV4', draft)[1].call_args.args[0][1]['content']
    assert RAV4_PAGE in prompt and 'corolla-cross-install-cases' not in prompt
    out, _ = tailgate(db, 'Corolla Cross', draft)
    assert CC_VIDEO in out['draft']  # same car: the video is right


def test_example_links_say_which_car_they_show(db):
    seed(db)
    eng = Engine(db, Mock())
    eng.store.all = lambda sql, args=None, _all=db.all: (
        [dict(question='電動尾門多少錢', content=f'完工價 $18,000\n安裝實例：{CC_VIDEO}', ai_draft='')] if 'desk_fb_attempts' in sql else _all(sql, args))
    shown = eng.approved('電動尾門多少錢')[0]
    assert f'{CC_VIDEO}＝YouTube：TOYOTA Corolla cross【電動尾門】' in shown


def test_fb_drafts_come_in_two_or_three_versions(db):
    out, provider = tailgate(db, 'RAV4', '版本一', label='完整報價',
                             alternatives=[dict(label='精簡版', draft='版本二'), dict(label='一樣的', draft='版本一'), 'bad', dict(label='先問年份', draft='版本三')])
    assert out['label'] == '完整報價' and [a['label'] for a in out['alternatives']] == ['精簡版', '先問年份']
    assert '2～3 個版本' in provider.call_args.args[0][0]['content']
    provider = Mock(side_effect=[{}, dict(draft='您好～', alternatives=[dict(label='x', draft='別的')])])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '幾點開', 'new': True}], now(), channel='line')
    assert out['alternatives'] == [] and '2～3 個版本' not in provider.call_args.args[0][0]['content']


def test_the_version_wayne_picked_is_recorded_as_the_ai_draft():
    d = dict(draft='您好～完工價 $18,000，歡迎來門市。', alternatives=[dict(label='精簡', draft='完工價 $18,000。')])
    assert fb.chosen(d, '完工價 $18,000！') == '完工價 $18,000。'
    assert fb.chosen(d, '您好～完工價 $18,000，歡迎來門市看看。') == d['draft']
    assert fb.chosen(None, '任何字') == ''
