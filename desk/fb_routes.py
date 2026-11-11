from flask import request, jsonify, g, render_template
from . import fb
from .errors import ActionError
from .knowledge import can_review, require_reviewer, submit
from .worker import local_time


def register(bp,store):
    def get_thread(tid):
        c=fb.conversation(store,tid) if isinstance(tid,str) else None
        if not c:
            raise ActionError('找不到對話',404)
        return c

    def detail(c):
        attempts=store.all("""SELECT a.*,u.username FROM desk_fb_attempts a JOIN desk_users u ON u.id=a.staff_id
          WHERE thread_id=%s ORDER BY approved_at DESC LIMIT 10""",(c['id'],))
        return dict(id=c['id'],name=c['name'],channel=c['channel'],label=fb.LABELS[c['channel']],key=c['key'],url=c['url'],
            context=c['context'],status=c['status'],version=c['version'],note=c['note'],result=c['draft'] or {},
            messages=fb.messages(store,c['id'])[-12:],is_public=c['channel'].endswith('_comment'),
            attempts=[dict(status=a['status'],time=local_time(a['approved_at']),staff=a['username'],text=a['content'],reason=a['reason']) for a in attempts])

    def listing():
        channel,tab=request.args.get('channel',''),request.args.get('tab','todo')
        if channel and channel not in fb.CHANNELS or tab not in ('todo','approved','done'):
            raise ActionError('分類不正確',400)
        try: page=max(0,int(request.args.get('page','0')))
        except ValueError: raise ActionError('頁碼不正確',400)
        states=list(fb.TODO) if tab=='todo' else ['approved'] if tab=='approved' else ['done','skip']
        rows=store.all("""SELECT * FROM desk_fb_threads WHERE status=ANY(%s) AND (%s='' OR channel=%s)
          ORDER BY last_at,id LIMIT 21 OFFSET %s""",(states,channel,channel,page*20))
        counts=fb.status(store)
        return dict(items=[detail(c) for c in rows[:20]],has_more=len(rows)>20,page=page,tab=tab,channel=channel,
            todo=counts['waiting_draft']+counts['awaiting_review'],approved=counts['approved_unsent'])

    @bp.get('/fb')
    def fb_page():
        return render_template('fb.html',staff=g.staff,can_review=can_review(g.staff),labels=fb.LABELS,view=listing(),
            rules=[dict(id=r['id'],instruction=r['instruction'],active=r['active'],label=fb.LABELS.get(r['channel'],r['channel']),
                        time=local_time(r['created_at'])[5:16],said=r['conversation_summary'][:60])
                   for r in store.all('SELECT * FROM desk_edit_rules ORDER BY id DESC LIMIT 200')] if can_review(g.staff) else [])

    @bp.get('/api/fb/list')
    def fb_list(): return jsonify(listing())

    @bp.get('/api/fb/conversation')
    def fb_detail(): return jsonify(detail(get_thread(request.args.get('thread_id'))))

    @bp.post('/api/fb/send')
    def fb_send(): return jsonify(fb.approve(store,g.staff,request.get_json(silent=True)))

    @bp.post('/api/fb/redraft')
    def fb_redraft(): return jsonify(fb.revise(store,g.staff,request.get_json(silent=True)))

    @bp.post('/api/fb/skip')
    def fb_skip(): return jsonify(fb.action(store,g.staff,request.get_json(silent=True),'skip'))

    @bp.post('/api/fb/cancel')
    def fb_cancel(): return jsonify(fb.action(store,g.staff,request.get_json(silent=True),'cancel'))

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
