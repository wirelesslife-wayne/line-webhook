"""Home-screen icon (Wayne 11/13): the phone fetches it without the login cookie; nothing else opens up."""
from test_desk import db, logged


def test_icon_and_manifest_load_without_login_but_pages_still_need_it(db, logged):
    app, client, _, _ = logged
    anon = app.test_client()
    for size in (180, 192, 512):
        r = anon.get(f'/desk/app-icon-{size}.png')
        assert r.status_code == 200 and r.mimetype == 'image/png' and r.data[:8] == b'\x89PNG\r\n\x1a\n'
    assert anon.get('/desk/app-icon-64.png').status_code == 404
    m = anon.get('/desk/manifest.webmanifest')
    assert m.status_code == 200 and m.headers['Content-Type'] == 'application/manifest+json'
    assert m.get_json()['short_name'] == '無限客服' and m.get_json()['start_url'] == '/desk/fb'
    assert anon.get('/desk/fb').status_code in (302, 401) and anon.get('/desk/api/fb/list').status_code in (302, 401)
    page = client.get('/desk/fb').get_data(as_text=True)
    assert 'apple-touch-icon' in page and '/desk/manifest.webmanifest' in page
