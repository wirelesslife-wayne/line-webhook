"""Wayne's common pictures: the AI suggests, Wayne attaches with one click, approval still sends. TEMP tables only."""
import hashlib
import secrets
from io import BytesIO
from unittest.mock import Mock
import pytest
from PIL import Image
from desk import fb, library
from desk.db import now
from desk.engine import Engine
from desk.errors import ActionError
from test_desk import db, logged
from test_fb import thread, tid, wayne, ready, approval, post


def card(db, name='電動尾門Q&A.png', use_when='問電動尾門', topic='電動尾門'):
    data = BytesIO()
    Image.new('RGB', (8, 8), 'navy').save(data, 'PNG')
    raw = data.getvalue()
    lib_id = secrets.token_hex(16)
    db.execute("""INSERT INTO desk_media_library(id,filename,topic,summary,use_when,content_type,data,size,sha256)
      VALUES(%s,%s,%s,'摘要',%s,'image/png',%s,%s,%s)""", (lib_id, name, topic, use_when, raw, len(raw), hashlib.sha256(raw).hexdigest()))
    return lib_id


def test_drafts_suggest_only_real_library_cards_for_private_messages(db):
    a, b = card(db), card(db, '三款電子後視鏡.jpg', '問電子後視鏡')
    provider = Mock(side_effect=[{}, dict(draft='您好～', cards=[1, '2', 2, 99, 'x'])])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '電動尾門多少錢', 'new': True}], now(), channel='fb_message')
    prompt = provider.call_args.args[0][1]['content']
    assert '電動尾門Q&A.png' in prompt and '何時附：問電動尾門' in prompt
    numbered = dict(library.catalog(db))
    assert {numbered[1]['id'], numbered[2]['id']} == {a, b}
    assert [c['id'] for c in out['cards']] == [numbered[1]['id'], numbered[2]['id']]  # duplicates and unknown numbers dropped
    provider = Mock(side_effect=[{}, dict(draft='您好～', cards=[1])])
    out = Engine(db, provider).draft([{'time': 'now', 'role': 'user', 'text': '多少錢', 'new': True}], now(), channel='fb_comment')
    assert out['cards'] == [] and '電動尾門Q&A.png' not in provider.call_args.args[0][1]['content']


def test_attach_from_library_then_send_with_the_reply(db, logged):
    _, client, staff, _ = logged
    lib_id = card(db)
    fb.push(db, [thread()])
    ready(db)
    with pytest.raises(ActionError):
        library.attach(db, staff, tid(), lib_id)  # only Wayne attaches
    boss = wayne(db, staff)
    got = library.attach(db, boss, tid(), lib_id)
    assert got['name'] == '電動尾門Q&A.png'
    with pytest.raises(ActionError, match='已經附上'):
        library.attach(db, boss, tid(), lib_id)
    out = fb.approve(db, boss, approval(db, media_ids=[got['id']]))
    assert out['status'] == 'approved'
    assert db.one('SELECT COUNT(*) n FROM desk_fb_media WHERE attempt_key IS NOT NULL')['n'] == 1


def test_library_routes(db, logged):
    _, client, staff, _ = logged
    wayne(db, staff)
    lib_id = card(db)
    db.execute("UPDATE desk_media_library SET active=FALSE WHERE id<>%s", (lib_id,))
    cards = client.get('/desk/api/fb/library').get_json()['cards']
    assert [c['id'] for c in cards] == [lib_id]
    assert client.get('/desk/api/fb/library/' + lib_id).mimetype == 'image/png'
    assert client.get('/desk/api/fb/library/' + '0' * 32).status_code == 404
    fb.push(db, [thread()])
    ready(db)
    r = post(client, staff, 'media/library', dict(thread_id=tid(), library_id=lib_id))
    assert r.status_code == 201 and r.get_json()['media']['name'] == '電動尾門Q&A.png'
    assert post(client, staff, 'media/library', dict(thread_id=tid(), library_id='bad')).status_code == 400


def test_monthly_cards_switch_off_after_their_date(db, logged):
    _, client, staff, _ = logged
    live, old = card(db, '11月營業月曆.png'), card(db, '10月營業月曆.png')
    db.execute("UPDATE desk_media_library SET expires_on=CURRENT_DATE+1 WHERE id=%s", (live,))
    db.execute("UPDATE desk_media_library SET expires_on=CURRENT_DATE-40 WHERE id=%s", (old,))
    assert [c['id'] for c in library.listing(db)] == [live]
    assert [r['id'] for _, r in library.catalog(db)] == [live]
    assert library.read(db, old) is None
    fb.push(db, [thread()])
    ready(db)
    with pytest.raises(ActionError):
        library.attach(db, wayne(db, staff), tid(), old)
