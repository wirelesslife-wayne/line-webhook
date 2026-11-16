import json
from datetime import timedelta
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
            cur.execute('SELECT thread_id,fetched_at FROM desk_fb_avatars WHERE thread_id=ANY(%s)',(ids,))
            faces={a['thread_id']:a['fetched_at'].strftime('%m%d%H%M') for a in cur.fetchall()}
        out=[]
        for c in rows:
            attempts=[a for a in all_attempts if a['thread_id']==c['id']][:10]
            mine=[m for m in files if m['thread_id']==c['id']]
            sent=next((a['retry_key'] for a in attempts if a['status'] in ('approved','sent')),None)
            media=[m for m in mine if m['free']] if c['status'] in fb.TODO else [m for m in mine if sent and m['attempt_key']==sent]
            timeline=fb.shape(e for e in events if e['thread_id']==c['id'])
            out.append(dict(id=c['id'],name=c['name'],channel=c['channel'],label=fb.LABELS[c['channel']],key=c['key'],url=c['url'],
                context=c['context'],status=c['status'],version=c['version'],note=c['note'],result=c['draft'] or {},
                hold=c['hold'],hold_note=c['hold_note'],hold_at=local_time(c['hold_at'])[5:16] if c['hold_at'] else '',
                hold_by=c['hold_by'],hold_iso=c['hold_at'].isoformat() if c['hold_at'] else '',
                hold_ends=local_time(c['hold_at']+timedelta(days=3))[5:10].replace('-','/').lstrip('0') if c['hold_at'] else '',
                auto=(c['draft'] or {}).get('auto','') if c['status']=='skip' and c.get('auto_at') else '',
                messages=timeline[-12:],older=max(0,len(timeline)-12),is_public=c['channel'].endswith('_comment'),
                media=[fb_media.describe(m) for m in media],shot=shots.get(c['id']),avatar=faces.get(c['id']),live=meta.live(c),
                attempts=[dict(status=a['status'],time=local_time(a['approved_at']),staff=a['username'],text=a['content'],reason=a['reason']) for a in attempts]))
        return out

    def detail(c):
        return details([c])[0]

    def listing():
        channel,tab=request.args.get('channel',''),request.args.get('tab','todo')
        if channel and channel not in fb.CHANNELS or tab not in ('todo','approved','done','hold','search'):
            raise ActionError('分類不正確',400)
        q=request.args.get('q','').strip()[:50]
        if tab=='search' and not q:
            raise ActionError('請輸入客人名字',400)
        try: page=max(0,int(request.args.get('page','0')))
        except ValueError: raise ActionError('頁碼不正確',400)
        order=request.args.get('order','old')
        if order not in ('old','new'): raise ActionError('排序不正確',400)
        late=request.args.get('late')=='1' and tab=='todo'  # Wayne 11/14: only those waiting more than 24 hours
        auto=request.args.get('auto')=='1' and tab=='done'  # Wayne 11/16: only those closed by themselves (ad buttons)
        states=list(fb.TODO) if tab=='todo' else ['approved'] if tab=='approved' else ['done','skip']
        # Wayne 11/14: the customer who has waited longest first, unless he switches to newest first
        # by when the customer last wrote (the desk may read it an hour later), not when the desk picked it up
        said="COALESCE((SELECT MAX(e.event_at) FROM desk_fb_events e WHERE e.thread_id=t.id AND e.role='user'),t.last_at)"
        where=f"t.status=ANY(%s) AND (%s='' OR t.channel=%s){f' AND {said} < NOW()-INTERVAL {chr(39)}24 hours{chr(39)}' if late else ''}"
        if auto:
            states,where=['skip'],"t.status=ANY(%s) AND (%s='' OR t.channel=%s) AND t.auto_at IS NOT NULL"
        if tab=='hold':  # Wayne 11/15: whatever still needs checking, whether or not a reply went out
            states,where=list(fb.TODO)+['approved','done','skip'],"t.hold<>'' AND t.status=ANY(%s) AND (%s='' OR t.channel=%s)"
        args=(states,channel,channel)
        if tab=='search':  # Wayne 11/15: a customer's name, in every tab and every channel
            states=list(fb.TODO)+['approved','done','skip']
            where="t.status=ANY(%s) AND (%s='' OR t.channel=%s) AND t.name ILIKE %s"
            args=(states,channel,channel,'%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        sort=f"{said} {'' if order=='old' else 'DESC'}" if tab!='done' else 't.updated_at DESC'  # Wayne 11/14: last closed first
        if tab=='hold':
            sort="CASE t.hold WHEN 'ours' THEN 0 ELSE 1 END,t.hold_at"  # ours first, longest waiting first
        if tab=='search':
            sort=f"{said} DESC"
        rows=store.all(f"""SELECT t.*,EXTRACT(EPOCH FROM NOW()-{said})/3600 AS waited,
          EXTRACT(DAY FROM NOW()-t.hold_at) AS hold_days FROM desk_fb_threads t WHERE {where}
          ORDER BY {sort},t.id LIMIT 21 OFFSET %s""",(*args,page*20))
        total=store.one(f'SELECT COUNT(*) AS n FROM desk_fb_threads t WHERE {where}',args)['n']
        overdue=manual=0
        if tab=='todo':
            overdue=store.one(f"SELECT COUNT(*) AS n FROM desk_fb_threads t WHERE t.status=ANY(%s) AND {said} < NOW()-INTERVAL '24 hours'",(states,))['n']
            manual=store.one("SELECT COUNT(*) AS n FROM desk_fb_threads WHERE status='blocked'")['n']
        counts=fb.status(store)
        per=({r['channel']:r['n'] for r in store.all('SELECT channel,COUNT(*) AS n FROM desk_fb_threads WHERE status=ANY(%s) GROUP BY channel',(states,))}
             if tab not in ('done','hold','search') else {})  # the finished tab only grows; a count there says nothing
        items=details(rows[:20])
        for item,row in zip(items,rows):
            item['waited']=round(float(row['waited'] or 0),1)  # hours since the customer last wrote
            item['closed_at']=local_time(row['updated_at'])[5:16]
            item['hold_days']=int(row['hold_days'] or 0)
        holds={r['hold']:r['n'] for r in store.all("SELECT hold,COUNT(*) AS n FROM desk_fb_threads WHERE hold<>'' GROUP BY hold")}
        autos=store.one("""SELECT COUNT(*) FILTER (WHERE auto_at >= date_trunc('day',NOW() AT TIME ZONE 'Asia/Taipei') AT TIME ZONE 'Asia/Taipei') AS today,
          COUNT(*) AS n FROM desk_fb_threads WHERE status='skip' AND auto_at IS NOT NULL""") if tab=='done' else dict(today=0,n=0)
        return dict(items=items,has_more=len(rows)>20,page=page,tab=tab,channel=channel,order=order,per_channel=per,
            total=total,overdue=overdue,manual=manual,late=late,q=q,auto=auto,auto_today=autos['today'],auto_total=autos['n'],
            hold=sum(holds.values()),hold_ours=holds.get('ours',0),hold_customer=holds.get('customer',0),  # Wayne 11/15: both kinds
            todo=counts['waiting_draft']+counts['awaiting_review']+counts['draft_failed'],approved=counts['approved_unsent'],
            sync=fb.sync_state(store))

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

    @bp.post('/api/fb/hold')
    def fb_hold(): return jsonify(fb.set_hold(store,g.staff,request.get_json(silent=True)))

    @bp.post('/api/fb/sync')
    def fb_sync(): return jsonify(fb.request_sync(store,g.staff))

    @bp.post('/api/fb/reopen')
    def fb_reopen(): return jsonify(fb.reopen(store,g.staff,request.get_json(silent=True)))

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

    @bp.get('/api/fb/avatar')
    def fb_avatar():
        from . import fb_avatars
        row=fb_avatars.read(store,request.args.get('thread_id'))
        if not row: raise ActionError('沒有大頭照',404)
        return Response(bytes(row['data']),mimetype='image/jpeg',headers={'Content-Disposition':'inline','Cache-Control':'private, max-age=604800'})  # the page adds the fetch time to the URL

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

    @bp.get('/api/fb/history')
    def fb_history():
        # Wayne 11/15 23:50 (Liang Hong: the 11/26 booking was in the 15th message from the end)
        c=get_thread(request.args.get('thread_id'))
        return jsonify(dict(messages=fb.shape(store.all('SELECT * FROM desk_fb_events WHERE thread_id=%s ORDER BY id',(c['id'],)))))

    @bp.get('/api/fb/references')
    def fb_references():
        from . import lookup
        c=get_thread(request.args.get('thread_id'))
        return jsonify(lookup.for_thread(store,fb.messages(store,c['id']),c['context']))

    @bp.get('/api/fb/lookup')
    def fb_lookup():
        from . import lookup
        kind=request.args.get('kind','history')
        if kind not in ('canned','history'): raise ActionError('分類不正確',400)
        return jsonify(items=lookup.search(store,request.args.get('q',''),kind,8))

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
