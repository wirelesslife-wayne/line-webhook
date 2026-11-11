from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from unittest.mock import Mock
import pytest
import requests
from desk.engine import Engine, ProviderPending
from desk.privacy import redact
from desk.provider import OpenAIProvider, Unavailable, budget_status, month_key, usage_cost, EXHAUSTED
from desk.worker import timeline
from test_desk import db, logged, send_data


class Ledger:
    def __init__(self):
        self.rows = []
        self.acquired = True

    @contextmanager
    def lock(self, name):
        assert name == 'desk-ai-budget'
        yield self.acquired

    def one(self, sql, args):
        rows = [r for r in self.rows if r['month']==args[0]]
        return {'usd':sum((r['usd'] for r in rows),Decimal(0)),
                'reserved':sum((r['usd'] for r in rows if r['status']!='settled'),Decimal(0))}

    def execute(self, sql, args):
        if sql.startswith('INSERT'):
            uid,month,model,usd = args
            self.rows.append(dict(id=uid,month=month,model=model,usd=usd,status='reserved'))
        else:
            cost,incoming,cached,outgoing,uid = args
            row = next(r for r in self.rows if r.get('id')==uid)
            row.update(usd=cost,status='settled',input_tokens=incoming,cached_tokens=cached,output_tokens=outgoing)


def reply(result=None, usage=None, status='completed'):
    return {'status':status,'usage':usage or {'input_tokens':1000,'input_tokens_details':{'cached_tokens':200},'output_tokens':100},
            'output':[{'type':'reasoning'}, {'type':'message','content':[{'type':'output_text','text':json.dumps(result or {'draft':'請提供車款年份'})}]}]}


@pytest.mark.parametrize('value',[
    '0912-345-678','0912345678','+886 912 345 678','02-2345-6789','+886-2-23456789',
    'a.b@example.com','ABC-1234','1234-AB','AB-123','ABC1234','A123456789',
    'U'+'a'*32,
    'AB1234','1234AB',
])
def test_privacy_formats(value):
    assert value not in redact('資料：'+value+'，想問 Toyota Corolla 2021 主機 $18,000')
    assert 'Corolla 2021 主機 $18,000' in redact(value+' Corolla 2021 主機 $18,000')


def test_text_only_24h_boundary_and_media():
    end = datetime(2026,11,9,12,tzinfo=timezone.utc)
    incoming = [
      {'id':1,'time_str':'2026-11-08 11:59:59','message_type':'text','content':'OLD_PRIVATE'},
      {'id':2,'time_str':'2026-11-08 12:00:00','message_type':'text','content':'BOUNDARY'},
      {'id':3,'time_str':'2026-11-09 11:00:00','message_type':'image','content':'https://private/image.jpg'},
      {'id':4,'time_str':'2026-11-09 11:30:00','message_type':'video','content':'https://private/movie.mp4'},
      {'id':5,'time_str':'2026-11-09 13:00:00','message_type':'text','content':'FUTURE'},
    ]
    store=Mock()
    store.incoming.return_value=incoming
    store.all.return_value=[{'sent_at':end-timedelta(hours=1),'content':'我方已回覆','images':['https://private/sent.png']},
      {'sent_at':end-timedelta(hours=25),'content':'OLD_REPLY','images':[]}]
    result=timeline(store,{'user_id':'synthetic','handled_upto':1},for_ai=True,current=end)
    text=json.dumps(result,ensure_ascii=False)
    assert all(v not in text for v in ['OLD_PRIVATE','OLD_REPLY','FUTURE','https://private'])
    assert all(v in text for v in ['BOUNDARY','[image]','[video]','我方已回覆'])
    assert 'images' not in text and 'user_id' not in text
    # UI retains the complete history and original media links.
    assert 'OLD_PRIVATE' in json.dumps(timeline(store,{'user_id':'synthetic','handled_upto':1}))


@pytest.mark.parametrize('value',['A123456789','U'+'a'*32,'a@foo.tw','AB1234'])
def test_privacy_identifiers_next_to_chinese(value):
    assert value not in redact('我的資料'+value+'請處理')


def test_month_is_taipei_calendar():
    assert month_key(datetime(2026,11,30,15,59,59,tzinfo=timezone.utc))=='2026-11'
    assert month_key(datetime(2026,11,30,16,tzinfo=timezone.utc))=='2026-12'


def test_cost_counts_cached_and_reasoning_once():
    assert usage_cost({'input_tokens':1000,'input_tokens_details':{'cached_tokens':200},
      'output_tokens':100,'output_tokens_details':{'reasoning_tokens':80}})==(Decimal('.00262'),1000,200,100)


@pytest.mark.parametrize('usage',[
    {}, {'input_tokens':-1,'output_tokens':0}, {'input_tokens':True,'output_tokens':0},
    {'input_tokens':1,'output_tokens':0,'input_tokens_details':{'cached_tokens':2}},
])
def test_invalid_usage_fails_closed(usage):
    with pytest.raises((ValueError,KeyError)):
        usage_cost(usage)


def test_adapter_redacts_entire_request_and_settles(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-test-key')
    db=Ledger()
    post=Mock(return_value=Mock(status_code=200,json=lambda:reply()))
    text='0912-345-678 ABC-1234 a@foo.tw A123456789 U'+'a'*32
    result=OpenAIProvider(db,post)([{'role':'system','content':'規則含 '+text,'metadata':'private'},
      {'role':'user','content':'詢價 '+text,'user_id':'DO_NOT_SEND'}])
    assert result['draft']
    payload=post.call_args.kwargs['json']
    wire=json.dumps(payload)
    assert all(v not in wire for v in ['0912-345-678','ABC-1234','a@foo.tw','A123456789','DO_NOT_SEND','metadata'])
    assert payload['store'] is False and payload['model']=='gpt-6.1-sol'
    assert payload['max_output_tokens']==3000 and payload['service_tier']=='default'
    assert post.call_args.kwargs['allow_redirects'] is False
    assert db.rows[0]['status']=='settled' and db.rows[0]['usd']==Decimal('.00262')
    assert set(db.rows[0])=={'id','month','model','usd','status','input_tokens','cached_tokens','output_tokens'}


def test_reservation_committed_before_http(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger()
    def post(*args,**kwargs):
        assert db.rows[0]['status']=='reserved' and db.rows[0]['usd']>0
        return Mock(status_code=200,json=lambda:reply())
    OpenAIProvider(db,post)([{'role':'user','content':'JSON 詢價'}])


@pytest.mark.parametrize('mode',['timeout','http','usage','json','incomplete'])
def test_failure_reservations_and_safe_errors(monkeypatch,mode):
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger()
    body=reply()
    if mode=='usage':body.pop('usage')
    if mode=='json':body['output'][1]['content'][0]['text']='invalid private raw response'
    if mode=='incomplete':body['status']='incomplete'
    post=Mock(side_effect=requests.Timeout('private API key')) if mode=='timeout' else Mock(return_value=Mock(status_code=500 if mode=='http' else 200,json=lambda:body))
    with pytest.raises(ProviderPending) as error:
        OpenAIProvider(db,post)([{'role':'user','content':'JSON 測試'}])
    assert 'private' not in str(error.value)
    assert len(db.rows)==1 and db.rows[0]['usd']>0
    assert db.rows[0]['status']==('settled' if mode in ('json','incomplete') else 'reserved')
    post.assert_called_once()


def test_budget_warning_stop_and_next_month(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger()
    db.rows=[dict(month='2026-11',usd=Decimal('24'),status='settled')]
    at=datetime(2026,11,9,tzinfo=timezone.utc)
    assert '80%' in budget_status(db,at)['warning']
    db.rows[0]['usd']=Decimal('30')
    assert budget_status(db,at)['warning']==EXHAUSTED
    monkeypatch.setattr('desk.provider.now',lambda:at)
    post=Mock()
    with pytest.raises(ProviderPending,match=EXHAUSTED):OpenAIProvider(db,post)([{'role':'user','content':'JSON'}])
    post.assert_not_called()
    monkeypatch.setattr('desk.provider.now',lambda:datetime(2026,11,30,16,tzinfo=timezone.utc))
    post.return_value=Mock(status_code=200,json=lambda:reply())
    OpenAIProvider(db,post)([{'role':'user','content':'JSON'}])
    assert db.rows[-1]['month']=='2026-12'


@pytest.mark.parametrize('configuration',['missing_key','unknown_model','bad_cap','zero_cap','tiny_cap','oversize','busy'])
def test_preflight_never_calls_api(monkeypatch,configuration):
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger();post=Mock();text='JSON'
    if configuration=='missing_key':monkeypatch.delenv('OPENAI_API_KEY')
    if configuration=='unknown_model':monkeypatch.setenv('DESK_MODEL','unknown')
    if configuration=='bad_cap':monkeypatch.setenv('DESK_MONTHLY_USD','NaN')
    if configuration=='zero_cap':monkeypatch.setenv('DESK_MONTHLY_USD','0')
    if configuration=='tiny_cap':monkeypatch.setenv('DESK_MONTHLY_USD','.00001')
    if configuration=='oversize':text='字'*30000
    if configuration=='busy':db.acquired=False
    provider=OpenAIProvider(db,post);provider.patience,provider.pause=2,0
    with pytest.raises(ProviderPending):provider([{'role':'user','content':text}])
    post.assert_not_called();assert db.rows==[]


def test_waits_for_its_turn_when_another_draft_is_running(monkeypatch):
    """11/11: LINE and FB drafters ran together after the top-up; whoever lost the lock gave up for good."""
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger();db.acquired=False
    def free_the_lock(seconds):db.acquired=True
    monkeypatch.setattr('desk.provider.time.sleep',free_the_lock)
    post=Mock(return_value=Mock(status_code=200,json=lambda:reply()))
    assert OpenAIProvider(db,post)([{'role':'user','content':'JSON 測試'}])['draft']
    post.assert_called_once()


def test_real_db_usage_settlement_and_two_stage_engine(db,monkeypatch):
    before=db.one("SELECT COUNT(*) AS n FROM desk_ai_usage WHERE status='settled'")['n']
    monkeypatch.setenv('OPENAI_API_KEY','test')
    post=Mock(side_effect=[Mock(status_code=200,json=lambda:reply({'brand':None,'model':None,'products':[]})),
                           Mock(status_code=200,json=lambda:reply({'draft':'您好，請問車款年份？'}))])
    monkeypatch.setattr(requests,'post',post)
    result=Engine(db).draft([{'time':'2026-11-09 18:30:00','role':'user','text':'0912345678 詢價','new':True},
       {'time':'2026-11-09 18:31:00','role':'assistant','text':'我方先前確認','new':False}],datetime(2026,11,9,tzinfo=timezone.utc))
    assert result['draft']
    assert db.one("SELECT COUNT(*) AS n FROM desk_ai_usage WHERE status='settled'")['n']==before+2
    assert '我方先前確認' in post.call_args.kwargs['json']['input'][1]['content']
    assert '0912345678' not in json.dumps(post.call_args_list[-1].kwargs['json'])


def test_real_db_exhausted_header_and_manual_send(db,logged):
    _,client,staff,notifier=logged
    db.execute("INSERT INTO desk_ai_usage(id,month,model,usd,status) VALUES(gen_random_uuid(),%s,'gpt-6.1-sol',30,'settled')",(month_key(),))
    listing=client.get('/desk/api/list').get_json()
    assert listing['ai_budget']['blocked'] and listing['ai_budget']['warning']==EXHAUSTED
    assert client.post('/desk/api/send',json=send_data(),headers={'X-CSRF-Token':staff['csrf']}).status_code==200
    notifier.push.assert_called_once()


def test_blocked_drafts_resume_on_next_month(db,monkeypatch):
    from desk.worker import draft_once
    from psycopg2.extras import Json
    db.execute("UPDATE desk_conversations SET status='blocked',draft=%s WHERE user_id='desk-test-a'",
        (Json({'draft':'','budget_month':'2026-11','staff_note':EXHAUSTED}),))
    monkeypatch.setattr('desk.provider.now',lambda:datetime(2026,12,1,tzinfo=timezone.utc))
    class Synthetic:
        def __init__(self,store):pass
        def draft(self,*args):return {'draft':'恢復擬稿'}
    draft_once(db,Synthetic)
    assert db.conversation('desk-test-a')['status']=='ready'


def test_refused_request_holds_no_budget(monkeypatch):
    """11/11: 104 out-of-credit refusals each kept a reservation and ate the monthly cap."""
    monkeypatch.setenv('OPENAI_API_KEY','test')
    db=Ledger()
    post=Mock(return_value=Mock(status_code=429,json=lambda:{'error':{'message':'private'}}))
    with pytest.raises(Unavailable) as error:
        OpenAIProvider(db,post)([{'role':'user','content':'JSON 測試'}])
    assert 'private' not in str(error.value)
    assert len(db.rows)==1 and db.rows[0]['usd']==0 and db.rows[0]['status']=='settled'


def test_published_shop_details_survive_redaction():
    shop=['台中市太平區新平路一段267號','0923-374-585']
    text='經銷商｜'+shop[0]+'｜'+shop[1]+'；客人電話 0912-345-678，住台中市北區三民路三段100號'
    out=redact(text,shop)
    assert shop[0] in out and shop[1] in out
    assert '0912-345-678' not in out and '三民路' not in out
    assert shop[1] not in redact(text)


def test_model_code_with_year_is_not_a_plate():
    """11/11: "TOYOTA cc 2026" reached the AI as "TOYOTA [車牌]"."""
    models=frozenset({'CC','CRV','HRV','NX'})
    for text in ['TOYOTA cc 2026年車','2026 CC安裝多少','CRV 2020','2019HRV','NX-2023','NX200能裝嗎']:
        assert '[車牌]' not in redact(text,models=models), text
    for text in ['ABC-1234','CC-1234','ABC 2020','2020-ABC','車牌 CC-2026','NX-201','200-NX']:
        assert '[車牌]' in redact(text,models=models), text
    assert '[車牌]' in redact('cc 2026')  # without the model list everything plate-shaped stays masked
