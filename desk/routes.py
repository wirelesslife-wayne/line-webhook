import hmac
import os
import secrets
from flask import Blueprint,request,g,jsonify,redirect,render_template,make_response
from .auth import Auth,COOKIE
from .db import now
from .engine import Engine
from .notifier import LineNotifier
from .service import send,mark_done,redraft,ActionError
from .worker import local_time,utc_time,timeline
from werkzeug.wsgi import get_input_stream


def make_blueprint(store,notifier=None):
    bp = Blueprint('desk',__name__,template_folder='templates')
    auth = Auth(store,os.environ['DESK_SECRET_KEY'])
    notifier = notifier or LineNotifier()

    @bp.before_request
    def protect():
        g.nonce = secrets.token_urlsafe(18)
        # Flask 3.0 has no per-request max_content_length setter.
        request.stream = get_input_stream(request.environ,max_content_length=32768)
        if request.endpoint == 'desk.login':
            return
        try:
            g.staff = auth.session(request.cookies.get(COOKIE))
        except Exception:
            return jsonify(error='客服後台暫時無法連線，請稍後再試'),503
        if not g.staff:
            return (jsonify(error='請先登入'),401) if '/api/' in request.path else redirect('/desk/login')
        if request.method != 'GET':
            body = request.get_json(silent=True)
            body = body if isinstance(body,dict) else request.form
            token = request.headers.get('X-CSRF-Token') or body.get('csrf','')
            if not isinstance(token,str) or not hmac.compare_digest(token,g.staff['csrf']):
                return jsonify(error='驗證已失效，請重新載入頁面'),403

    @bp.after_request
    def security(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']=("default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "img-src 'self' https:; style-src 'nonce-"+g.nonce+"'; script-src 'nonce-"+g.nonce+"'; connect-src 'self'")
        return response

    @bp.errorhandler(ActionError)
    def action_error(e):
        return jsonify(error=str(e)),e.status

    @bp.errorhandler(Exception)
    def unexpected(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e,HTTPException):
            return jsonify(error=e.name),e.code
        return jsonify(error='客服後台暫時無法處理；草稿請先保留，再重新載入'),503

    @bp.route('/login',methods=['GET','POST'])
    def login():
        error = None
        if request.method=='POST':
            if not auth.check_preauth(request.cookies.get('desk_login'),request.form.get('csrf','')):
                return jsonify(error='登入驗證失效，請重新開啟登入頁'),403
            cookie = auth.login(request.form.get('username','')[:80],request.form.get('password','')[:256])
            if cookie:
                response = redirect('/desk/')
                response.set_cookie(COOKIE,cookie,max_age=43200,secure=True,httponly=True,samesite='Lax',path='/desk')
                response.delete_cookie('desk_login',path='/desk')
                return response
            error = '帳號或密碼不符；連續失敗 5 次會鎖定 15 分鐘'
        csrf = auth.preauth()
        response = make_response(render_template('login.html',csrf=csrf,error=error),401 if error else 200)
        response.set_cookie('desk_login',csrf,max_age=900,secure=True,httponly=True,samesite='Lax',path='/desk')
        return response

    @bp.post('/logout')
    def logout():
        store.execute('DELETE FROM desk_sessions WHERE sid=%s',(g.staff['sid'],))
        response = redirect('/desk/login')
        response.delete_cookie(COOKIE,path='/desk')
        return response

    @bp.route('')
    @bp.route('/')
    def index():
        return render_template('page.html',staff=g.staff)

    @bp.get('/api/list')
    def listing():
        tab = request.args.get('tab','todo')
        statuses = {'todo':['waiting','drafting','ready','blocked','error'],'done':['done'],'skip':['skip']}.get(tab)
        if not statuses:
            raise ActionError('分類不正確',400)
        page = max(0,int(request.args.get('page',0)))
        rows = store.all('SELECT * FROM desk_conversations WHERE status=ANY(%s) ORDER BY first_at,user_id LIMIT 100 OFFSET %s',(statuses,page*100))
        items = []
        for c in rows:
            r = c['draft'] or {}
            items.append({'user_id':c['user_id'],'name':c['display_name'],'status':c['status'],
              'first':local_time(c['first_at']),'last':local_time(c['last_at']),
              'minutes':max(0,int((now()-c['first_at']).total_seconds()/60)),
              'source_upto':c['last_source_id'],'drafted_upto':c['drafted_upto'],
              'urgent':bool(r.get('urgent')),'scenario':r.get('scenario','')})
        price = store.meta('price_success')
        heartbeat = store.meta('reader_heartbeat')
        age = int((now()-utc_time(price)).total_seconds()/86400) if price else None
        stale = age is None or age>=1 or store.meta('price_error',False)
        from .provider import budget_status
        try:
            ai_budget = budget_status(store)
        except Exception:
            ai_budget = {'warning':'AI 用量暫時無法讀取，請手動回覆','blocked':True}
        return jsonify(items=items,page=page,has_more=len(items)==100,
            ai_budget=ai_budget,
            price_warning=('價格資料尚未完成官網更新' if age is None else f'價格資料 {age} 天沒更新') if stale else '',
            worker_warning='收訊背景處理尚未啟用或已超過 90 秒未更新' if not heartbeat or (now()-utc_time(heartbeat)).total_seconds()>90 else '')

    @bp.get('/api/conversation')
    def detail():
        c = store.conversation(request.args.get('user_id',''))
        if not c:
            raise ActionError('找不到對話',404)
        try:
            images = Engine(store).attachments(' '.join(r['content'] or '' for r in store.incoming(c['user_id'],c['handled_upto'])),
                (c['draft'] or {}).get('extract') if c['drafted_upto']==c['last_source_id'] else None)
        except ValueError:
            images = []
        pending = store.one("""SELECT a.retry_key,a.content,a.images,a.source_upto,a.staff_id,u.username,a.created_at
          FROM desk_send_attempts a JOIN desk_users u ON u.id=a.staff_id
          WHERE a.user_id=%s AND a.status IN ('sending','unknown') ORDER BY a.created_at DESC LIMIT 1""",(c['user_id'],))
        latest = max(c['last_source_id'],store.latest_source(c['user_id']))
        stale = bool(c['draft']) and (c['drafted_upto'] or 0) < latest
        return jsonify(user_id=c['user_id'],name=c['display_name'],status=c['status'],source_upto=c['last_source_id'],
            result=c['draft'] or {},stale_draft=stale,latest_source=latest,messages=timeline(store,c),images=images,
            pending={'retry_key':str(pending['retry_key']),'text':pending['content'],'images':pending['images'],
                'source_upto':pending['source_upto'],'staff':pending['username'],
                'can_retry':pending['staff_id']==g.staff['id'] and (now()-pending['created_at']).total_seconds()<23*3600} if pending else None)

    @bp.post('/api/send')
    def do_send():
        data = request.get_json(silent=True)
        if not isinstance(data,dict) or data.get('confirmed') is not True:
            raise ActionError('請先確認收件人及內容',400)
        return jsonify(send(store,notifier,g.staff,data))

    @bp.post('/api/action')
    def action():
        data = request.get_json(silent=True) or {}
        uid = data.get('user_id','')
        if not isinstance(uid,str) or not store.conversation(uid):
            raise ActionError('找不到對話',404)
        if data.get('action')=='done':
            mark_done(store,g.staff,uid,data.get('source_upto'))
        elif data.get('action')=='redraft':
            redraft(store,g.staff,uid,data.get('source_upto'))
        else:
            raise ActionError('操作不正確',400)
        return jsonify(ok=True)

    return bp
