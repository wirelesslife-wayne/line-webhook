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


def store():
    load_env(WORKSPACE/'secrets'/'line_webhook_db.env')
    result = Store(os.environ['DATABASE_URL'])
    result.initialize()
    return result
