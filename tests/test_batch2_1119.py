"""Wayne 11/19: second batch of the ChatGPT＋Gemini page review — 不發送 asks why (and 「AI 學得如何」 counts it),
衝突題 can show one product at a time."""
from datetime import datetime, timedelta
import pytest
from desk import fb, quality
from desk.errors import ActionError
from desk.tools import load_kb_conflicts
from test_desk import db, logged  # noqa: F401
from test_fb import thread, tid, wayne, ready
from test_kb_conflicts import source
from test_quality import _approved, _thread


def test_skip_asks_why_and_the_quality_page_counts_it(db, logged, monkeypatch):
    app, client, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    version = fb.conversation(db, tid())['version']
    with pytest.raises(ActionError):
        fb.action(db, boss, dict(thread_id=tid(), version=version, reason='because'), 'skip')
    assert fb.conversation(db, tid())['status'] == 'ready'  # a bad reason changes nothing
    fb.action(db, boss, dict(thread_id=tid(), version=version, reason='bad_draft'), 'skip')
    c = fb.conversation(db, tid())
    assert c['status'] == 'skip' and c['note'] == '不回覆：AI 寫不好，自己回'
    assert db.one('SELECT reason FROM desk_fb_skips WHERE thread_id=%s', (tid(),))['reason'] == 'bad_draft'
    now = datetime.now(quality.TAIPEI)
    monkeypatch.setattr(quality, 'BASELINE', now - timedelta(days=1))
    d = client.get('/desk/api/quality').get_json()
    assert d['skipped'] == dict(total=1, bad=1, parts=[dict(label='AI 寫不好，自己回', n=1)])
    good = '您好～完工價 12,800 元。'
    _approved(db, boss, _thread(db, 30), good, good, now)
    lines = quality.weekly_text(db, today=now + timedelta(days=7)).splitlines()
    assert '不發送：1 則（AI 寫不好 1 則）' in lines and len(lines) <= 12


def test_skip_without_a_reason_still_works(db, logged):
    _, _, staff, _ = logged
    boss = wayne(db, staff)
    fb.push(db, [thread()])
    ready(db)
    fb.action(db, boss, dict(thread_id=tid(), version=fb.conversation(db, tid())['version']), 'skip')
    assert fb.conversation(db, tid())['note'] == '不回覆'
    assert db.one('SELECT COUNT(*) AS n FROM desk_fb_skips')['n'] == 0


def test_conflicts_can_show_one_product(db, logged, tmp_path, monkeypatch):
    _, client, staff, _ = logged
    monkeypatch.setattr(load_kb_conflicts, 'store', lambda: db)
    monkeypatch.setattr(load_kb_conflicts, 'ensure_schema', lambda d: None)
    assert load_kb_conflicts.main([source(tmp_path)]) == 0
    wayne(db, staff)
    d = client.get('/desk/api/conflicts').get_json()
    assert {(p['name'], p['n']) for p in d['products']} == {('電子後視鏡', 1), ('不分產品', 1)} and d['product'] == ''
    one = client.get('/desk/api/conflicts?product=電子後視鏡').get_json()
    assert [x['topic'] for x in one['items']] == ['S14 後鏡頭畫質'] and one['open'] == 2  # the tab count stays the total
    assert client.get('/desk/api/conflicts?product=沒有這個').get_json()['items'] == []
    assert client.get('/desk/api/conflicts?product=' + 'x' * 41).status_code == 400
