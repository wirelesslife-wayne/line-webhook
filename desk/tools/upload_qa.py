"""Idempotent, redacted historical import. Source SQLite is opened read-only."""
from datetime import datetime
import json
import re
import sqlite3
from psycopg2.extras import execute_values
from .common import WORKSPACE,store
from ..worker import TAIPEI


def redactor(names):
    known=sorted({n.strip() for n in names if n and len(n.strip())>=2},key=len,reverse=True)
    name_re=re.compile('|'.join(re.escape(n) for n in known)) if known else None
    def redact(text):
        text=str(text or '')
        if name_re:
            text=name_re.sub('[姓名]',text)
        text=re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b','[Email]',text)
        text=re.sub(r'(?<!\d)(?:(?:\+?886[ -]?)?0?9[\d -]{8,12}|0[2-8][\d ()-]{7,12})(?!\d)','[電話]',text)
        text=re.sub(r'(?i)(?<![A-Z0-9])(?:[A-Z]{2,3}[- ]\d{3,4}|\d{3,4}[- ][A-Z]{2,3})(?![A-Z0-9])','[車牌]',text)
        text=re.sub(r'(?i)\bU[0-9a-f]{32}\b','[LINE識別碼]',text)
        text=re.sub(r'(姓名|名字|聯絡人|稱呼)\s*[:：]\s*[^\s，。；,;]{1,30}',r'\1：[姓名]',text)
        return text
    return redact


def run():
    path=WORKSPACE/'customer-service-knowledge'/'output'/'line_qa.db'
    con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        names=[r[0] for r in con.execute('SELECT DISTINCT customer_name FROM customer_qa')]
        raw=con.execute("SELECT id,time_24h,question,answer FROM customer_qa WHERE time_24h >= '2026-01-01' ORDER BY id").fetchall()
    finally:
        con.close()
    redact=redactor(names)
    # Fail before writing any QA if the date format cannot be understood.
    rows=[(r[0],datetime.fromisoformat(r[1]).replace(tzinfo=TAIPEI),redact(r[2]),redact(r[3])) for r in raw]
    db=store()
    with db.cursor() as cur:
        execute_values(cur,'''INSERT INTO desk_qa(source_id,asked_at,question,answer) VALUES %s
          ON CONFLICT(source_id) DO UPDATE SET asked_at=EXCLUDED.asked_at,question=EXCLUDED.question,answer=EXCLUDED.answer''',rows,page_size=500)
        ids=[r[0] for r in rows]
        cur.execute('SELECT COUNT(*) AS n FROM desk_qa WHERE source_id=ANY(%s)',(ids,))
        count=cur.fetchone()['n']
        if count!=len(rows):
            raise RuntimeError('歷史問答筆數未對齊')
    print(json.dumps({'ok':True,'source_rows':len(raw),'verified_rows':count,'redacted':True}))


if __name__=='__main__':
    run()
