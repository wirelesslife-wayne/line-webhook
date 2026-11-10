"""Media tests use temporary tables in a rolled-back transaction and FakeNotifier."""
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from PIL import Image
import pytest
from playwright.sync_api import expect
from psycopg2.extras import Json
from desk import media
from desk.notifier import message_payload
from desk.service import send, ActionError
from desk.worker import timeline
from test_desk import db, logged, send_data
from test_workbench import ui, open_a


def picture(fmt='JPEG', size=(320,200), color='red'):
    out=BytesIO();Image.new('RGB',size,color).save(out,fmt);return out.getvalue()


@pytest.fixture(scope='session')
def mp4(tmp_path_factory):
    path=tmp_path_factory.mktemp('desk-media')/'sample.mp4'
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.fail('ffmpeg is required to generate the synthetic MP4 fixture')
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',
        'color=c=red:s=320x200:d=0.5:r=10','-an','-c:v','libx264','-pix_fmt','yuv420p',
        '-movflags','+faststart',str(path)],check=True,capture_output=True)
    return path.read_bytes()


def post_upload(client,staff,*,uid='desk-test-a',data=None,mime='image/jpeg',preview=None):
    return client.post('/desk/api/media',headers={'X-CSRF-Token':staff['csrf']},data={
        'user_id':uid,'file':(BytesIO(picture() if data is None else data),'sample.'+('mp4' if mime=='video/mp4' else 'jpg'),mime),
        'preview':(BytesIO(picture(size=(240,150)) if preview is None else preview),'preview.jpg','image/jpeg')})


def uploaded(logged,**kwargs):
    _,client,staff,_=logged
    response=post_upload(client,staff,**kwargs)
    assert response.status_code==201,response.json
    return response.json['media']


def with_media(*items):
    return {**send_data(),'media':[i['id'] for i in items]}


@pytest.mark.parametrize('fmt,mime',[('JPEG','image/jpeg'),('PNG','image/png')])
def test_upload_and_public_read(db,logged,fmt,mime):
    app,client,staff,notifier=logged
    original=picture(fmt)
    item=uploaded(logged,data=original,mime=mime)
    assert media.TOKEN.fullmatch(item['id'])
    assert item['url']=='/desk/media/'+item['id']
    assert item['preview_url']!=item['url']
    row=db.one('SELECT * FROM desk_media WHERE id=%s',(item['id'],))
    assert row['uploaded_by']==staff['id'] and row['user_id']=='desk-test-a'
    assert row['size']==len(original) and row['content_type']==mime
    anon=app.test_client()
    response=anon.get(item['url'])
    assert response.status_code==200 and response.data==original
    assert response.mimetype==mime and int(response.headers['Content-Length'])==len(original)
    assert response.headers['X-Content-Type-Options']=='nosniff'
    assert anon.head(item['url']).headers['Content-Length']==str(len(original))
    preview=anon.get(item['preview_url'])
    assert preview.mimetype=='image/jpeg'
    assert max(Image.open(BytesIO(preview.data)).size)<=240
    assert anon.get('/desk/media/'+'0'*32).status_code==404
    assert anon.get('/desk/media/guess').status_code==404
    assert anon.get('/desk/api/conversation?user_id=desk-test-a').status_code==401
    assert anon.post('/desk/api/media').status_code==401
    assert anon.get('/desk/').status_code==302
    assert anon.post(item['url']).status_code!=200
    assert anon.get('/desk/media').status_code!=200
    notifier.push.assert_not_called()


def test_random_tokens_are_distinct(logged):
    first=uploaded(logged);second=uploaded(logged)
    assert len({first['id'],second['id'],first['preview_url'],second['preview_url']})==4


@pytest.mark.parametrize('data,mime',[(b'<svg/>','image/svg+xml'),(b'GIF89a','image/jpeg'),
    (b'garbage','video/mp4'),(b'','image/png'),(picture('PNG'),'image/jpeg')])
def test_invalid_file_content(logged,data,mime):
    _,client,staff,notifier=logged
    assert post_upload(client,staff,data=data,mime=mime).status_code==400
    notifier.push.assert_not_called()


@pytest.mark.parametrize('preview',[b'garbage',picture(size=(241,100))])
def test_preview_validation(logged,preview):
    _,client,staff,_=logged
    assert post_upload(client,staff,preview=preview).status_code==400


def test_limits_and_envelope(logged,monkeypatch):
    _,client,staff,_=logged
    assert media.IMAGE_LIMIT==10_000_000 and media.VIDEO_LIMIT==50_000_000
    monkeypatch.setattr(media,'IMAGE_LIMIT',10)
    assert '10MB' in post_upload(client,staff).json['error']
    monkeypatch.setattr(media,'VIDEO_LIMIT',20)
    assert 'YouTube' in post_upload(client,staff,mime='video/mp4',data=b'x'*21).json['error']
    response=client.post('/desk/api/media',data=b'x'*(media.VIDEO_LIMIT+media.PREVIEW_LIMIT+32769),
        content_type='application/octet-stream',headers={'X-CSRF-Token':staff['csrf']})
    assert response.status_code==413


def test_upload_requires_header_csrf(logged):
    _,client,staff,_=logged
    for token in (None,'bad'):
        response=client.post('/desk/api/media',data={'user_id':'desk-test-a'},
            headers={'X-CSRF-Token':token} if token else {})
        assert response.status_code==403
    assert post_upload(client,staff,uid='missing').status_code==404


def test_four_file_limit_and_remove(db,logged):
    _,client,staff,_=logged
    items=[uploaded(logged) for _ in range(4)]
    assert post_upload(client,staff).status_code==400
    item=items[0]
    path='/desk/api/media/'+item['id']+'/remove'
    assert client.post(path,json={'user_id':'desk-test-a'}).status_code==403
    assert client.post(path,json={'user_id':'desk-test-b'},headers={'X-CSRF-Token':staff['csrf']}).status_code==409
    assert client.post(path,json={'user_id':'desk-test-a'},headers={'X-CSRF-Token':staff['csrf']}).status_code==200
    assert client.get(item['url']).status_code==410
    assert db.one('SELECT filename,data FROM desk_media WHERE id=%s',(item['id'],))['data'] is None
    uploaded(logged)


def test_video_preview_and_range(db,logged,mp4):
    app,_,_,_=logged
    item=uploaded(logged,data=mp4,mime='video/mp4')
    assert item['type']=='video'
    assert media.mp4_valid(mp4)
    anon=app.test_client()
    full=anon.get(item['url']);assert full.mimetype=='video/mp4' and full.data==mp4
    part=anon.get(item['url'],headers={'Range':'bytes=0-15'})
    assert part.status_code==206 and part.data==mp4[:16]
    assert part.headers['Content-Length']=='16'
    assert anon.get(item['preview_url']).mimetype=='image/jpeg'


def test_send_order_urls_and_timeline(db,logged,monkeypatch,mp4):
    _,_,staff,notifier=logged
    monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    image=uploaded(logged);video=uploaded(logged,data=mp4,mime='video/mp4')
    data=with_media(image,video)
    assert send(db,notifier,staff,data)['ok']
    args=notifier.push.call_args.args
    messages=message_payload(args[1],args[2])
    assert [m['type'] for m in messages]==['text','image','video']
    assert messages[1]['originalContentUrl']=='https://desk.example.test'+image['url']
    assert messages[2]['originalContentUrl']=='https://desk.example.test'+video['url']
    assert messages[2]['previewImageUrl']=='https://desk.example.test'+video['preview_url']
    assert args[3]==data['retry_key']
    assert db.one('SELECT COUNT(*) AS n FROM desk_media WHERE sent_at IS NOT NULL')['n']==2
    sent=timeline(db,db.conversation('desk-test-a'))[-1]
    assert sent['images']==args[2]
    ai_sent=timeline(db,db.conversation('desk-test-a'),for_ai=True)[-1]
    assert ai_sent['text'].endswith(' [image] [video]')
    assert 'images' not in ai_sent and 'desk/media/' not in ai_sent['text']
    assert send(db,notifier,staff,data)['already_sent']
    notifier.push.assert_called_once()


@pytest.mark.parametrize('base',['','http://desk.test','https://user:pw@desk.test','https://desk.test/path'])
def test_send_requires_trusted_https_base(db,logged,monkeypatch,base):
    _,_,staff,notifier=logged;item=uploaded(logged)
    monkeypatch.setenv('DESK_MEDIA_BASE_URL',base)
    with pytest.raises(ActionError,match='HTTPS'):send(db,notifier,staff,with_media(item))
    notifier.push.assert_not_called()


@pytest.mark.parametrize('mode',['other_user','other_staff','removed','expired','external','duplicate','too_many'])
def test_cross_conversation_and_tampering_rejected(db,logged,monkeypatch,mode):
    _,_,staff,notifier=logged;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    item=uploaded(logged);data=with_media(item)
    if mode=='other_user':data.update(user_id='desk-test-b',source_upto=202)
    if mode=='other_staff':staff={**staff,'id':staff['id']+1}
    if mode=='removed':db.execute('UPDATE desk_media SET removed_at=NOW()')
    if mode=='expired':db.execute("UPDATE desk_media SET uploaded_at=NOW()-INTERVAL '30 days'")
    if mode=='external':data.update(media=[],images=['https://example.com/a.jpg'])
    if mode=='duplicate':data['media']*=2
    if mode=='too_many':data['media']=[uuid.uuid4().hex for _ in range(5)]
    with pytest.raises(ActionError):send(db,notifier,staff,data)
    notifier.push.assert_not_called()


def test_unknown_retry_keeps_snapshot_and_protects_media(db,logged,monkeypatch):
    _,client,staff,notifier=logged;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    item=uploaded(logged);data=with_media(item)
    notifier.push.return_value={'ok':False,'status':0,'unknown':True,'reason':'待查核'}
    with pytest.raises(ActionError):send(db,notifier,staff,data)
    snapshot=notifier.push.call_args.args
    assert client.post('/desk/api/media/'+item['id']+'/remove',json={'user_id':'desk-test-a'},
        headers={'X-CSRF-Token':staff['csrf']}).status_code==409
    db.execute("UPDATE desk_send_attempts SET updated_at=NOW()-INTERVAL '11 seconds'")
    with pytest.raises(ActionError,match='不同內容'):send(db,notifier,staff,{**data,'media':[]})
    monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://changed.example.test')
    notifier.push.return_value={'ok':True,'status':200,'reason':''}
    assert send(db,notifier,staff,data)['ok']
    assert notifier.push.call_args.args==snapshot


def test_cleanup_retains_audit_and_expires_public_read(db,logged):
    app,_,_,_=logged;item=uploaded(logged)
    db.execute("UPDATE desk_media SET uploaded_at=NOW()-INTERVAL '30 days'")
    assert app.test_client().get(item['url']).status_code==410
    media.cleanup(db)
    row=db.one('SELECT * FROM desk_media WHERE id=%s',(item['id'],))
    assert row['data'] is None and row['preview_data'] is None and row['cleared_at']
    assert row['filename'] and row['size']>0


def test_capacity_evicts_oldest_sent_and_preserves_unsent(db,logged,monkeypatch):
    _,_,staff,notifier=logged;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    first=uploaded(logged);second=uploaded(logged);third=uploaded(logged)
    send(db,notifier,staff,with_media(first,second))
    db.execute("UPDATE desk_media SET sent_at=NOW()-INTERVAL '1 day' WHERE id=%s",(first['id'],))
    unit=db.one('SELECT size+preview_size AS size FROM desk_media LIMIT 1')['size']
    monkeypatch.setattr(media,'CAPACITY',unit*3)
    fourth=uploaded(logged,uid='desk-test-b')
    assert db.one('SELECT data FROM desk_media WHERE id=%s',(first['id'],))['data'] is None
    for item in (second,third,fourth):
        assert db.one('SELECT data FROM desk_media WHERE id=%s',(item['id'],))['data'] is not None
    monkeypatch.setattr(media,'CAPACITY',unit)
    _,client,staff,_=logged
    assert post_upload(client,staff,uid='desk-test-b').status_code==507
    assert db.one('SELECT data FROM desk_media WHERE id=%s',(third['id'],))['data'] is not None


def test_suggestions_removed_and_uploaded_only(db,logged):
    _,client,_,_=logged
    html=client.get('/desk/').get_data(as_text=True)
    assert '可附的商品圖片' not in html and 'name="image"' not in html
    assert 'accept="image/jpeg,image/png,video/mp4" multiple' in html
    response=client.get('/desk/api/conversation?user_id=desk-test-a').json
    assert 'images' not in response and response['media']==[]
    item=uploaded(logged)
    assert client.get('/desk/api/conversation?user_id=desk-test-a').json['media']==[item]


def set_upload(page,files):
    page.locator('#media-input').set_input_files(files)
    expect(page.locator('#upload-status')).to_contain_text('已加入')
    expect(page.locator('#open-confirm')).to_be_enabled()


def test_browser_upload_video_confirm_mobile_and_remove(ui,mp4,monkeypatch):
    page,db,notifier=ui;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    folder=Path(os.environ['DESK_EVIDENCE_DIR']);folder.mkdir(parents=True,exist_ok=True)
    open_a(page)
    assert page.locator('summary').count()==0
    set_upload(page,[{'name':'員工選的圖片.jpg','mimeType':'image/jpeg','buffer':picture()},
                     {'name':'員工選的影片.mp4','mimeType':'video/mp4','buffer':mp4}])
    assert page.locator('#uploads img').count()==2,page.locator('#action-feedback').inner_text()
    expect(page.locator('#uploads img').first).to_have_js_property('naturalWidth',240)
    expect(page.locator('#uploads img').last).to_have_js_property('naturalWidth',240)
    # Browser-generated video thumbnail is a JPEG of the first (red) frame.
    row=db.one("SELECT preview_data FROM desk_media WHERE content_type='video/mp4'")
    pixel=Image.open(BytesIO(bytes(row['preview_data']))).getpixel((50,50))
    assert pixel[0]>200 and pixel[1]<40 and pixel[2]<40
    page.screenshot(path=str(folder/'desktop_uploads.png'),full_page=True)
    page.locator('#open-confirm').click()
    expect(page.locator('#preview-images img')).to_have_count(2)
    expect(page.locator('#preview-images img').last).to_have_js_property('naturalWidth',240)
    expect(page.locator('#image-count')).to_contain_text('2 個圖片／影片')
    page.screenshot(path=str(folder/'desktop_confirm_media.png'),full_page=True)
    page.locator('#cancel').click()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(folder/'mobile_uploads.png'),full_page=True)
    page.locator('#open-confirm').click()
    expect(page.locator('#preview-images img').last).to_have_js_property('naturalWidth',240)
    page.screenshot(path=str(folder/'mobile_confirm_media.png'))
    page.locator('#cancel').click()
    page.locator('[data-remove-media="0"]').click()
    expect(page.locator('#uploads img')).to_have_count(1)
    page.locator('#open-confirm').click();page.locator('#send').click()
    expect(page.locator('#feedback')).to_contain_text('已送出並記錄')
    assert notifier.push.call_args.args[2][0]['type']=='video'
    assert len(notifier.push.call_args.args[2])==1
    page.locator('[data-tab="done"]').click()
    page.locator('[data-uid="desk-test-a"]').click()
    expect(page.locator('.message.ours .images a')).to_have_count(1)
    expect(page.locator('.message.ours')).to_contain_text('影片 · 員工選的影片.mp4')


def test_browser_confirmation_freezes_media(ui,monkeypatch):
    page,db,notifier=ui;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    open_a(page)
    set_upload(page,{'name':'snapshot.jpg','mimeType':'image/jpeg','buffer':picture()})
    token=db.one('SELECT id FROM desk_media')['id']
    page.locator('#open-confirm').click()
    page.evaluate("selectedMedia=[];document.getElementById('draft').value='later edit'")
    page.locator('#send').click()
    expect(page.locator('#feedback')).to_contain_text('已送出並記錄')
    assert [i['id'] for i in notifier.push.call_args.args[2]]==[token]
    assert notifier.push.call_args.args[1]!='later edit'


def test_browser_network_failure_preserves_retry_key(ui,monkeypatch):
    page,db,notifier=ui;monkeypatch.setenv('DESK_MEDIA_BASE_URL','https://desk.example.test')
    open_a(page)
    set_upload(page,{'name':'retry.jpg','mimeType':'image/jpeg','buffer':picture()})
    requests=[]
    def fail_once(route):
        requests.append(route.request.post_data_json)
        if len(requests)==1:route.abort()
        else:route.continue_()
    page.route('**/desk/api/send',fail_once)
    page.locator('#open-confirm').click();page.locator('#send').click()
    expect(page.locator('#action-feedback')).to_be_visible()
    expect(page.locator('#media-input')).to_be_disabled()
    expect(page.locator('#draft')).to_have_attribute('readonly','')
    page.locator('#open-confirm').click();page.locator('#send').click()
    expect(page.locator('#feedback')).to_contain_text('已送出並記錄')
    assert requests[0]==requests[1]
    notifier.push.assert_called_once()


def test_browser_rejects_limits_and_compresses_image(ui,tmp_path):
    page,_,notifier=ui;open_a(page)
    page.locator('#draft').fill('')
    page.locator('#open-confirm').click()
    expect(page.locator('#action-feedback')).to_contain_text('請先填寫回覆內容')
    page.locator('#media-input').set_input_files([{'name':str(i)+'.jpg','mimeType':'image/jpeg','buffer':picture()} for i in range(5)])
    expect(page.locator('#action-feedback')).to_contain_text('最多只能加入 4')
    huge=tmp_path/'huge.mp4'
    with huge.open('wb') as file:
        file.truncate(50*1024*1024+1)
    page.locator('#media-input').set_input_files(str(huge))
    expect(page.locator('#action-feedback')).to_contain_text('影片太大，請改傳 YouTube 連結')
    # A valid >10MB JPEG (trailing padding) exercises real browser resize/reencode.
    large=picture(size=(3000,2100))+b'\0'*(10*1024*1024)
    set_upload(page,{'name':'large.jpg','mimeType':'image/jpeg','buffer':large})
    expect(page.locator('#uploads')).to_contain_text('large.jpg')
    row=ui[1].one('SELECT data,size FROM desk_media')
    assert row['size']<=10_000_000
    assert max(Image.open(BytesIO(bytes(row['data']))).size)<=2048
    notifier.push.assert_not_called()
