"""Background reader and draft worker; database locks coordinate gunicorn workers."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import threading
import time
import requests
from psycopg2.extras import Json
from .db import now
from .engine import Engine, ProviderPending, is_builtin, image_url

TAIPEI = timezone(timedelta(hours=8))


def utc_time(value):
    d = datetime.fromisoformat(value)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def local_time(value):
    d = utc_time(value) if isinstance(value, str) else value
    return d.astimezone(TAIPEI).strftime('%Y-%m-%d %H:%M:%S')


def due(conversation, current=None):
    return conversation['status'] == 'waiting' and ((current or now())-conversation['last_at']).total_seconds() >= 60


def builtin_only(rows):
    return bool(rows) and all(is_builtin(r.get('content')) or r.get('message_type') == 'sticker' for r in rows)


def timeline(store, convo, for_ai=False, current=None):
    uid = convo['user_id']
    rows = [{'time': local_time(r['time_str']), 'sort':utc_time(r['time_str']),
             'role':'user','text':(r['content'] or '') if not for_ai or r['message_type']=='text'
               else '['+({'image':'image','video':'video'}.get(r['message_type'],'attachment'))+']',
             'new':r['id']>convo['handled_upto']}
            for r in store.incoming(uid)]
    rows += [{'time':local_time(r['sent_at']),'sort':r['sent_at'],'role':'assistant',
              'text':r['content'],'images':r['images'],'new':False}
             for r in store.all('SELECT content,images,sent_at FROM desk_outgoing WHERE user_id=%s ORDER BY sent_at',(uid,))]
    rows.sort(key=lambda r:r['sort'])
    if for_ai:
        end = current or now()
        rows = [r for r in rows if end-timedelta(hours=24) <= r['sort'] <= end]
    for r in rows:
        r.pop('sort')
        if for_ai:
            if r.pop('images',None):
                r['text'] += ' [image]'
    return rows


class PriceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.payload = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'wlf-data':
            self.active = True
        if tag == 'img':
            for key in ('src','data-src'):
                u = attrs.get(key,'')
                if image_url(u):
                    self.images.append(u)

    def handle_data(self, data):
        if self.active:
            self.payload.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False


def validate_prices(data):
    if not isinstance(data, dict) or not data.get('cars') or not data.get('plans'):
        raise ValueError('價格資料格式不完整')
    for car in data['cars']:
        if not all(k in car for k in ('b','model','year','p')):
            raise ValueError('車型方案未對齊')
        try:
            data['plans'][car['p']]
        except (KeyError,IndexError,TypeError):
            raise ValueError('車型方案未對齊')
    plans = data['plans'].values() if isinstance(data['plans'],dict) else data['plans']
    for plan in plans:
        if plan == []:  # Official catalog uses an empty list for a no-offer plan.
            continue
        if not isinstance(plan,dict):
            raise ValueError('方案格式不完整')
        for entries in plan.values():
            for item in entries:
                if not isinstance(item.get('price'), (int,float)) or item['price'] < 0 or not item.get('code'):
                    raise ValueError('價格欄位錯誤')


def refresh_prices(store, fetch=requests.get):
    with store.lock('desk-prices') as acquired:
        if not acquired:
            return
        last = store.meta('price_attempt')
        if last and (now()-utc_time(last)).total_seconds()<86400:
            return
        store.set_meta('price_attempt',now().isoformat())
        try:
            response = fetch('https://carandroid.com.tw/branding/', timeout=20)
            response.raise_for_status()
            parser = PriceParser()
            parser.feed(response.content.decode('utf-8'))
            data = json.loads(''.join(parser.payload))
            validate_prices(data)
            store.put_kb('wlf-data.json',data)
            store.set_meta('website_images',list(dict.fromkeys(parser.images)))
            store.set_meta('price_success',now().isoformat())
            store.set_meta('price_error',False)
        except Exception:
            # Preserve last good KB; never log page content or credentials.
            store.set_meta('price_error',True)


def draft_once(store, engine_factory=Engine):
    from .provider import BudgetExceeded, month_key
    with store.lock('desk-drafting') as acquired:
        if not acquired:
            return
        store.execute("""UPDATE desk_conversations SET status='waiting',updated_at=NOW()
          WHERE status='blocked' AND draft->>'budget_month' IS NOT NULL
          AND draft->>'budget_month' <> %s""", (month_key(),))
        for c in store.all("SELECT * FROM desk_conversations WHERE status='waiting' ORDER BY first_at"):
            if not due(c):
                continue
            uid, upto = c['user_id'],c['last_source_id']
            if store.latest_source(uid) != upto:
                continue
            pending = store.incoming(uid,c['handled_upto'])
            if builtin_only(pending):
                store.execute("UPDATE desk_conversations SET status='skip',draft=NULL WHERE user_id=%s AND last_source_id=%s AND status='waiting'",(uid,upto))
                continue
            store.execute("UPDATE desk_conversations SET status='drafting',updated_at=NOW() WHERE user_id=%s AND last_source_id=%s AND status='waiting'",(uid,upto))
            try:
                result = engine_factory(store).draft(timeline(store,c,for_ai=True), now().astimezone(TAIPEI))
                state = 'ready'
            except BudgetExceeded as e:
                result, state = {'draft':'','staff_note':str(e),'budget_month':month_key()},'blocked'
            except ProviderPending as e:
                result, state = {'draft':'','staff_note':str(e)},'blocked'
            except Exception:
                result,state = {'draft':'','staff_note':'擬稿失敗，請稍後重新擬稿或自行填寫'},'error'
            # Validate directly against source, even before polling ingests new rows.
            if store.latest_source(uid) != upto:
                store.execute("UPDATE desk_conversations SET status='waiting' WHERE user_id=%s AND status='drafting'",(uid,))
                continue
            store.execute('''UPDATE desk_conversations SET status=%s,draft=%s,drafted_upto=%s,updated_at=NOW()
              WHERE user_id=%s AND last_source_id=%s AND status='drafting' ''',(state,Json(result),upto,uid,upto))


def start_worker(store):
    def reader():
        while True:
            try:
                store.ingest()
                refresh_prices(store)
                store.set_meta('reader_heartbeat',now().isoformat())
            except Exception:
                pass  # Isolated from webhook, UI reports stale heartbeat.
            time.sleep(30)

    def drafter():
        # Lock protects recovery from other live workers currently drafting.
        while True:
            try:
                with store.lock('desk-drafting') as acquired:
                    if acquired:
                        store.execute("UPDATE desk_conversations SET status='waiting' WHERE status='drafting'")
                draft_once(store)
                store.set_meta('draft_heartbeat',now().isoformat())
            except Exception:
                pass
            time.sleep(5)
    for fn in (reader,drafter):
        threading.Thread(target=fn,daemon=True,name='cs-desk-'+fn.__name__).start()
