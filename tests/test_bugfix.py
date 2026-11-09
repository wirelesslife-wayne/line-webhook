"""Regression coverage; FakeNotifier and rollback-only temporary Postgres tables."""
from datetime import timedelta
from unittest.mock import Mock
import uuid
import pytest
from psycopg2.extras import Json
from desk.catalog import select_images,fetch_catalog,product_images,refresh_catalog,original_candidates
import requests
from desk.db import now, Store
from desk.engine import Engine
from desk.service import send,mark_done,redraft,ActionError
from desk.worker import timeline,draft_once
from test_desk import db,logged,send_data


def image(name,code):
    return {'name':name,'url':f'https://example.com/{code}.jpg','categories':[]}


def test_rav4_android_images_exclude_unrelated_and_limit():
    index=[image('RAV4 安卓主機',str(i)) for i in range(6)]
    index += [image('RAV4 電動尾門','tail'),image('RAV4 電子後視鏡','mirror'),image('Corolla 安卓主機','wrong'),image('Z8 安卓盒','box')]
    wlf={'cars':[{'model':'RAV4'},{'model':'Corolla'}]}
    selected=select_images(index,wlf,'RAV4 安卓',{'model':'RAV4','products':['安卓主機']})
    assert len(selected)==4
    assert all(i['name']=='RAV4 安卓主機' for i in selected)


@pytest.mark.parametrize('query,ex',[
    ('不認識的商品',{}),('',{}),('RAV4',{'model':'RAV4'}),
    ('火箭發射器',{'products':['火箭發射器']}),
])
def test_no_product_no_fallback(query,ex):
    assert select_images([image('安卓主機','a')],{'z':[{'name':'Z8 安卓盒','image':'https://example.com/z.jpg'}]},query,ex)==[]


def test_series_and_wlf_z_mapping():
    index=[image('EX 安卓主機','ex'),image('DX 安卓主機','dx')]
    assert select_images(index,{},'',{'products':['安卓主機'],'series':'EX'})==[{'name':'EX 安卓主機','url':'https://example.com/ex.jpg'}]
    assert select_images([],{'z':[{'name':'Z8 安卓盒','image':'https://example.com/z.jpg'}]},'',{'products':['Z8']})[0]['name']=='Z8 安卓盒'


def test_complete_catalog_and_jpg_png_only():
    products=[{'id':i,'name':'安卓主機','categories':[],'images':[{'src':f'https://example.com/{i}.jpg'},{'src':'https://example.com/a.webp'}]} for i in range(101)]
    responses=[Mock(headers={'X-WP-Total':'101'},json=lambda:products[:100]),Mock(headers={'X-WP-Total':'101'},json=lambda:products[100:])]
    fetch=Mock(side_effect=responses)
    assert len(product_images(fetch_catalog(fetch)))==101
    assert fetch.call_args.kwargs['params']['page']==2


def test_incomplete_catalog_preserves_previous(db):
    db.set_meta('product_images',[image('安卓主機','previous')])
    fetch=Mock(return_value=Mock(headers={'X-WP-Total':'2'},json=lambda:[{'id':1}]))
    refresh_catalog(db,fetch)
    assert db.meta('product_images_error') is True
    assert db.meta('product_images')[0]['name']=='安卓主機'


def test_webp_original_candidates_stay_on_same_upload_path():
    candidates=original_candidates('https://example.com/uploads/item-300x300.webp')
    assert candidates == [
        'https://example.com/uploads/item-300x300.jpg',
        'https://example.com/uploads/item-300x300.png',
        'https://example.com/uploads/item-scaled.jpg',
        'https://example.com/uploads/item-scaled.png',
        'https://example.com/uploads/item.jpg',
        'https://example.com/uploads/item.png']
    assert original_candidates('http://example.com/a.webp')==[]
    assert original_candidates('https://user:pass@example.com/a.webp')==[]
    assert original_candidates('https://example.com/a.jpg')==[]
    assert 'https://example.com/a.jpg' in original_candidates('https://example.com/a.jpg.webp')


@pytest.mark.parametrize('code,mime',[(404,'image/jpeg'),(200,'text/html'),(200,'image/webp'),(301,'image/jpeg')])
def test_webp_invalid_head_never_becomes_attachment(code,mime):
    products=[{'name':'EX 安卓主機','images':[{'src':'https://example.com/a.webp'}]}]
    head=Mock(return_value=Mock(status_code=code,headers={'Content-Type':mime}))
    assert product_images(products,head)==[]
    assert head.call_args.kwargs=={'timeout':8,'allow_redirects':False}


def test_webp_verified_original_and_per_batch_dedup():
    products=[{'name':'EX 安卓主機','images':[{'src':'https://example.com/a.webp'}]}]*2
    head=Mock(side_effect=[Mock(status_code=404,headers={}),
        Mock(status_code=200,headers={'Content-Type':'image/png; charset=binary'})])
    resolutions={}
    result=product_images(products,head,resolutions)
    assert head.call_count==2
    assert len(result)==2 and all(p['url']=='https://example.com/a.png' for p in result)
    assert resolutions=={'https://example.com/a.webp':'https://example.com/a.png'}


def test_webp_network_failure_returns_empty_and_caches_miss():
    products=[{'name':'EX 安卓主機','images':[{'src':'https://example.com/a.webp'}]}]*2
    head=Mock(side_effect=requests.Timeout())
    resolutions={}
    assert product_images(products,head,resolutions)==[]
    assert head.call_count==len(original_candidates('https://example.com/a.webp'))
    assert resolutions=={'https://example.com/a.webp':None}


def test_webp_resolution_only_in_daily_refresh_and_db_cache(db):
    products=[{'id':1,'name':'EX 安卓主機','images':[{'src':'https://example.com/a.webp'}]}]
    fetch=Mock(return_value=Mock(headers={'X-WP-Total':'1'},json=lambda:products))
    head=Mock(return_value=Mock(status_code=200,headers={'Content-Type':'image/jpeg'}))
    refresh_catalog(db,fetch,head)
    refresh_catalog(db,fetch,head)
    head.assert_called_once()
    fetch.assert_called_once()
    assert db.meta('product_image_originals')=={'https://example.com/a.webp':'https://example.com/a.jpg'}
    assert Engine(db).attachments('EX 安卓主機',{'products':['安卓主機'],'series':'EX'})[0]['url']=='https://example.com/a.jpg'
    head.assert_called_once()


def test_old_unhandled_history_survives_24_hours():
    t=now();store=Mock()
    store.incoming.return_value=[{'id':1,'time_str':(t-timedelta(days=3)).isoformat(),'message_type':'text','content':'已處理舊訊息'},
                                {'id':2,'time_str':(t-timedelta(days=2)).isoformat(),'message_type':'text','content':'尚未處理舊訊息'}]
    store.all.return_value=[]
    rows=timeline(store,{'user_id':'test','handled_upto':1},for_ai=True,current=t)
    assert [r['text'] for r in rows]==['尚未處理舊訊息']


@pytest.mark.parametrize('result,expected',[
    ({'ok':True,'status':200,'reason':''},'sent'),
    ({'ok':False,'status':400,'reason':'格式錯誤'},'failed'),
    ({'ok':False,'status':0,'reason':'逾時','unknown':True},'unknown'),
    ({'ok':False,'status':500,'reason':'回應不明','unknown':True},'unknown'),
])
def test_fake_notifier_outcomes(db,logged,result,expected):
    _,_,staff,notifier=logged;notifier.push.return_value=result;data=send_data()
    if result['ok']:
        assert send(db,notifier,staff,data)['ok']
    else:
        with pytest.raises(ActionError):send(db,notifier,staff,data)
    assert db.one('SELECT status FROM desk_send_attempts WHERE retry_key=%s',(data['retry_key'],))['status']==expected
    assert db.conversation(data['user_id'])['status']==('done' if result['ok'] else 'waiting')


def test_notifier_exception_persists_unknown(db,logged):
    _,_,staff,notifier=logged;notifier.push.side_effect=TimeoutError('private')
    data=send_data()
    with pytest.raises(ActionError,match='待確認'):send(db,notifier,staff,data)
    assert db.one('SELECT status FROM desk_send_attempts WHERE retry_key=%s',(data['retry_key'],))['status']=='unknown'


def test_two_staff_same_source_and_success_idempotency(db,logged):
    _,_,staff,notifier=logged;data=send_data()
    assert send(db,notifier,staff,data)['ok']
    assert send(db,notifier,staff,data)['already_sent']
    db.execute("UPDATE desk_send_attempts SET updated_at=NOW()-INTERVAL '11 seconds'")
    other={**data,'retry_key':str(uuid.uuid4())}
    with pytest.raises(ActionError,match='已處理'):send(db,notifier,{**staff,'id':staff['id']+1},other)
    notifier.push.assert_called_once()


@pytest.mark.parametrize('state',['failed','manually_checked'])
def test_closed_retry_key_cannot_send(db,logged,state):
    _,_,staff,notifier=logged;data=send_data()
    db.execute('''INSERT INTO desk_send_attempts(retry_key,user_id,staff_id,content,images,source_upto,status)
        VALUES(%s,%s,%s,%s,%s,%s,%s)''',(data['retry_key'],data['user_id'],staff['id'],data['text'],Json([]),101,state))
    with pytest.raises(ActionError,match='已結束'):send(db,notifier,staff,data)
    notifier.push.assert_not_called()


def test_retry_uid_version_and_staff_binding(db,logged):
    _,_,staff,notifier=logged;data=send_data()
    notifier.push.return_value={'ok':False,'status':0,'reason':'未知','unknown':True}
    with pytest.raises(ActionError):send(db,notifier,staff,data)
    for change in [{'source_upto':102},{'user_id':'desk-test-b'},{'text':'改過的字'}]:
        with pytest.raises(ActionError,match='不同內容'):send(db,notifier,staff,{**data,**change})
    notifier.push.assert_called_once()


def test_other_staff_pending_is_visible_without_retry(db,logged):
    _,client,staff,notifier=logged;data=send_data()
    notifier.push.return_value={'ok':False,'status':0,'reason':'未知','unknown':True}
    with pytest.raises(ActionError):send(db,notifier,staff,data)
    db.execute("INSERT INTO desk_users(username,password_hash) VALUES('second-staff','unused')")
    other=db.one("SELECT id FROM desk_users WHERE username='second-staff'")['id']
    db.execute('UPDATE desk_send_attempts SET staff_id=%s',(other,))
    pending=client.get('/desk/api/conversation?user_id=desk-test-a').get_json()['pending']
    assert pending['staff']=='second-staff' and not pending['can_retry']


def test_stale_draft_after_ingest_not_autofilled(db,logged):
    _,client,_,_=logged
    db.execute("UPDATE desk_conversations SET draft=%s,drafted_upto=100 WHERE user_id='desk-test-a'",(Json({'draft':'舊草稿'}),))
    result=client.get('/desk/api/conversation?user_id=desk-test-a').get_json()
    assert result['stale_draft'] and result['result']['draft']=='舊草稿'


def test_draft_claim_lost_never_calls_provider(db,monkeypatch):
    original=db.execute
    def execute(sql,args=()):
        if "SET status='drafting'" in sql:return 0
        return original(sql,args)
    monkeypatch.setattr(db,'execute',execute);engine=Mock()
    draft_once(db,engine);engine.assert_not_called()


@pytest.mark.parametrize('state',['done','skip','drafting'])
def test_redraft_rejects_invalid_state(db,logged,state):
    _,_,staff,_=logged
    db.execute("UPDATE desk_conversations SET status=%s WHERE user_id='desk-test-a'",(state,))
    with pytest.raises(ActionError):redraft(db,staff,'desk-test-a',101)


def test_mark_done_new_source_rolls_back_actions(db,logged):
    _,_,staff,_=logged
    before=db.all('SELECT * FROM desk_actions')
    db.test_incoming['desk-test-a'].append({'id':102})
    with pytest.raises(ActionError,match='新訊息'):mark_done(db,staff,'desk-test-a',101)
    assert db.conversation('desk-test-a')['handled_upto']==0
    assert db.all('SELECT * FROM desk_actions')==before


@pytest.mark.parametrize('ingest_first',[False,True])
def test_mark_ingest_both_orderings_with_real_source_rows(db,logged,monkeypatch,ingest_first):
    _,_,staff,_=logged
    db.execute('''INSERT INTO messages(id,user_id,display_name,time_str)
        VALUES(101,'desk-test-a','測試客人甲',%s)''',(now().strftime('%Y-%m-%d %H:%M:%S'),))
    db.set_meta('source_cursor',101)
    monkeypatch.setattr(db,'latest_source',lambda uid:Store.latest_source(db,uid))
    def incoming():
        db.execute('''INSERT INTO messages(id,user_id,display_name,time_str)
            VALUES(102,'desk-test-a','測試客人甲',%s)''',(now().strftime('%Y-%m-%d %H:%M:%S'),))
        assert db.ingest()==1
    if ingest_first:
        incoming()
        with pytest.raises(ActionError,match='新訊息'):
            mark_done(db,staff,'desk-test-a',101)
    else:
        mark_done(db,staff,'desk-test-a',101)
        incoming()
    c=db.conversation('desk-test-a')
    assert c['last_source_id']==102 and c['status']=='waiting'
    assert c['handled_upto']==(0 if ingest_first else 101)


def test_new_message_during_delivery_remains_unhandled(db,logged):
    _,_,staff,notifier=logged
    def arrived(*args):
        db.test_incoming['desk-test-a'].append({'id':102})
        return {'ok':True,'status':200,'reason':''}
    notifier.push.side_effect=arrived
    send(db,notifier,staff,send_data())
    c=db.conversation('desk-test-a')
    assert c['status']=='waiting' and c['handled_upto']==101
