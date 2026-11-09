"""Pure tests + rollback-only Postgres tests; no real AI or LINE calls."""
import ast
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock
import uuid
import pytest
import requests
from flask import Flask
from psycopg2.extras import RealDictCursor,Json
from werkzeug.security import generate_password_hash
from desk.auth import Auth,digest,COOKIE
from desk.db import Store,now,SCHEMA
from desk.engine import Engine,ProviderPending,check,is_builtin,image_url
from desk.notifier import LineNotifier,delivery_result
from desk.routes import make_blueprint
from desk.service import send,ActionError
from desk.tools.upload_qa import redactor
from desk.worker import utc_time,local_time,due,builtin_only,draft_once,refresh_prices,timeline,validate_prices


def test_timezone_rollover():
    assert local_time('2026-11-09 18:30:00')=='2026-11-10 02:30:00'
    assert local_time('2026-11-09T18:30:00+00:00')=='2026-11-10 02:30:00'


def test_quiet_period():
    t=now()
    assert not due({'status':'waiting','last_at':t-timedelta(seconds=59)},t)
    assert due({'status':'waiting','last_at':t-timedelta(seconds=60)},t)
    assert not due({'status':'ready','last_at':t-timedelta(minutes=4)},t)


def test_builtin_mixed_group():
    assert is_builtin('呼叫客服') and is_builtin('[sticker]')
    assert builtin_only([{'content':'呼叫客服'},{'message_type':'sticker'}])
    assert not builtin_only([{'content':'呼叫客服'},{'content':'要買主機'}])


@pytest.mark.parametrize('draft,expected',[
    ('總共 18,000 元',[]),('三項合計 21,000 元',[]),
    ('總共 77,777 元',['金額 77,777 在資料裡找不到，請確認']),
])
def test_amount_sums(draft,expected):
    assert check(draft,'主機 $10,000；環景 $8,000；線組 $3,000')==expected


def test_banned_and_unknown_amount():
    assert len(check('預約成功，9,999 元','價格 $8,000'))==2


@pytest.mark.parametrize('url,allowed',[
    ('https://example.com/item.JPG?size=2',True),('https://example.com/a.png',True),
    ('http://example.com/a.jpg',False),('javascript:alert(1)',False),
    ('https://user:pass@example.com/a.jpg',False),('https://example.com/a.mp4',False)])
def test_image_urls(url,allowed):
    assert image_url(url)==allowed


def test_redaction():
    scrub=redactor(['王小明'])
    text=scrub('王小明，電話 0912-345-678，車牌 ABC-1234，Email a@foo.tw，姓名：陳小華')
    for value in ['王小明','0912-345-678','ABC-1234','a@foo.tw','陳小華']:
        assert value not in text


def test_notifier_disabled(monkeypatch):
    monkeypatch.delenv('DESK_SEND_ENABLED',raising=False)
    post=Mock(side_effect=AssertionError('network forbidden'))
    monkeypatch.setattr(requests,'post',post)
    with pytest.raises(RuntimeError,match='FakeNotifier'):
        LineNotifier().push('test','hello',[],str(uuid.uuid4()))
    post.assert_not_called()


@pytest.mark.parametrize('code,accepted',[(200,True),(400,False),(401,False),(429,False),(500,False),(409,True)])
def test_notifier_mock(monkeypatch,code,accepted):
    result=delivery_result(code,'test' if code==409 else None)
    assert result['ok']==accepted
    assert result['unknown']==(code>=500)


def test_notifier_timeout(monkeypatch):
    monkeypatch.delitem(sys.modules,'pytest')
    monkeypatch.setenv('DESK_SEND_ENABLED','1');monkeypatch.setenv('LINE_CHANNEL_ACCESS_TOKEN','test')
    monkeypatch.setattr(requests,'post',Mock(side_effect=requests.Timeout()))
    with pytest.raises(RuntimeError,match='FakeNotifier'):
        LineNotifier().push('synthetic-user','hello',[],str(uuid.uuid4()))


@pytest.fixture
def db(monkeypatch):
    url=os.environ.get('DESK_TEST_DATABASE_URL')
    if not url:
        pytest.skip('set DESK_TEST_DATABASE_URL for rollback-only integration tests')
    base=Store(url)
    conn=__import__('psycopg2').connect(url)
    # Temporary tables shadow every production table. Even test failures cannot
    # change a real customer's state; all test DDL/data is rolled back too.
    with conn.cursor() as cur:
        cur.execute(SCHEMA.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE').replace('CREATE INDEX IF NOT EXISTS','CREATE INDEX'))
        cur.execute('INSERT INTO desk_kb SELECT * FROM public.desk_kb')
        cur.execute('CREATE TEMP TABLE messages AS SELECT * FROM public.messages WITH NO DATA')
    class RollbackStore(Store):
        @contextmanager
        def cursor(self):
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SAVEPOINT desk_test_cursor')
                try:
                    yield cur
                except Exception:
                    cur.execute('ROLLBACK TO SAVEPOINT desk_test_cursor')
                    raise
                finally:
                    cur.execute('RELEASE SAVEPOINT desk_test_cursor')
        @contextmanager
        def lock(self,name):
            yield True
        def latest_source_cursor(self,cur,uid):
            return self.latest_source(uid)
    result=RollbackStore(url)
    result.test_connection=conn
    result.execute("INSERT INTO desk_users(username,password_hash) VALUES('desk-test-user',%s)",(generate_password_hash('test-password'),))
    result.execute('''INSERT INTO desk_conversations(user_id,display_name,last_source_id,last_at,first_at)
      VALUES('desk-test-a','測試客人甲',101,NOW()-INTERVAL '2 minutes',NOW()-INTERVAL '40 minutes'),
            ('desk-test-b','測試客人乙',202,NOW()-INTERVAL '20 seconds',NOW()-INTERVAL '20 seconds')''')
    incoming={'desk-test-a':[{'id':101,'time_str':'2026-11-09 10:30:00','content':'主機報價？','message_type':'text'}],
              'desk-test-b':[{'id':202,'time_str':'2026-11-09 10:31:00','content':'呼叫客服','message_type':'text'}]}
    result.incoming=lambda uid,after=0:[r for r in incoming.get(uid,[]) if r['id']>after]
    result.latest_source=lambda uid:max((r['id'] for r in incoming.get(uid,[])),default=0)
    result.test_incoming=incoming
    monkeypatch.setenv('DESK_SECRET_KEY','test-session-secret-only')
    try:
        yield result
    finally:
        conn.rollback();conn.close()


@pytest.fixture
def logged(db):
    app=Flask('test-desk');app.config['TESTING']=True
    notifier=Mock()
    notifier.push.return_value={'ok':True,'status':200,'reason':''}
    app.register_blueprint(make_blueprint(db,notifier),url_prefix='/desk')
    client=app.test_client()
    cookie=Auth(db,os.environ['DESK_SECRET_KEY']).login('desk-test-user','test-password')
    client.set_cookie(COOKIE,cookie,domain='localhost',path='/desk')
    staff=Auth(db,os.environ['DESK_SECRET_KEY']).session(cookie)
    return app,client,staff,notifier


def send_data(uid='desk-test-a',upto=101):
    return {'user_id':uid,'text':'您好，請問車款與年份？','images':[],'source_upto':upto,'retry_key':str(uuid.uuid4()),'confirmed':True}


def test_session_and_login_lock(db):
    auth=Auth(db,os.environ['DESK_SECRET_KEY'])
    for _ in range(5):
        assert auth.login('desk-test-user','wrong') is None
    assert auth.login('desk-test-user','test-password') is None
    db.execute('UPDATE desk_login_attempts SET locked_until=NOW()-INTERVAL \'1 second\' WHERE bucket=%s',(digest('desk-test-user'),))
    cookie=auth.login('desk-test-user','test-password')
    assert auth.session(cookie)['username']=='desk-test-user'
    assert auth.session(cookie+'x') is None
    db.execute('UPDATE desk_sessions SET expires_at=NOW()-INTERVAL \'1 second\' WHERE sid=%s',(auth.session(cookie)['sid'],))
    assert auth.session(cookie) is None


def test_anonymous_csrf_cookie_flags(db,logged):
    app,client,staff,notifier=logged
    anon=app.test_client()
    assert anon.get('/desk/').status_code==302
    for path in ['/desk/api/list','/desk/api/conversation?user_id=desk-test-a']:
        assert anon.get(path).status_code==401
    assert anon.post('/desk/api/send',json=send_data()).status_code==401
    assert client.post('/desk/api/send',json=send_data()).status_code==403
    assert anon.post('/desk/login',data={'username':'x','password':'x'}).status_code==403
    auth=Auth(db,os.environ['DESK_SECRET_KEY']);token=auth.preauth()
    anon.set_cookie('desk_login',token,path='/desk')
    response=anon.post('/desk/login',data={'username':'desk-test-user','password':'test-password','csrf':token})
    cookie=next(h for h in response.headers.getlist('Set-Cookie') if h.startswith(COOKIE+'='))
    assert all(flag in cookie for flag in ('Secure','HttpOnly','SameSite=Lax','Max-Age=43200','Path=/desk'))
    assert client.post('/desk/logout',json={},headers={'X-CSRF-Token':staff['csrf']}).status_code==302
    assert client.get('/desk/api/list').status_code==401
    notifier.push.assert_not_called()


def test_list_grouping_order_and_detail(logged):
    _,client,_,_=logged
    data=client.get('/desk/api/list').get_json()
    assert [r['user_id'] for r in data['items']][:2]==['desk-test-a','desk-test-b']
    assert data['items'][0]['minutes']>=40
    detail=client.get('/desk/api/conversation?user_id=desk-test-a').get_json()
    assert detail['messages'][0]['time']=='2026-11-09 18:30:00'


def test_mock_send_success_and_duplicate(db,logged,monkeypatch):
    _,client,staff,notifier=logged
    payload=send_data();headers={'X-CSRF-Token':staff['csrf']}
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==200
    assert db.conversation('desk-test-a')['status']=='done'
    assert db.one("SELECT COUNT(*) AS n FROM desk_outgoing WHERE user_id='desk-test-a'")['n']==1
    sent_at=db.one('SELECT updated_at FROM desk_send_attempts WHERE retry_key=%s',(payload['retry_key'],))['updated_at']
    monkeypatch.setattr('desk.service.now',lambda:sent_at+timedelta(seconds=1))
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==200
    notifier.push.assert_called_once()
    payload['retry_key']=str(uuid.uuid4())
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==429
    assert timeline(db,db.conversation('desk-test-a'))[-1]['role']=='assistant'


def test_mock_send_failure_retains_draft(db,logged):
    _,client,staff,notifier=logged
    db.execute("UPDATE desk_conversations SET draft=%s WHERE user_id='desk-test-a'",(Json({'draft':'原草稿'}),))
    notifier.push.return_value={'ok':False,'status':400,'reason':'格式錯誤','unknown':False}
    assert client.post('/desk/api/send',json=send_data(),headers={'X-CSRF-Token':staff['csrf']}).status_code==502
    assert db.conversation('desk-test-a')['draft']['draft']=='原草稿'
    assert db.conversation('desk-test-a')['status']=='waiting'
    assert db.one("SELECT COUNT(*) AS n FROM desk_outgoing WHERE user_id='desk-test-a'")['n']==0


def test_ambiguous_retry_reuses_same_key(db,logged):
    _,client,staff,notifier=logged
    notifier.push.return_value={'ok':False,'status':0,'reason':'結果待確認','unknown':True}
    payload=send_data();headers={'X-CSRF-Token':staff['csrf']}
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==502
    assert client.get('/desk/api/conversation?user_id=desk-test-a').get_json()['pending']['retry_key']==payload['retry_key']
    replacement={**payload,'retry_key':str(uuid.uuid4())}
    assert client.post('/desk/api/send',json=replacement,headers=headers).status_code==409
    db.execute("UPDATE desk_send_attempts SET updated_at=NOW()-INTERVAL '11 seconds'")
    notifier.push.return_value={'ok':True,'status':409,'reason':''}
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==200
    assert notifier.push.call_args.args[-1]==payload['retry_key']
    assert db.one("SELECT COUNT(*) AS n FROM desk_outgoing WHERE user_id='desk-test-a'")['n']==1


def test_stale_source_and_manual_done(db,logged):
    _,client,staff,notifier=logged
    payload=send_data();headers={'X-CSRF-Token':staff['csrf']}
    db.test_incoming['desk-test-a'].append({'id':102,'time_str':'2026-11-09 10:31:00','content':'還有問題','message_type':'text'})
    assert client.post('/desk/api/send',json=payload,headers=headers).status_code==409
    db.test_incoming['desk-test-a'].pop()
    assert client.post('/desk/api/action',json={'action':'done','user_id':'desk-test-a','source_upto':101},headers=headers).status_code==200
    notifier.push.assert_not_called()
    assert db.conversation('desk-test-a')['status']=='done'


def test_draft_regenerates_if_new_message_arrives(db):
    class Slow:
        def __init__(self,store):pass
        def draft(self,*args):
            db.test_incoming['desk-test-a'].append({'id':102,'time_str':'2026-11-09 11:00:00','content':'更新','message_type':'text'})
            return {'draft':'舊草稿'}
    draft_once(db,Slow)
    assert db.conversation('desk-test-a')['status']=='waiting'
    assert db.conversation('desk-test-a')['draft'] is None


def test_draft_builtin_and_pending_provider(db):
    db.execute("UPDATE desk_conversations SET last_at=NOW()-INTERVAL '61 seconds' WHERE user_id='desk-test-b'")
    draft_once(db)
    assert db.conversation('desk-test-a')['status']=='blocked'
    assert db.conversation('desk-test-b')['status']=='skip'


def test_engine_includes_our_reply(db):
    captured=[]
    def provider(messages):
        captured.append(messages)
        return {'brand':None,'model':None,'products':[]} if len(captured)==1 else {'draft':'您好，請問年份？'}
    Engine(db,provider).draft([{'time':'2026-11-09 18:30:00','role':'user','text':'詢價','new':True},
      {'time':'2026-11-09 18:31:00','role':'assistant','text':'我方先前確認內容','new':False}],now())
    assert '我方先前確認內容' in captured[1][1]['content']


def test_price_failure_keeps_last_good(db):
    before=db.kb()['wlf-data.json']
    db.execute("DELETE FROM desk_meta WHERE key='price_attempt'")
    fetch=Mock(side_effect=requests.Timeout())
    refresh_prices(db,fetch)
    assert db.kb()['wlf-data.json']==before and db.meta('price_error') is True
    refresh_prices(db,fetch)
    fetch.assert_called_once()


def test_price_real_schema_accepts_empty_plan(db):
    validate_prices(db.kb()['wlf-data.json'])
    with pytest.raises(ValueError):
        validate_prices({'cars':[{}],'plans':[{}]})


def test_ingest_source_read_only_and_cursor(db):
    last=db.one('SELECT COALESCE(MAX(id),0) AS id FROM messages')['id']
    db.set_meta('source_cursor',max(0,last-10))
    expected=db.one('SELECT COUNT(*) AS n FROM messages WHERE oa IS NULL AND id>%s',(max(0,last-10),))['n']
    before=db.one('SELECT COUNT(*) AS n,MAX(id) AS id FROM messages')
    assert db.ingest()==expected
    assert db.ingest()==0
    after=db.one('SELECT COUNT(*) AS n,MAX(id) AS id FROM messages')
    assert before==after


def test_request_size_limit(logged):
    _,client,staff,_=logged
    assert client.post('/desk/api/send',data='x'*40000,content_type='application/json',headers={'X-CSRF-Token':staff['csrf']}).status_code==413


def test_real_pg_lock():
    url=os.environ.get('DESK_TEST_DATABASE_URL')
    if not url:pytest.skip('live DB not set')
    db=Store(url)
    with db.lock('desk-test-lock') as first:
        with db.lock('desk-test-lock') as second:
            assert first is True and second is False
    with db.lock('desk-test-lock') as released:
        assert released is True


def load_legacy(monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('DESK_SECRET_KEY',raising=False)
    spec=importlib.util.spec_from_file_location('legacy_app',Path(__file__).resolve().parents[1]/'app.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_legacy_four_routes(monkeypatch):
    legacy=load_legacy(monkeypatch);client=legacy.app.test_client()
    legacy.LINE_CHANNEL_SECRET='test';legacy.LINE_XU_CHANNEL_SECRET='xu'
    legacy.PUSH_AUTH_TOKEN='push';legacy.LINE_XU_ACCESS_TOKEN='xu-token'
    monkeypatch.setattr(legacy,'get_user_profile',lambda *args:'Synthetic')
    def signed(path,secret):
        body=json.dumps({'events':[{'type':'message','source':{'userId':'synthetic-user'},'timestamp':1794218400000,'message':{'type':'text','text':'hello'}}]}).encode()
        signature=__import__('base64').b64encode(__import__('hmac').new(secret.encode(),body,hashlib.sha256).digest()).decode()
        return client.post(path,data=body,headers={'X-Line-Signature':signature})
    assert client.post('/webhook',data=b'{}').status_code==400
    assert signed('/webhook','test').data==b'OK'
    assert signed('/webhook-xu','xu').data==b'OK'
    assert len(client.get('/messages').get_json())==2
    assert client.post('/push-marketing',json={'text':'test'}).status_code==401
    post=Mock(return_value=Mock(status_code=200,text='{}'));monkeypatch.setattr(legacy.requests,'post',post)
    assert client.post('/push-marketing',headers={'X-Push-Token':'push'},json={'text':'test'}).status_code==200
    assert post.call_args.args[0].endswith('/broadcast')


def test_optional_import_failure_preserves_routes(monkeypatch):
    import builtins
    original=builtins.__import__
    def broken(name,*args,**kwargs):
        if name=='desk':raise ImportError('simulated')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',broken)
    legacy=load_legacy(monkeypatch)
    assert {r.rule for r in legacy.app.url_map.iter_rules()} >= {'/webhook','/webhook-xu','/push-marketing','/messages'}
    assert legacy.app.test_client().get('/').status_code==200


def test_install_requires_strong_key_and_no_network(monkeypatch):
    from desk import install
    app=Flask('optional')
    monkeypatch.delenv('DESK_SECRET_KEY',raising=False)
    install(app)
    assert 'desk_store' not in app.extensions
    monkeypatch.setenv('DESK_SECRET_KEY','short')
    with pytest.raises(ValueError):install(app)
    monkeypatch.setenv('DESK_SECRET_KEY','x'*48)
    monkeypatch.delenv('DESK_WORKER_ENABLED',raising=False)
    monkeypatch.setattr('psycopg2.connect',Mock(side_effect=AssertionError('no startup database I/O')))
    install(app)
    assert 'desk_store' in app.extensions
