"""Rollback-only knowledge workflow; no LINE or paid AI calls."""
import os
from pathlib import Path
import pytest
from psycopg2.extras import Json
from playwright.sync_api import expect
from desk.engine import Engine
from desk.knowledge import reference
from test_desk import db,logged,send_data
from test_workbench import ui,open_a


def payload(**changes):
    return dict(user_id='desk-test-a',question='2019 RAV4 原廠鏡頭滲水，可以換環景嗎？',
                answer='可以，沿用原廠鏡頭位置，鏡頭換成我們的。',
                summary='換環景可沿用原廠鏡頭位置',scope='2019 RAV4／環景',**changes)


def post(client,staff,path,data):
    return client.post('/desk/api/'+path,json=data,headers={'X-CSRF-Token':staff['csrf']})


def as_wayne(db,staff):
    db.execute('UPDATE desk_users SET username=%s WHERE id=%s',('Wayne',staff['id']))


def test_submission_optional_and_audited(db,logged):
    _,client,staff,notifier=logged
    before=dict(db.conversation('desk-test-a'))
    response=post(client,staff,'knowledge',payload())
    assert response.status_code==201
    row=db.one('SELECT * FROM desk_knowledge WHERE id=%s',(response.json['id'],))
    assert row['status']=='pending' and row['submitted_by']==staff['id']
    assert row['reviewed_at'] is None
    assert dict(db.conversation('desk-test-a'))==before
    assert reference(db,'RAV4 原廠鏡頭')=={'重點':'','問答':[]}
    assert db.one('SELECT action FROM desk_knowledge_events')['action']=='submit'
    notifier.push.assert_not_called()


def test_only_exact_wayne_can_review(db,logged):
    _,client,staff,notifier=logged
    kid=post(client,staff,'knowledge',payload()).json['id']
    for username in ('desk-test-user','wayne','Wayne-admin'):
        db.execute('UPDATE desk_users SET username=%s WHERE id=%s',(username,staff['id']))
        assert '知識待審' not in client.get('/desk/').text
        assert client.get('/desk/knowledge').status_code==403
        assert client.get('/desk/api/knowledge').status_code==403
        for action in ('approve','reject','withdraw'):
            assert post(client,staff,f'knowledge/{kid}/review',{'action':action,'version':1}).status_code==403
    as_wayne(db,staff)
    assert '知識待審' in client.get('/desk/').text
    assert client.get('/desk/knowledge').status_code==200
    assert len(client.get('/desk/api/knowledge').json['items'])==1
    notifier.push.assert_not_called()


def capture_draft(db):
    calls=[]
    def provider(messages):
        calls.append(messages)
        return {'products':[]} if len(calls)==1 else {'draft':'測試費用 77,777 元'}
    result=Engine(db,provider).draft([{'time':'2026-11-10 12:00:00','role':'user',
        'text':'2019 RAV4 原廠鏡頭換環景','new':True}], '2026-11-10 12:00:00')
    return calls[1][1]['content'],result


def test_approval_edit_prompt_price_and_withdraw(db,logged):
    _,client,staff,notifier=logged
    kid=post(client,staff,'knowledge',payload()).json['id']
    assert '唯一核准測試字串' not in capture_draft(db)[0]
    as_wayne(db,staff)
    data={**payload(),'action':'approve','version':1,'answer':'唯一核准測試字串，參考價 77,777 元'}
    assert post(client,staff,f'knowledge/{kid}/review',data).status_code==200
    prompt,result=capture_draft(db)
    assert '唯一核准測試字串' in prompt
    assert 'Wayne 核准的正確答法，優先於歷史問答' in prompt
    assert prompt.index('換環景可沿用') < prompt.index('"prices"')
    assert any('77,777' in flag for flag in result['flags'])
    approved=client.get('/desk/api/knowledge?status=approved').json['items'][0]
    assert approved['reviewer']=='Wayne' and approved['reviewed_at'] and approved['version']==2
    assert post(client,staff,f'knowledge/{kid}/review',data).status_code==409
    assert post(client,staff,f'knowledge/{kid}/review',{'action':'withdraw','version':2}).status_code==200
    assert '唯一核准測試字串' not in capture_draft(db)[0]
    assert [r['action'] for r in db.all('SELECT action FROM desk_knowledge_events ORDER BY id')]==['submit','approve','withdraw']
    notifier.push.assert_not_called()


def test_reject_never_used(db,logged):
    _,client,staff,_=logged
    kid=post(client,staff,'knowledge',payload()).json['id']
    as_wayne(db,staff)
    assert post(client,staff,f'knowledge/{kid}/review',{'action':'reject','version':1}).status_code==200
    assert reference(db,'RAV4 原廠鏡頭')['問答']==[]
    assert post(client,staff,f'knowledge/{kid}/review',{**payload(),'action':'approve','version':2}).status_code==409


@pytest.mark.parametrize('kind',['submit','approve','reject','withdraw'])
def test_knowledge_csrf(db,logged,kind):
    _,client,staff,notifier=logged
    kid=post(client,staff,'knowledge',payload()).json['id']
    as_wayne(db,staff)
    path='/desk/api/knowledge' if kind=='submit' else f'/desk/api/knowledge/{kid}/review'
    data=payload() if kind=='submit' else {**payload(),'action':kind,'version':1}
    for headers in ({},{'X-CSRF-Token':'invalid'}):
        assert client.post(path,json=data,headers=headers).status_code==403
    notifier.push.assert_not_called()


@pytest.mark.parametrize('data',[None,[],{}, {'question':'x'}, {**payload(),'summary':' '*3},
    {**payload(),'answer':7},{**payload(),'scope':'x'*201},{**payload(),'answer':'x'*5001}])
def test_invalid_submission(logged,data):
    _,client,staff,_=logged
    assert post(client,staff,'knowledge',data).status_code==400


def test_prefill_unsent_sent_and_new_messages(db,logged):
    _,client,staff,notifier=logged
    path='/desk/api/knowledge/prefill?user_id=desk-test-a'
    assert client.get(path).json=={'question':'主機報價？','answer':''}
    data=send_data();data['text']='剛送出成功的內容'
    assert post(client,staff,'send',data).status_code==200
    assert client.get(path).json=={'question':'主機報價？','answer':'剛送出成功的內容'}
    db.test_incoming['desk-test-a'].append({'id':102,'time_str':'2026-11-10 01:00:00','content':'新的環景問題','message_type':'text'})
    assert client.get(path).json=={'question':'新的環景問題','answer':''}
    assert notifier.push.call_count==1  # Fake only; save never sends.


def test_reference_budget_and_top_three(db,logged):
    _,client,staff,_=logged
    as_wayne(db,staff)
    for i in range(15):
        data={**payload(),'summary':f'重點{i} '+('甲'*290),'question':f'問題{i}'}
        if i==14:data.update(question='RAV4 原廠鏡頭環景',summary='高相似度的鏡頭知識')
        kid=post(client,staff,'knowledge',data).json['id']
        assert post(client,staff,f'knowledge/{kid}/review',{**data,'action':'approve','version':1}).status_code==200
    refs=reference(db,'RAV4 原廠鏡頭環景')
    assert len(refs['重點'])<=3000 and refs['重點'].startswith('高相似度的鏡頭知識')
    assert len(refs['問答'])==3 and refs['問答'][0]['question']=='RAV4 原廠鏡頭環景'


@pytest.mark.parametrize('mobile',[False,True])
def test_ui_submit_review_and_screenshots(ui,logged,mobile,tmp_path):
    page,db,notifier=ui
    _,_,staff,_=logged
    folder=Path(os.environ.get('DESK_EVIDENCE_DIR',str(tmp_path)));folder.mkdir(parents=True,exist_ok=True)
    prefix='mobile' if mobile else 'desktop'
    if mobile:page.set_viewport_size({'width':390,'height':844})
    open_a(page)
    expect(page.locator('#learn-open')).to_be_visible()
    page.screenshot(path=str(folder/f'{prefix}_learn_button.png'),full_page=True)
    page.locator('#learn-open').click()
    expect(page.locator('#learn-question')).to_have_value('主機報價？')
    expect(page.locator('#learn-answer')).to_have_value('您好，RAV4 可安裝 EX 安卓主機。')
    page.locator('#learn-question').fill(payload()['question'])
    page.locator('#learn-answer').fill(payload()['answer'])
    page.locator('#learn-summary').fill(payload()['summary'])
    page.locator('#learn-scope').fill(payload()['scope'])
    page.screenshot(path=str(folder/f'{prefix}_learn_form.png'),full_page=True)
    page.locator('#learn-submit').click()
    expect(page.locator('#learn-dialog')).not_to_be_visible()
    expect(page.locator('#feedback')).to_contain_text('已送交 Wayne 審核')
    expect(page.locator('#draft')).to_have_value('您好，RAV4 可安裝 EX 安卓主機。')
    as_wayne(db,staff)
    page.goto(page.url.split('/desk/')[0]+'/desk/knowledge')
    expect(page.locator('.knowledge-item')).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(folder/f'{prefix}_knowledge_pending.png'),full_page=True)
    page.locator('[name=answer]').fill('修改後的正確答法')
    page.get_by_role('button',name='核准',exact=True).click()
    expect(page.locator('#feedback')).to_contain_text('已核准')
    page.locator('[data-status=approved]').click()
    expect(page.locator('[name=answer]')).to_have_value('修改後的正確答法')
    page.get_by_role('button',name='撤回核准').click()
    expect(page.locator('#feedback')).to_contain_text('已撤回')
    notifier.push.assert_not_called()


def test_ui_after_send_can_learn_from_done(ui):
    page,db,notifier=ui
    open_a(page);page.locator('#open-confirm').click();page.locator('#send').click()
    expect(page.locator('#feedback')).to_have_text('已送出並記錄。')
    page.locator('[data-tab=done]').click();open_a(page)
    page.locator('#learn-open').click()
    expect(page.locator('#learn-question')).to_have_value('主機報價？')
    expect(page.locator('#learn-answer')).to_have_value('您好，RAV4 可安裝 EX 安卓主機。')
    assert notifier.push.call_count==1
