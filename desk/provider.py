"""Approved Responses API adapter with persistent, conservative monthly accounting.

No credentials, customer identifiers, prompts or responses are saved in the ledger.
An outstanding reservation survives crashes/timeouts and is never retried silently.
"""
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import time
import uuid
import requests
from .db import now
from .engine import ProviderPending
from .privacy import redact

TAIPEI = timezone(timedelta(hours=8))
MODEL = 'gpt-6.1-sol'
# Official standard, short-context USD / 1M tokens, checked 2026-11-09:
# https://developers.openai.com/api/docs/models/gpt-6.1-sol
RATES = (Decimal('2'), Decimal('0.10'), Decimal('10'))
MAX_OUTPUT = 3000
MAX_INPUT_BYTES = 80000
EXHAUSTED = '本月 AI 額度已用完，請手動回覆'


class BudgetExceeded(ProviderPending):
    """Eligible for automatic retry when the Taipei billing month changes."""


class Unavailable(ProviderPending):
    """The API refused the request before doing any work (e.g. the account is out of credit); retried later."""


class Busy(Unavailable):
    """Another draft holds the budget lock; nothing was sent."""


def month_key(at=None):
    return (at or now()).astimezone(TAIPEI).strftime('%Y-%m')


def monthly_limit():
    try:
        value = Decimal(os.environ.get('DESK_MONTHLY_USD', '30'))
        if not value.is_finite() or value < 0:
            raise ValueError()
        return value
    except (InvalidOperation, ValueError):
        raise ProviderPending('AI 月額度設定錯誤，請手動回覆') from None


def model_rates(model):
    if model == MODEL:
        return RATES
    # Never guess another model's price when DESK_MODEL is overridden.
    raise ProviderPending('AI 模型尚未設定官方計價，請手動回覆')


def usage_cost(usage, rates=RATES):
    def count(value):
        if type(value) is not int or value < 0:
            raise ValueError('invalid usage')
        return value
    incoming = count(usage['input_tokens'])
    outgoing = count(usage['output_tokens'])
    cached = count((usage.get('input_tokens_details') or {}).get('cached_tokens', 0))
    if cached > incoming:
        raise ValueError('invalid cached usage')
    # Output tokens include reasoning tokens; do not charge them twice.
    cost = ((incoming-cached)*rates[0]+cached*rates[1]+outgoing*rates[2])/1000000
    return cost, incoming, cached, outgoing


def budget_status(store, at=None):
    month = month_key(at)
    row = store.one('''SELECT COALESCE(SUM(usd),0) AS usd,
      COALESCE(SUM(usd) FILTER (WHERE status <> 'settled'),0) AS reserved
      FROM desk_ai_usage WHERE month=%s''', (month,))
    spent = Decimal(row['usd'])
    cap = monthly_limit()
    blocked = spent >= cap
    warning = EXHAUSTED if blocked else (
        f'本月 AI 額度已使用 {spent/cap:.0%}，接近上限；仍可手動回覆' if spent >= cap*Decimal('.8') else '')
    return {'month':month, 'usd':str(spent), 'reserved_usd':str(row['reserved']),
            'limit_usd':str(cap), 'blocked':blocked, 'warning':warning}


class OpenAIProvider:
    def __init__(self, store, post=None):
        self.store = store
        self.post = post or requests.post
        self.public = ()  # Our own published shop addresses/phones; the engine fills this in.
        self.models = frozenset()  # Car model codes, so "CC 2026" is not masked as a plate; the engine fills this in.
        self.patience, self.pause = 90, 2  # Wait up to 3 minutes for another draft to finish.

    def __call__(self, messages):
        # Only background workers call this. The LINE and FB drafters share one budget lock,
        # so with a queue on both sides one of them is always busy: wait for a turn instead of giving up.
        for turn in range(self.patience):
            try:
                return self.once(messages)
            except Busy:
                if turn == self.patience-1:
                    raise
                time.sleep(self.pause)

    def once(self, messages):
        key = os.environ.get('OPENAI_API_KEY', '')
        if not key:
            raise ProviderPending('AI 擬稿尚未設定金鑰，請手動回覆')
        model = os.environ.get('DESK_MODEL', MODEL)
        rates = model_rates(model)
        # Rebuild from an allowlist; no metadata, IDs, names or media objects pass.
        inputs = [{'role':m['role'], 'content':redact(m['content'],self.public,self.models)}
                  for m in messages if m.get('role') in ('system','user','assistant')]
        if not inputs or any(not isinstance(m.get('content'),str) for m in messages):
            raise ProviderPending('AI 只接受文字，請手動回覆')
        size = len(json.dumps(inputs, ensure_ascii=False).encode('utf-8'))
        if size > MAX_INPUT_BYTES:
            raise ProviderPending('本次對話或知識資料過長，請手動回覆')
        # UTF-8 byte bound + framing allowance is deliberately conservative;
        # count all input at the higher cache-write rate to cover cache overhead.
        reserved = ((size+4096)*Decimal('2.50')+MAX_OUTPUT*rates[2])/1000000
        with self.store.lock('desk-ai-budget') as acquired:
            if not acquired:
                raise Busy('AI 正在處理其他擬稿，稍後會自動重新擬稿')
            month = month_key()
            status = budget_status(self.store)
            if status['blocked']:
                raise BudgetExceeded(EXHAUSTED)
            if Decimal(status['usd'])+reserved > monthly_limit():
                raise BudgetExceeded('本月 AI 剩餘額度不足以安全擬稿，請手動回覆')
            attempt = str(uuid.uuid4())
            self.store.execute('''INSERT INTO desk_ai_usage(id,month,model,usd,status)
              VALUES(%s,%s,%s,%s,'reserved')''', (attempt,month,model,reserved))
            payload = {'model':model,'input':inputs,'store':False,'service_tier':'default',
                       'max_output_tokens':MAX_OUTPUT,'reasoning':{'effort':'low'},
                       'text':{'format':{'type':'json_object'}}}
            refused = False
            try:
                response = self.post('https://api.openai.com/v1/responses',
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
                    json=payload, timeout=(10,60), allow_redirects=False)
                if 400 <= response.status_code < 500:
                    # Refused before any work was done, so nothing is billable: do not hold budget for it.
                    self.store.execute('''UPDATE desk_ai_usage SET usd=%s,input_tokens=%s,
                      cached_tokens=%s,output_tokens=%s,status='settled' WHERE id=%s''', (0,0,0,0,attempt))
                    refused = True
                if response.status_code != 200:
                    raise ValueError('api failure')
                body = response.json()
                cost, incoming, cached, outgoing = usage_cost(body['usage'],rates)
                # Usage is accounted even if JSON content is invalid or incomplete.
                self.store.execute('''UPDATE desk_ai_usage SET usd=%s,input_tokens=%s,
                  cached_tokens=%s,output_tokens=%s,status='settled' WHERE id=%s''',
                  (cost,incoming,cached,outgoing,attempt))
            except Exception:
                # Keep reserved funds if billable usage is unknown; expose no body.
                if refused:
                    raise Unavailable('AI 服務拒絕了這次請求（可能是金鑰失效或餘額不足），恢復後會自動重新擬稿') from None
                raise ProviderPending('AI 擬稿失敗，用量待核對，請手動回覆') from None
            try:
                if body.get('status') != 'completed':
                    raise ValueError()
                content = ''.join(part['text'] for item in body.get('output',[])
                    if item.get('type') == 'message' for part in item.get('content',[])
                    if part.get('type') == 'output_text')
                result = json.loads(content)
                if not isinstance(result,dict):
                    raise ValueError()
                return result
            except (ValueError, KeyError, TypeError):
                raise ProviderPending('AI 回覆格式不符，請手動回覆') from None
