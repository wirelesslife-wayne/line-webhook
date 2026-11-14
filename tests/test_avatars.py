"""The customer's picture next to the name (Wayne 11/14). Synthetic pictures, TEMP tables, rollback."""
from io import BytesIO
from unittest.mock import Mock
from PIL import Image
from desk import fb, fb_avatars
from desk.tools import fb_browser
from test_desk import db, logged
from test_fb import thread, tid


def picture(fmt='PNG', size=(300, 200)):
    out = BytesIO()
    Image.new('RGB', size, (200, 120, 60)).save(out, fmt)
    return out.getvalue()


def test_pictures_are_shrunk_to_a_small_square_jpeg_and_junk_is_ignored(db):
    fb.push(db, [thread()])
    assert fb_avatars.save(db, tid(), picture())
    small = bytes(fb_avatars.read(db, tid())['data'])
    with Image.open(BytesIO(small)) as im:
        assert im.format == 'JPEG' and im.size == (96, 96)
    assert not fb_avatars.save(db, tid(), b'<html>not a picture</html>')
    assert not fb_avatars.save(db, 'fb:unknown', picture())


def test_the_browser_run_hands_over_an_address_or_a_saved_file(db, tmp_path):
    fb.push(db, [thread(), thread('ig_message', key='900002')])
    shot = tmp_path / 'face.png'
    shot.write_bytes(picture())
    get = Mock(return_value=Mock(status_code=200, content=picture('JPEG')))
    rows = fb.validate([dict(thread(), avatar='https://profile.line-scdn.net/0habc/preview'),
                        dict(thread('ig_message', key='900002'), avatar=str(shot))])
    assert fb_avatars.collect(db, rows, get) == 2 and get.call_count == 1
    assert fb_avatars.collect(db, rows[:1], get) == 0 and get.call_count == 1  # downloaded once a week, not every hour
    other = fb.validate([dict(thread(key='900003'), avatar='https://evil.example/x.jpg')])
    fb.push(db, [thread(key='900003')])
    assert fb_avatars.collect(db, other, get) == 0 and get.call_count == 1  # only the platforms' picture servers


def test_the_list_says_which_conversations_have_a_picture_and_serves_it(db, logged):
    _, client, _, _ = logged
    fb.push(db, [thread(), thread('fb_comment', key='900002#x')])
    fb_avatars.save(db, tid(), picture())
    items = {i['id']: i for i in client.get('/desk/api/fb/list').get_json()['items']}
    assert items[tid()]['avatar'] and items[fb.thread_id('fb_comment', '900002#x')]['avatar'] is None
    r = client.get('/desk/api/fb/avatar?thread_id=' + tid())
    assert r.status_code == 200 and r.mimetype == 'image/jpeg'
    assert client.get('/desk/api/fb/avatar?thread_id=nope').status_code == 404


def test_line_picture_id_with_separators_and_the_list_of_faces_already_kept(db):
    fb.push(db, [dict(thread('line_message', key='U1234'), url='')])
    get = Mock(return_value=Mock(status_code=200, content=picture('JPEG')))
    rows = fb.validate([dict(thread('line_message', key='U1234'), url='', avatar_line='0hAB~cd-E~fgh_IJ~klmn')])
    assert fb_avatars.collect(db, rows, get) == 1
    assert get.call_args.args[0] == 'https://profile.line-scdn.net/0hABcd-Efgh_IJklmn/preview'
    assert fb_avatars.have(db, line=True) == ['U1234'] and fb_avatars.have(db) == []
