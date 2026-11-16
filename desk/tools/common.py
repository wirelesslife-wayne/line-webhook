import os
from pathlib import Path
from ..db import Store

WORKSPACE = Path.home()/'Desktop'/'Claude工作區'


def load_env(path, overwrite=False):
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key,value=line.split('=',1)
        value=value.strip().strip('"').strip("'")
        if overwrite:
            os.environ[key.strip()]=value
        else:
            os.environ.setdefault(key.strip(),value)


def ensure_schema(db):
    """Apply desk SCHEMA only when its text changed since the last time (remembered in desk_marks).
    It used to run on every tool start, i.e. every 10 minutes from the scheduled runs: each time its ALTER TABLE
    asked for an exclusive lock, and on 11/16 it queued behind a long read and froze the whole desk for a minute at a
    time (the LINE and FB runs failed, drafting stopped). When it does run, it gives up after 5 seconds instead."""
    import hashlib
    from ..db import SCHEMA
    want = hashlib.sha256(SCHEMA.encode('utf-8')).hexdigest()
    try:
        row = db.one("SELECT value FROM desk_marks WHERE name='schema'")
        if row and row['value'] == want:
            return False
    except Exception:
        pass  # a new database has no desk_marks yet
    with db.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='5s'")
        cur.execute(SCHEMA)
        cur.execute("""INSERT INTO desk_marks(name,value) VALUES('schema',%s)
          ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value,at=NOW()""", (want,))
    return True


def store():
    load_env(WORKSPACE/'secrets'/'line_webhook_db.env')
    result = Store(os.environ['DATABASE_URL'])
    ensure_schema(result)
    return result
