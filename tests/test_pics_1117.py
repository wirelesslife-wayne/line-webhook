"""Wayne 11/17: LINE customers' photos as thumbnails (preview v2). Synthetic data, TEMP tables, rollback; nothing is sent."""
import base64
from io import BytesIO
from PIL import Image
from desk import fb, fb_pics
from test_desk import db, logged
from test_fb import thread

KEY = 'U' + '0' * 31 + '7'
PIC = '612345678901234567'


def jpeg(w=900, h=600, gps=False):
    out = BytesIO()
    im = Image.new('RGB', (w, h), (120, 80, 40))
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: 'N', 2: (25.0, 1.0, 1.0)}  # a GPS block, like a phone photo
    im.save(out, 'JPEG', exif=exif.tobytes())
    return out.getvalue()


def line_thread(extra=()):
    msgs = [dict(role='user', text='把手斷了', at='2026-11-17T07:00:00.000Z'),
            dict(role='user', text='[圖片]', at='2026-11-17T07:01:00.000Z', pic='-'.join([PIC[:6], PIC[6:12], PIC[12:]]))] + list(extra)
    return thread(channel='line_message', key=KEY, messages=msgs)


def test_photo_ids_are_kept_and_the_ai_still_reads_text(db):
    fb.push(db, [line_thread()])
    t = fb.thread_id('line_message', KEY)
    assert db.one("SELECT pic FROM desk_fb_events WHERE thread_id=%s AND content='[圖片]'", (t,))['pic'] == PIC
    assert all('pic' not in m for m in fb.messages(db, t, True))
    assert [m.get('pic') for m in fb.shape(db.all('SELECT * FROM desk_fb_events WHERE thread_id=%s ORDER BY id', (t,)))] == [None, PIC]
    # a run before 11/17 sent no id: the next read fills it in without a new snapshot
    db.execute('UPDATE desk_fb_events SET pic=NULL WHERE thread_id=%s', (t,))
    fb.push(db, [line_thread()])
    assert db.one("SELECT pic FROM desk_fb_events WHERE thread_id=%s AND content='[圖片]'", (t,))['pic'] == PIC
    # FB/IG and our own photos never carry one
    assert fb.pic_of(dict(role='page', pic=PIC)) is None and fb.pic_of(dict(role='user', pic='abc')) is None


def test_upload_needs_a_ticket_and_strips_location(db, logged):
    app, client, staff, _ = logged
    fb.push(db, [line_thread()])
    todo = fb_pics.todo(db)
    assert [x['key'] for x in todo] == [KEY] and todo[0]['pic'].replace('-', '') == PIC
    body = dict(key=KEY, pic=PIC, data=base64.b64encode(jpeg(gps=True)).decode())
    r = client.post('/desk/api/fb/pics/upload', data=__import__('json').dumps(dict(body, ticket='0' * 32)), content_type='text/plain')
    assert r.status_code == 403 and r.headers['Access-Control-Allow-Origin'] == 'https://chat.line.biz'
    assert client.open('/desk/api/fb/pics/upload', method='OPTIONS').status_code == 204
    ticket = fb_pics.ticket(db)
    r = client.post('/desk/api/fb/pics/upload', data=__import__('json').dumps(dict(body, ticket=ticket)), content_type='text/plain')
    assert r.get_json() == {'result': 'saved'}
    assert fb_pics.save(db, KEY, PIC, body['data']) == 'have' and fb_pics.todo(db) == []
    assert fb_pics.save(db, KEY, PIC + '1', base64.b64encode(b'not a photo').decode()) == 'not a photo'
    assert fb_pics.save(db, 'Uxyz', PIC, body['data']) == 'bad id'
    row = db.one('SELECT token,thumb,big FROM desk_fb_pics')
    for data in (bytes(row['thumb']), bytes(row['big'])):
        with Image.open(BytesIO(data)) as im:
            assert max(im.size) <= 1600 and not im.getexif().get(0x8825)
    assert max(Image.open(BytesIO(bytes(row['thumb']))).size) <= 400
    # staff see it; the list says which state each photo is in
    assert client.get('/desk/api/fb/pic/' + row['token']).mimetype == 'image/jpeg'
    assert client.get('/desk/api/fb/pic/' + row['token'] + '?size=full').status_code == 200
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    pics = [m for m in d['messages'] if m.get('pic_state')]
    assert [(m['pic_state'], m['pic']) for m in pics] == [('ok', row['token'])]
    # after 30 days: gone, and the file is deleted
    db.execute("UPDATE desk_fb_pics SET saved_at=NOW()-INTERVAL '31 days'")
    db.execute("UPDATE desk_fb_events SET event_at=NOW()-INTERVAL '31 days' WHERE pic IS NOT NULL")
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    assert [m['pic_state'] for m in d['messages'] if m.get('pic_state')] == ['gone']
    fb_pics.cleanup(db)
    assert db.one('SELECT COUNT(*) AS n FROM desk_fb_pics')['n'] == 0


def test_photos_need_a_login(db, logged):
    app, client, staff, _ = logged
    fb.push(db, [line_thread()])
    fb_pics.save(db, KEY, PIC, base64.b64encode(jpeg()).decode())
    token = db.one('SELECT token FROM desk_fb_pics')['token']
    stranger = app.test_client()
    assert stranger.get('/desk/api/fb/pic/' + token).status_code == 401
    assert client.get('/desk/api/fb/pic/' + 'f' * 32).status_code == 404
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    assert d['messages'][-1]['pic_state'] == 'ok'
    db.execute('DELETE FROM desk_fb_pics')
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    assert d['messages'][-1]['pic_state'] == 'missing'


VID = '612345678901234599'


def mp4(n=1000):
    return b'\x00\x00\x00\x18ftypmp42' + b'\x00' * n


def test_videos_cover_then_video_with_limits(db, logged, monkeypatch):
    """Wayne 11/17 (videos): cover + length first, then the MP4; over 20 MB only the cover; total kept under the cap."""
    import json
    from desk import fb_vids
    app, client, staff, _ = logged
    msgs = [dict(role='user', text='[影片]', at='2026-11-17T07:02:00.000Z', pic=VID)]
    fb.push(db, [line_thread(msgs)])
    assert [x['kind'] for x in fb_pics.todo(db) + fb_vids.todo(db)] == ['photo', 'video']
    cover = base64.b64encode(jpeg(320, 180)).decode()
    ticket = fb_pics.ticket(db)
    up = lambda **b: client.post('/desk/api/fb/pics/upload', data=json.dumps(dict(b, ticket=ticket, key=KEY)), content_type='text/plain').get_json()
    assert up(pic=VID, kind='video', data=cover, size=1000, dur=7805) == {'result': 'saved'}
    assert [x['pic'].replace('-', '') for x in fb_vids.todo(db)] == [VID]  # the video itself is still missing
    r = client.post('/desk/api/fb/pics/video', data=mp4(), headers={'X-Desk-Ticket': '0' * 32, 'X-Desk-Key': KEY, 'X-Desk-Pic': VID}, content_type='text/plain')
    assert r.status_code == 403
    hdr = {'X-Desk-Ticket': ticket, 'X-Desk-Key': KEY, 'X-Desk-Pic': VID}
    assert client.post('/desk/api/fb/pics/video', data=b'not a video', headers=hdr, content_type='text/plain').get_json() == {'result': 'not a video'}
    assert client.post('/desk/api/fb/pics/video', data=mp4(), headers=hdr, content_type='text/plain').get_json() == {'result': 'saved'}
    assert fb_vids.todo(db) == []
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    v = [m['vid'] for m in d['messages'] if m.get('vid')][0]
    assert v['state'] == 'ok' and v['dur'] == 7805 and 'pic' not in d['messages'][-1]
    assert client.get(f"/desk/api/fb/vid/{v['token']}?part=cover").mimetype == 'image/jpeg'
    r = client.get(f"/desk/api/fb/vid/{v['token']}", headers={'Range': 'bytes=0-9'})
    assert r.status_code == 206 and len(r.data) == 10 and r.mimetype == 'video/mp4'
    assert app.test_client().get(f"/desk/api/fb/vid/{v['token']}").status_code == 401
    # over 20 MB: only the cover, no ▶
    big = '612345678901234598'
    fb.push(db, [line_thread(msgs + [dict(role='user', text='[影片]', at='2026-11-17T07:03:00.000Z', pic=big)])])
    assert up(pic=big, kind='video', data=cover, size=25_000_000, dur=102000) == {'result': 'big'}
    # the cap: the oldest kept video is cleared to make room (cover stays)
    monkeypatch.setattr(fb_vids, 'TOTAL_LIMIT', 1500)
    third = '612345678901234597'
    fb.push(db, [line_thread(msgs + [dict(role='user', text='[影片]', at='2026-11-17T07:03:00.000Z', pic=big),
                                     dict(role='user', text='[影片]', at='2026-11-17T07:04:00.000Z', pic=third)])])
    assert up(pic=third, kind='video', data=cover, size=1000, dur=1000) == {'result': 'saved'}
    assert client.post('/desk/api/fb/pics/video', data=mp4(), headers=dict(hdr, **{'X-Desk-Pic': third}), content_type='text/plain').get_json() == {'result': 'saved'}
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    assert [m['vid']['state'] for m in d['messages'] if m.get('vid')] == ['cleared', 'big', 'ok']
    # 30 days: gone
    db.execute("UPDATE desk_fb_vids SET saved_at=NOW()-INTERVAL '31 days'")
    db.execute("UPDATE desk_fb_events SET event_at=NOW()-INTERVAL '31 days' WHERE content='[影片]'")
    d = client.get('/desk/api/fb/conversation?thread_id=' + fb.thread_id('line_message', KEY)).get_json()
    assert {m['vid']['state'] for m in d['messages'] if m.get('vid')} == {'gone'}
