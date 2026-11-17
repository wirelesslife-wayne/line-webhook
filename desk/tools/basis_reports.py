"""「回報資料有誤」 for Claude: list what Wayne reported on 「AI 回覆依據」, close one after fixing the source.
Usage: python -m desk.tools.basis_reports list | done <id>"""
import json
import sys
from .common import store
from .. import basis


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    args = list(sys.argv[1:] if argv is None else argv)
    db = store()
    if args[:1] == ['done'] and len(args) == 2:
        out = basis.set_report(db, args[1], 'done')
    else:
        out = basis.reports(db)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
