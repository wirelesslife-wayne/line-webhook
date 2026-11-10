"""Optional staff submissions; only the existing Wayne account may review."""
from psycopg2.extras import Json
from .service import ActionError
from .engine import bigrams


def can_review(staff):
    return staff['username'] == 'Wayne'


def require_reviewer(staff):
    if not can_review(staff):
        raise ActionError('只有 Wayne 可以審核知識', 403)


def fields(data):
    if not isinstance(data, dict):
        raise ActionError('請填寫知識表單', 400)
    result = {}
    for key, label, limit in [('question','客人問的',5000), ('answer','我們的回答',5000),
                              ('summary','一句話重點',300), ('scope','適用車款／產品',200)]:
        value = data.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit or (key != 'scope' and not value.strip()):
            raise ActionError(f'{label}必須填寫且不可超過 {limit} 字' if key != 'scope' else f'{label}不可超過 {limit} 字', 400)
        result[key] = value.strip()
    return result


def submit(store, staff, data):
    values = fields(data)
    uid = data.get('user_id')
    if not isinstance(uid, str) or not store.conversation(uid):
        raise ActionError('找不到對話', 404)
    with store.cursor() as cur:
        cur.execute('''INSERT INTO desk_knowledge(conversation_id,question,answer,summary,scope,submitted_by)
          VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',
          (uid, values['question'], values['answer'], values['summary'], values['scope'], staff['id']))
        kid = cur.fetchone()['id']
        cur.execute('''INSERT INTO desk_knowledge_events(knowledge_id,staff_id,action,snapshot)
          VALUES(%s,%s,'submit',%s)''', (kid, staff['id'], Json(values)))
    return {'ok':True, 'id':kid, 'status':'pending'}


def review(store, staff, kid, data):
    require_reviewer(staff)
    if not isinstance(data, dict) or type(data.get('version')) is not int:
        raise ActionError('請重新載入知識清單', 400)
    action = data.get('action')
    if action not in ('approve','reject','withdraw'):
        raise ActionError('審核操作不正確', 400)
    values = fields(data) if action == 'approve' else None
    with store.cursor() as cur:
        cur.execute('SELECT * FROM desk_knowledge WHERE id=%s FOR UPDATE', (kid,))
        row = cur.fetchone()
        if not row:
            raise ActionError('找不到知識', 404)
        expected = 'approved' if action == 'withdraw' else 'pending'
        if row['status'] != expected or row['version'] != data['version']:
            raise ActionError('這筆知識已更新，請重新載入', 409)
        values = values or {k:row[k] for k in ('question','answer','summary','scope')}
        status = {'approve':'approved','reject':'rejected','withdraw':'withdrawn'}[action]
        cur.execute('''UPDATE desk_knowledge SET question=%s,answer=%s,summary=%s,scope=%s,status=%s,
          reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW(),version=version+1 WHERE id=%s''',
          (values['question'], values['answer'], values['summary'], values['scope'], status, staff['id'], kid))
        cur.execute('''INSERT INTO desk_knowledge_events(knowledge_id,staff_id,action,snapshot)
          VALUES(%s,%s,%s,%s)''', (kid, staff['id'], action, Json(values)))
    return {'ok':True, 'status':status}


def reference(store, text):
    # Read current status for every draft; pending/rejected/withdrawn never enter prompts.
    rows = store.all("SELECT id,question,answer,summary,scope FROM desk_knowledge WHERE status='approved' ORDER BY id")
    query = bigrams(text.casefold())
    def score(row):
        return len(query & bigrams((row['question']+' '+row['summary']+' '+row['scope']).casefold()))
    ranked = sorted(rows, key=lambda row: (-score(row), row['id']))
    lines = [r['summary'] + ('（'+r['scope']+'）' if r['scope'] else '') for r in rows]
    if len('\n'.join(lines)) > 3000:
        lines = [r['summary'] + ('（'+r['scope']+'）' if r['scope'] else '') for r in ranked]
    summaries, used = [], 0
    for line in lines:
        cost = len(line) + bool(summaries)
        if used + cost <= 3000:
            summaries.append(line)
            used += cost
    return {'重點':'\n'.join(summaries), '問答':[
        {key:r[key] for key in ('question','answer','scope')} for r in ranked if score(r) > 0][:3]}
