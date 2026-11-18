"""Wayne 11/18: 「AI 學得如何」— 首稿可用率、重大錯誤、抽查正確率、樣本 (Claude／ChatGPT／Gemini consensus)."""
import uuid
from datetime import datetime, timedelta
from desk import quality
from test_desk import db, logged  # noqa: F401
from test_fb import wayne


def test_what_counts_as_usable_and_as_a_major_error():
    g = lambda ai, sent, redrafted=False: quality.grade(dict(ai_draft=ai, content=sent, redrafted=redrafted))
    same = '您好～B42 MAX 完工價 12,800 元，施工約 90 分鐘，歡迎來門市看看。'
    assert g(same, same) == dict(ratio=1.0, errors=[], usable=True, unchanged=True)
    assert g(same, same, redrafted=True)['usable'] is False  # 叫 AI 重擬過才送出的不算第一次就能用
    assert g(same, same.replace('～', '！'))['usable'] is True  # only the tone changed
    price = g(same, same.replace('12,800', '13,800'))
    assert price['usable'] is False and price['errors'] == ['金額 12,800 被改掉']
    assert g(same, same.replace('12,800', '12800'))['errors'] == []  # same amount, other way of writing it
    phone = '電話 02-29525859，國外請撥 +886-2-29525859'
    assert g(phone, '電話 (02) 2952-5859，國外請撥 +886 2 2952 5859')['errors'] == []  # only reformatted
    assert g(phone, '請直接私訊我們')['errors'] == ['電話 02-29525859、+886-2-29525859 被拿掉']  # no 金額 from a phone
    link = '影片 https://reurl.cc/abc 看看'
    assert g(link, '影片看看')['errors'] == ['連結 https://reurl.cc/abc 被拿掉']
    added = g('完工價 12,800 元', '完工價 12,800 元，影片 https://reurl.cc/abc')
    assert added['errors'] == [] and added['usable'] is False  # 你自己多補的不算錯，但那則不算可用
    assert g('2026 年款可以裝', '2025 年款可以裝')['errors'] == []  # a year is no amount


def _thread(db, n, ad=False):
    tid = f'fb:quality-{n}'
    db.execute("""INSERT INTO desk_fb_threads(id,channel,key,name,context) VALUES(%s,'fb_message',%s,%s,%s)""",
               (tid, f'psid:q{n}', f'客人{n}', '廣告「電動尾門」' if ad else ''))
    return tid


def _approved(db, staff, tid, ai, sent, at, question='B42 多少錢'):
    db.execute("""INSERT INTO desk_fb_attempts(retry_key,thread_id,staff_id,source_version,content,public_confirmed,status,
      question,ai_draft,approved_at) VALUES(%s,%s,%s,1,%s,FALSE,'sent',%s,%s,%s)""",
               (str(uuid.uuid4()), tid, staff['id'], sent, question, ai, at))


def test_report_counts_from_the_baseline_and_says_when_there_are_too_few(db, logged, monkeypatch):
    app, client, staff, _ = logged
    assert client.get('/desk/api/quality').status_code == 403  # only Wayne
    staff = wayne(db, staff)
    now = datetime.now(quality.TAIPEI)
    monkeypatch.setattr(quality, 'BASELINE', now - timedelta(days=3))
    good = '您好～B42 MAX 完工價 12,800 元，歡迎來門市。'
    _approved(db, staff, _thread(db, 0), good, good, now - timedelta(days=5))  # before the baseline: not counted
    for i in range(1, 5):
        _approved(db, staff, _thread(db, i, ad=i < 3), good, good, now - timedelta(hours=i))
    redone = _thread(db, 5)
    db.execute("INSERT INTO desk_fb_redrafts(thread_id,staff_id,instruction) VALUES(%s,%s,'短一點')", (redone, staff['id']))
    _approved(db, staff, redone, good, good, now)
    _approved(db, staff, _thread(db, 6), good, good.replace('12,800', '9,999'), now - timedelta(minutes=5), '保固多久')
    d = client.get('/desk/api/quality').get_json()
    c = d['card']
    assert (c['samples'], c['usable'], c['major'], c['redrafted'], c['ads']) == (6, 4, 1, 1, 2)
    assert c['enough'] is False and d['enough'] == quality.ENOUGH  # 6 < 30: the page shows 樣本不足, not 67%
    assert c['spot_checked'] == 4  # unchanged, not redrafted
    assert d['problems'][0]['what'] == '金額 12,800 被改掉' and d['problems'][0]['intent'] == '保固售後'
    cats = {x['name']: x for x in d['categories']}
    assert cats['廣告客人']['samples'] == 2 and cats['一般客人']['samples'] == 4
    assert cats['報價']['gate']['ok'] is False and cats['報價']['gate']['text'].startswith('還差：')
    assert client.get('/desk/quality').status_code == 200


def test_weekly_line_text(db, logged, monkeypatch):
    app, client, staff, _ = logged
    staff = wayne(db, staff)
    monday = datetime(2026, 11, 30, 9, tzinfo=quality.TAIPEI)
    monkeypatch.setattr(quality, 'BASELINE', datetime(2026, 11, 19, tzinfo=quality.TAIPEI))
    assert quality.weekly_text(db, today=monday) is None  # nothing approved last week: no message
    good = '您好～完工價 12,800 元。'
    for i in range(3):
        _approved(db, staff, _thread(db, 10 + i), good, good, datetime(2026, 11, 24, 10 + i, tzinfo=quality.TAIPEI))
    _approved(db, staff, _thread(db, 20), good, good.replace('12,800', '13,800'), datetime(2026, 11, 25, tzinfo=quality.TAIPEI))
    text = quality.weekly_text(db, today=monday)
    lines = text.splitlines()
    assert lines[0] == '🔴 AI 客服草稿上週成績（11/23～11/29）'
    assert lines[1] == f'首稿可用率：樣本不足（4／{quality.ENOUGH} 則）' and lines[2] == '高風險修改：1 次（金額 1）'
    assert lines[3] == '數字比對通過：3／3 則'
    assert len(lines) <= 12 and lines[-1].endswith('/desk/quality')


def test_short_replies_must_keep_more_and_same_errors_are_merged():
    g = lambda ai, sent: quality.grade(dict(ai_draft=ai, content=sent, redrafted=False))
    assert g('您好～這款目前有現貨，可以直接預約安裝喔！', '您好～這款目前需預訂，可以直接預約安裝喔！')['usable'] is False
    rows = [dict(day=f'11/2{i} 10:00', channel='FB 私訊', name=f'客人{i}', what='連結 https://x.cc/a 被拿掉', intent='報價')
            for i in range(3)] + [dict(day='11/19 09:00', channel='IG 私訊', name='客人9', what='金額 1,000 被改掉', intent='其他')]
    out = quality.merged(rows)
    assert [(p['what'], p['times']) for p in out] == [('連結 https://x.cc/a 被拿掉', 3), ('金額 1,000 被改掉', 1)]
    assert out[0]['day'] == '11/22 10:00' and out[0]['more'][0].startswith('11/22')


def test_gate_needs_every_week_full_and_still_a_person_checks():
    w = lambda n, pct, major=0: dict(samples=n, usable_pct=pct, major=major)
    assert quality.gate([w(100, 99)] * 3 + [w(10, 100)])['text'] == '還差：1 週不到 25 則（最近一週 10 則）'
    assert quality.gate([w(30, 99)] * 3 + [w(30, 99, 1)])['ok'] is False
    assert quality.gate([w(30, 99)] * 3 + [w(30, 90)])['text'] == '還差：1 週可用率不到 95%（最低 90%）'
    ok = quality.gate([w(30, 96)] * 4)
    assert ok['ok'] is True and '人工抽查' in ok['text']
