"""Load the knowledge consolidation's conflicts into 知識庫衝突題 (Wayne 11/18).
Usage: python -m desk.tools.load_kb_conflicts <pass2.jsonl>
Run again after a rebuild: a topic already loaded (same group and topic) keeps Wayne's answer and is not added twice.
The most-asked groups come first."""
import hashlib
import json
import sys
from psycopg2.extras import Json
from .common import store, ensure_schema


def rows(path):
    groups = [json.loads(line) for line in open(path, encoding='utf-8') if line.strip()]
    groups.sort(key=lambda g: -g.get('n', 0))
    rank = 0
    for g in groups:
        for c in g.get('conflicts') or []:
            opts = [o for o in c.get('options') or [] if isinstance(o, dict) and o.get('say')]
            if len(opts) < 2 or not c.get('topic'):
                continue
            rank += 1
            digest = hashlib.sha256(f"{g['group']}|{c['topic']}".encode('utf-8')).hexdigest()
            yield digest, g['group'], c['topic'][:200], opts, (c.get('suggest') or '')[:500], rank


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__)
        return 2
    db = store()
    ensure_schema(db)
    added = 0
    for digest, grp, topic, opts, suggest, rank in rows(args[0]):
        added += db.execute("""INSERT INTO desk_kb_conflicts(digest,grp,topic,options,suggest,rank)
          VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (digest) DO NOTHING""", (digest, grp, topic, Json(opts), suggest, rank))
    print(json.dumps(dict(added=added, **dict(db.one("SELECT COUNT(*) AS total FROM desk_kb_conflicts"))), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
