import json
from flask import request, jsonify, g, render_template, Response
from . import fb, fb_media, library, meta
from .errors import ActionError
from .knowledge import can_review, require_reviewer, submit
from .worker import local_time, line_on


def register(bp,store):
    def get_thread(tid):
        c=fb.conversation(store,tid) if isinstance(tid,str) else None
        if not c:
            raise ActionError('找不到對話',404)
        return c

    def details(rows):
        # One connection for the whole page: the list is polled every few seconds by a single web worker.
        if not rows:
            return []
        ids=[c['id'] for c in rows]
        with store.cursor() as cur:
            cur.execute("""SELECT a.*,u.username FROM desk_fb_attempts a JOIN desk_users u ON u.id=a.staff_id
              WHERE thread_id=ANY(%s) ORDER BY approved_at DESC""",(ids,))
            all_attempts=cur.fetchall()
            cur.execute('SELECT * FROM desk_fb_events WHERE thread_id=ANY(%s) ORDER BY id',(ids,))
            events=cur.fetchall()
            cur.execute(f"""SELECT id,filename,content_type,thread_id,attempt_key,({fb_media.FREE}) AS free FROM desk_fb_media m
              WHERE thread_id=ANY(%s) AND removed_at IS NULL ORDER BY uploaded_at,id""",(ids,))
            files=cur.fetchall()
            cur.execute("SELECT thread_id,taken_at FROM desk_fb_shots WHERE thread_id=ANY(%s) AND taken_at >= NOW()-INTERVAL '30 days'",(ids,))
            shots={s['thread_id']:local_time(s['taken_at'])[5:16] for s in cur.fetchall()}
        out=[]
        for c in rows:
            attempts=[a for a in all_attempts if a['thread_id']==c['id']][:10]
            mine=[m for m in files if m['thread_id']==c['id']]
            sent=next((a['retry_key'] for a in attempts if a['status'] in ('approved','sent')),None)
            media=[m for m in mine if m['free']] if c['status'] in fb.TODO else [m for m in mine if sent and m['attempt_key']==sent]
            out.append(dict(id=c['id'],name=c['name'],channel=c['channel'],label=fb.LABELS[c['channel']],key=c['key'],url=c['url'],
                context=c['context'],status=c['status'],version=c['version'],note=c['note'],result=c['draft'] or {},
                messages=fb.shape(e for e in events if e['thread_id']==c['id'])[-12:],is_public=c['channel'].endswith('_comment'),
                media=[fb_media.describe(m) for m in media],shot=shots.get(c['id']),live=meta.live(c),
                attempts=[dict(status=a['status'],time=local_time(a['approved_at']),staff=a['username'],text=a['content'],reason=a['reason']) for a in attempts]))
        return out

    def detail(c):
        return details([c])[0]

    def listing():
        channel,tab=request.args.get('channel',''),request.args.get('tab','todo')
        if channel and channel not in fb.CHANNELS or tab not in ('todo','approved','done'):
            raise ActionError('分類不正確',400)
        try: page=max(0,int(request.args.get('page','0')))
        except ValueError: raise ActionError('頁碼不正確',400)
        order=request.args.get('order','old')
        if order not in ('old','new'): raise ActionError('排序不正確',400)
        states=list(fb.TODO) if tab=='todo' else ['approved'] if tab=='approved' else ['done','skip']
        # Wayne 11/14: the customer who has waited longest first, unless he switches to newest first
        # by when the customer last wrote (the desk may read it an hour later), not when the desk picked it up
        said="COALESCE((SELECT MAX(e.event_at) FROM desk_fb_events e WHERE e.thread_id=t.id AND e.role='user'),t.last_at)"
        rows=store.all(f"""SELECT t.* FROM desk_fb_threads t WHERE t.status=ANY(%s) AND (%s='' OR t.channel=%s)
          ORDER BY {said} {'' if order=='old' else 'DESC'},t.id LIMIT 21 OFFSET %s""",(states,channel,channel,page*20))
        counts=fb.status(store)
        per=({r['channel']:r['n'] for r in store.all('SELECT channel,COUNT(*) AS n FROM desk_fb_threads WHERE status=ANY(%s) GROUP BY channel',(states,))}
             if tab!='done' else {})  # the finished tab only grows; a count there says nothing
        return dict(items=details(rows[:20]),has_more=len(rows)>20,page=page,tab=tab,channel=channel,order=order,per_channel=per,
            todo=counts['waiting_draft']+counts['awaiting_review']+counts['draft_failed'],approved=counts['approved_unsent'])

    @bp.get('/fb')
    def fb_page():
        return render_template('fb.html',line_on=line_on(),staff=g.staff,can_review=can_review(g.staff),labels=fb.LABELS,view=listing(),
            rules=[dict(id=r['id'],instruction=r['instruction'],active=r['active'],label=fb.LABELS.get(r['channel'],r['channel']),
                        time=local_time(r['created_at'])[5:16],said=r['conversation_summary'][:60])
                   for r in store.all('SELECT * FROM desk_edit_rules ORDER BY id DESC LIMIT 200')] if can_review(g.staff) else [])

    @bp.get('/api/fb/list')
    def fb_list(): return jsonify(listing())

    @bp.get('/api/fb/conversation')
    def fb_detail(): return jsonify(detail(get_thread(request.args.get('thread_id'))))

    @bp.post('/api/fb/send')
    def fb_send():
        data=request.get_json(silent=True)
        c=get_thread(data.get('thread_id')) if isinstance(data,dict) else None
        if c: meta.check_sendable(c)
        out=fb.approve(store,g.staff,data)
        if c and meta.live(c) and out.get('status')=='approved':
            meta.dispatch(store,out['attempt_id'])  # Send API right away, in the background
            out['live']=True
        return jsonify(out)

    # Meta webhook (Messenger + Instagram). Only signed deliveries are accepted; ignored while DESK_META_API is off.
    @bp.route('/meta/webhook',methods=['GET','POST'])
    def meta_webhook():
        if request.method=='GET':
            challenge=meta.verify_challenge(request.args)
            return (challenge,200,{'Content-Type':'text/plain'}) if challenge is not None else ('Forbidden',403)
        body=request.get_data(cache=False)
        if not meta.signed(body,request.headers.get('X-Hub-Signature-256')):
            return 'Forbidden',403
        if meta.mode()=='off':
            return 'EVENT_RECEIVED',200
        try:
            payload=json.loads(body)
        except ValueError:
            return 'Bad Request',400
        meta.enqueue(store,payload)
        meta.RUN(lambda: meta.process(store))
        return 'EVENT_RECEIVED',200

    @bp.get('/meta/media/<media_id>')
    def meta_media(media_id):
        row=meta.public_media(store,media_id)
        if not row: return jsonify(error='Not Found'),404
        return Response(bytes(row['data']),mimetype=row['content_type'],headers={'Content-Disposition':'inline'})

    @bp.get('/privacy')
    def privacy(): return render_template('privacy.html')

    @bp.get('/data-deletion')
    def data_deletion(): return render_template('data_deletion.html')

    @bp.get('/terms')
    def terms(): return render_template('terms.html')

    @bp.post('/api/fb/redraft')
    def fb_redraft(): return jsonify(fb.revise(store,g.staff,request.get_json(silent=True)))

    @bp.post('/api/fb/skip')
    def fb_skip(): return jsonify(fb.action(store,g.staff,request.get_json(silent=True),'skip'))

    @bp.post('/api/fb/cancel')
    def fb_cancel(): return jsonify(fb.action(store,g.staff,request.get_json(silent=True),'cancel'))

    @bp.post('/api/fb/media')
    def fb_media_upload():
        if 'file' not in request.files: raise ActionError('請選擇檔案',400)
        return jsonify(media=fb_media.upload(store,g.staff,request.form.get('thread_id'),request.files['file'])),201

    @bp.post('/api/fb/media/<media_id>/remove')
    def fb_media_remove(media_id):
        data=request.get_json(silent=True)
        fb_media.remove(store,g.staff,media_id,data.get('thread_id') if isinstance(data,dict) else None)
        return jsonify(ok=True)

    @bp.get('/api/fb/media/<media_id>')
    def fb_media_read(media_id):
        row=fb_media.read(store,media_id)
        if not row: raise ActionError('找不到附件',404)
        return Response(bytes(row['data']),mimetype=row['content_type'],headers={'Content-Disposition':'inline'})

    @bp.get('/api/fb/library')
    def fb_library(): return jsonify(cards=library.listing(store))

    @bp.get('/api/fb/library/<lib_id>')
    def fb_library_read(lib_id):
        row=library.read(store,lib_id)
        if not row: raise ActionError('找不到這張常用圖片',404)
        return Response(bytes(row['data']),mimetype=row['content_type'],headers={'Content-Disposition':'inline','Cache-Control':'private, max-age=86400'})

    @bp.post('/api/fb/media/library')
    def fb_library_attach():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): raise ActionError('請選擇圖片',400)
        return jsonify(media=library.attach(store,g.staff,data.get('thread_id'),data.get('library_id'))),201

    @bp.get('/api/fb/shot')
    def fb_shot():
        from . import fb_shots
        row=fb_shots.read(store,request.args.get('thread_id'))
        if not row: raise ActionError('找不到截圖',404)
        return Response(bytes(row['data']),mimetype=row['content_type'],headers={'Content-Disposition':'inline','Cache-Control':'private, max-age=86400'})  # the page adds the shot time to the URL

    @bp.post('/api/fb/links')
    def fb_links():
        from .links import audit
        data=request.get_json(silent=True)
        text=data.get('text') if isinstance(data,dict) else None
        if not isinstance(text,str) or len(text)>4000: raise ActionError('內容格式不正確',400)
        return jsonify(links=audit(store,text))

    @bp.post('/api/fb/rules/<int:rule_id>')
    def fb_rule(rule_id):
        require_reviewer(g.staff)
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or type(data.get('active')) is not bool:
            raise ActionError('規則狀態不正確',400)
        if not store.execute('UPDATE desk_edit_rules SET active=%s WHERE id=%s',(data['active'],rule_id)):
            raise ActionError('找不到修改指示',404)
        return jsonify(ok=True)

    @bp.get('/api/fb/knowledge/prefill')
    def fb_prefill():
        c=get_thread(request.args.get('thread_id'))
        items=fb.messages(store,c['id'])
        last=max((i for i,m in enumerate(items) if m['role']=='user'),default=-1)
        return jsonify(question=items[last]['text'][:5000] if last>=0 else '',
            answer=next((m['text'][:5000] for m in reversed(items[last+1:]) if m['role']=='assistant'),''))

    @bp.post('/api/fb/knowledge')
    def fb_knowledge():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): raise ActionError('請填寫知識表單',400)
        c=get_thread(data.get('user_id'))
        class KnowledgeStore:
            def conversation(self,uid): return c if uid==c['id'] else None
            def cursor(self): return store.cursor()
        return jsonify(submit(KnowledgeStore(),g.staff,data)),201
