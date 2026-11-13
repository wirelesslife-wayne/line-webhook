"""Drafts link our YouTube video of the customer's car, not the website's install-case pages (Wayne 11/13).
Synthetic data, TEMP tables, rollback."""
from unittest.mock import Mock
from desk import videos
from desk.db import now
from desk.engine import Engine
from test_desk import db

NAMES = {'COROLLA', 'COROLLA CROSS', 'CR-V', 'CX-5', 'NX', 'A3', 'FIT', 'RAV4'}
CC, CRV = 'Nanz5A4XkX0', 'rMmgrXua1E8'


def seed(db):
    videos.save(db, [(CC, 'TOYOTA Corolla cross【電動尾門】'), (CRV, '《電吸尾門+腳踢尾門🆙》本田-CRV5'),
                     ('Cj4SqlUwd8M', '《TOYOTA｜COROLLA CROSS 全方位影音升級 》裝這台A34"10吋安卓環景一體機'), ('bad id', 'x'), ('AAAAAAAAAAA', '')])
    db.execute("INSERT INTO desk_links(url,target,title,state) VALUES('https://reurl.test/cc',%s,'YouTube：TOYOTA Corolla cross【電動尾門】','ok')",
               (f'https://youtube.com/shorts/{CC}?si=x',))
    db.execute("INSERT INTO desk_cases(url,brand,model,products) VALUES('https://site.test/toyota-corolla-cross-install-cases/','TOYOTA','COROLLA CROSS','電動尾門')")


def test_a_title_names_only_its_most_specific_car():
    assert videos.cars_in('TOYOTA Corolla cross【電動尾門】', NAMES) == ['COROLLA CROSS']
    assert videos.cars_in('《電吸尾門+腳踢尾門🆙》本田-CRV5', NAMES) == ['CR-V']
    assert videos.cars_in('Mazda CX-50 環景', NAMES) == [] and videos.cars_in('Mazda CX-5 環景', NAMES) == ['CX-5']
    assert videos.cars_in('原廠不給的 Lexus NX350h 升級電動尾門', NAMES) == ['NX']
    assert videos.cars_in('裝這台A34"10吋安卓環景一體機 OUTFIT', NAMES) == []  # A34 is our unit, not an Audi A3
    assert videos.cars_in('Custom-fit design is awesome', NAMES | {'IS'}) == [] and videos.cars_in('Honda FIT 4 電動尾門', NAMES) == ['FIT']


def test_videos_for_this_car_with_the_asked_product_first(db):
    seed(db)
    assert db.one('SELECT COUNT(*) AS n FROM desk_videos')['n'] == 3  # bad id and empty title are not stored
    got = videos.for_car(db, dict(model='Corolla Cross', products=['電動尾門']), NAMES)
    assert got[0] == dict(標題='TOYOTA Corolla cross【電動尾門】', 網址='https://reurl.test/cc')  # the short link Wayne already uses
    assert [g['網址'] for g in got[1:]] == ['https://youtu.be/Cj4SqlUwd8M']
    assert videos.for_car(db, dict(model='COROLLA', products=['電動尾門']), NAMES) == []  # an Altis is not a Corolla Cross
    assert videos.for_car(db, dict(model='CR-V'), NAMES) == [dict(標題='《電吸尾門+腳踢尾門🆙》本田-CRV5', 網址='https://youtu.be/' + CRV)]
    assert videos.for_car(db, dict(model=None), NAMES) == []


def test_new_uploads_come_from_the_channel_feed_once_a_day(db):
    feed = ('<feed><title>頻道</title><entry><id>yt:video:abcdefghijk</id><yt:videoId>abcdefghijk</yt:videoId>'
            '<yt:channelId>UC</yt:channelId><title>HONDA FIT 4 電動尾門 &amp; 腳踢</title></entry></feed>')
    assert videos.refresh_if_due(db, get=lambda u: Mock(status_code=200, text=feed)) == 1
    assert db.one("SELECT title FROM desk_videos WHERE id='abcdefghijk'")['title'] == 'HONDA FIT 4 電動尾門 & 腳踢'
    assert videos.refresh_if_due(db, get=Mock(side_effect=AssertionError('checked twice in a day'))) == 0
    db.execute("UPDATE desk_marks SET at=NOW()-INTERVAL '2 days' WHERE name='videos_feed'")
    assert videos.refresh_if_due(db, get=lambda u: Mock(status_code=503, text='')) == 0  # feed down: nothing lost
    assert db.one('SELECT COUNT(*) AS n FROM desk_videos')['n'] == 1


def draft_for(db, model, text):
    provider = Mock(side_effect=[dict(brand='TOYOTA', model=model, products=['電動尾門']), dict(draft=text)])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    return out, provider.call_args.args[0]


def test_draft_gets_this_cars_videos_and_never_a_website_case_page(db):
    """Wayne 11/13: a customer asked about a 2024 CC tailgate; the draft had no video, earlier ones linked the case page."""
    seed(db)
    text = ('您好～可以升級電動尾門。\n\n👉 完工價 $18,000\n產品介紹：https://reurl.cc/yW3vV6\n安裝影片：https://reurl.test/cc\n'
            '安裝實例：https://site.test/toyota-corolla-cross-install-cases/\n安裝實例：\nhttps://carandroid.com.tw/x-install-cases/\n\n歡迎先到門市體驗參觀！')
    out, (system, user) = draft_for(db, 'COROLLA CROSS', text)
    assert 'https://reurl.test/cc' in out['draft'] and 'install-cases' not in out['draft'] and '安裝實例' not in out['draft']
    assert not [f for f in out['flags'] if 'reurl.test/cc' in f]  # a link the desk offered is not "unknown"
    assert '"videos"' in user['content'] and 'https://reurl.test/cc' in user['content'] and 'install_cases' not in user['content']
    assert '安裝影片：網址' in system['content'] and '先一律不要附' in system['content']
    out, (_, user) = draft_for(db, 'RAV4', text)  # another car's video is still taken out
    assert 'reurl.test/cc' not in out['draft'] and 'reurl.test/cc' not in user['content']
    assert any('COROLLA CROSS' in f for f in out['flags'])
