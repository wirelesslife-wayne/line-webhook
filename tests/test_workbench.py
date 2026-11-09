"""Real template in local Flask + FakeNotifier, isolated headless Chromium."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit,parse_qs
import pytest
from psycopg2.extras import Json
from werkzeug.serving import make_server,WSGIRequestHandler
from playwright.sync_api import sync_playwright,expect
from desk.auth import COOKIE
from test_desk import db,logged

expect.set_options(timeout=20000)


class Quiet(WSGIRequestHandler):
    def log_request(self,*args):pass


@pytest.fixture
def ui(db,logged):
    app,client,staff,notifier=logged
    db.set_meta('product_images',[{'name':'EX 安卓主機','url':'https://example.com/android.jpg','categories':[]}])
    db.execute("UPDATE desk_conversations SET draft=%s,drafted_upto=last_source_id,status='ready' WHERE user_id='desk-test-a'",
        (Json({'draft':'您好，RAV4 可安裝 EX 安卓主機。','extract':{'model':'RAV4','products':['安卓主機'],'series':'EX'}}),))
    server=make_server('127.0.0.1',0,app,request_handler=Quiet)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True, **(
            {'executable_path':os.environ['DESK_TEST_CHROMIUM']} if os.environ.get('DESK_TEST_CHROMIUM') else {}))
        context=browser.new_context(viewport={'width':1440,'height':1100})
        context.add_cookies([{'name':COOKIE,'value':client.get_cookie(COOKIE,path='/desk').value,'domain':'127.0.0.1','path':'/desk','httpOnly':True}])
        page=context.new_page();page.on('dialog',lambda dialog:dialog.accept())
        # Only local Flask and a synthetic image; no customer or paid API traffic.
        image=b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
          <rect width="320" height="200" fill="#e3edf4"/><rect x="40" y="25" width="240" height="150" rx="14" fill="#16222e"/>
          <rect x="55" y="40" width="210" height="110" rx="5" fill="#0f3d5c"/>
          <text x="160" y="102" text-anchor="middle" font-family="sans-serif" font-size="32" fill="white">EX</text>
          <text x="160" y="134" text-anchor="middle" font-family="sans-serif" font-size="14" fill="white">TEST IMAGE</text></svg>'''
        def network(route):
            if urlsplit(route.request.url).hostname=='127.0.0.1':route.continue_()
            elif route.request.url.startswith('https://example.com/'):
                route.fulfill(status=200,content_type='image/svg+xml',body=image)
            else:route.abort()
        page.route('**/*',network)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{server.server_port}/desk/')
        expect(page.locator('[data-uid="desk-test-a"]')).to_be_visible()
        try:yield page,db,notifier
        finally:
            context.close();browser.close();server.shutdown();thread.join(timeout=5)
        assert not errors


def open_a(page):
    page.locator('[data-uid="desk-test-a"]').click()
    expect(page.locator('#detail h2')).to_have_text('測試客人甲')


def held_conversations(page):
    held=[]
    def handle(route):
        uid=parse_qs(urlsplit(route.request.url).query).get('user_id',[''])[0]
        if uid=='desk-test-a':held.append(route)
        else:route.continue_()
    page.route('**/api/conversation?*',handle)
    return held


def release_and_wait(page,route):
    # networkidle alone may already be satisfied while a routed request is held.
    with page.expect_response(route.request.url) as response:
        route.continue_()
    assert response.value.status==200, response.value.text()
    response.value.finished()
    page.wait_for_load_state('networkidle')


def test_reverse_a_b_response(ui):
    page,_,_=ui;held=held_conversations(page)
    page.locator('[data-uid="desk-test-a"]').click()
    page.locator('[data-uid="desk-test-b"]').click()
    expect(page.locator('#detail h2')).to_have_text('測試客人乙')
    release_and_wait(page,held[0])
    expect(page.locator('#detail h2')).to_have_text('測試客人乙')


def test_switch_while_marking(ui):
    page,db,_=ui;open_a(page);held=[]
    page.route('**/api/action',lambda r:held.append(r))
    page.locator('#done').click()
    page.locator('[data-uid="desk-test-b"]').click()
    expect(page.locator('#detail h2')).to_have_text('測試客人乙')
    release_and_wait(page,held[0])
    expect(page.locator('#detail h2')).to_have_text('測試客人乙')
    assert db.conversation('desk-test-a')['status']=='done'


def test_confirm_snapshot_survives_earlier_response(ui):
    page,_,notifier=ui;held=held_conversations(page)
    page.locator('[data-uid="desk-test-a"]').click()
    page.locator('[data-uid="desk-test-b"]').click()
    expect(page.locator('#detail h2')).to_have_text('測試客人乙')
    page.locator('#draft').fill('確認乙的文字')
    page.locator('#open-confirm').click()
    expect(page.locator('#recipient')).to_have_text('送給 測試客人乙')
    release_and_wait(page,held[0])
    # Mutating the page after preview cannot mutate the captured send payload.
    page.evaluate("document.getElementById('draft').value='畫面後來改過的字'")
    page.locator('#send').click()
    expect(page.locator('#confirm')).not_to_be_visible()
    assert notifier.push.call_args.args[:2]==('desk-test-b','確認乙的文字')


def test_mobile_back_cancels_inflight_select(ui):
    page,_,_=ui;page.set_viewport_size({'width':390,'height':844});held=held_conversations(page)
    page.locator('[data-uid="desk-test-a"]').click()
    page.locator('#back').click();release_and_wait(page,held[0])
    expect(page.locator('#detail')).not_to_be_visible()
    expect(page.locator('[data-uid="desk-test-b"]')).to_be_visible()


@pytest.mark.parametrize('mobile',[False,True])
@pytest.mark.parametrize('action',['send','done'])
def test_mark_and_send_success_navigation(ui,mobile,action):
    page,_,_=ui
    if mobile:page.set_viewport_size({'width':390,'height':844})
    open_a(page)
    if action=='send':
        page.locator('#open-confirm').click();page.locator('#send').click()
    else:
        page.locator('#done').click()
    expect(page.locator('#feedback')).to_have_text('已送出並記錄。' if action=='send' else '已標記已在 LINE 後台回覆。')
    if mobile:expect(page.locator('#detail')).not_to_be_visible()
    else:expect(page.locator('#detail h2')).to_have_text('測試客人乙')


@pytest.mark.parametrize('mode',['failed','timeout','unknown'])
def test_failed_send_preserves_typed_content(ui,mode):
    page,_,notifier=ui;open_a(page)
    notifier.push.return_value={'ok':False,'status':400 if mode=='failed' else 0,'reason':'測試傳送失敗','unknown':mode!='failed'}
    page.locator('#draft').fill('員工自己打的字，不可清除')
    page.locator('#open-confirm').click();page.locator('#send').click()
    expect(page.locator('#action-feedback')).to_contain_text('測試傳送失敗')
    expect(page.locator('#draft')).to_have_value('員工自己打的字，不可清除')


def test_mark_conflict_preserves_editor_and_refreshes_messages(ui):
    page,db,_=ui;open_a(page);page.locator('#draft').fill('保留編輯')
    db.test_incoming['desk-test-a'].append({'id':102,'time_str':'2026-11-09 10:35:00','content':'客人的新問題','message_type':'text'})
    page.locator('#done').click()
    expect(page.locator('#action-feedback')).to_contain_text('請看完再標記')
    expect(page.locator('#draft')).to_have_value('保留編輯')
    expect(page.locator('.messages')).to_contain_text('客人的新問題')
    assert db.conversation('desk-test-a')['handled_upto']==0


def test_stale_draft_not_autofilled_and_detail_poll(ui):
    page,db,_=ui
    db.execute("UPDATE desk_conversations SET drafted_upto=100 WHERE user_id='desk-test-a'")
    open_a(page);expect(page.locator('#draft')).to_have_value('')
    expect(page.locator('#detail')).to_contain_text('草稿是舊的')
    db.test_incoming['desk-test-a'].append({'id':102,'time_str':'2026-11-09 10:35:00','content':'新問題','message_type':'text'})
    page.evaluate('poll()')
    expect(page.locator('#refresh-detail')).to_be_visible()


def test_list_poll_response_reversal(ui):
    page,_,_=ui;held=[]
    page.route('**/api/list?*',lambda r:held.append(r))
    page.locator('[data-tab="done"]').click()
    page.locator('[data-tab="skip"]').click()
    assert len(held)==2
    held[1].fulfill(json={'items':[],'has_more':False})
    held[0].fulfill(json={'items':[{'user_id':'wrong','name':'倒序舊回應','last':'2026-11-09','status':'done'}],'has_more':False})
    expect(page.locator('#list')).not_to_contain_text('倒序舊回應')
    expect(page.locator('[data-tab="skip"]')).to_have_attribute('aria-pressed','true')


def test_evidence_desktop_mobile_thumbnails_confirm(ui,tmp_path):
    page,_,_=ui
    folder=Path(os.environ.get('DESK_EVIDENCE_DIR',str(tmp_path)));folder.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(folder/'desktop_list.png'),full_page=True)
    open_a(page);page.locator('summary').click()
    expect(page.locator('.images img')).to_be_visible()
    expect(page.locator('#detail .images img')).to_have_js_property('naturalWidth',320)
    expect(page.locator('#detail .images')).to_contain_text('EX 安卓主機')
    page.screenshot(path=str(folder/'desktop_conversation_attachments.png'),full_page=True)
    page.locator('[name=image]').check();page.locator('#open-confirm').click()
    expect(page.locator('#preview-images img')).to_have_js_property('naturalWidth',320)
    page.screenshot(path=str(folder/'desktop_confirm.png'),full_page=True)
    page.locator('#cancel').click();page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(folder/'mobile_conversation_attachments.png'),full_page=True)
    page.locator('#back').click();page.screenshot(path=str(folder/'mobile_list.png'),full_page=True)
