"""UTF-8 JSON boundary for Claude browser operations; never delivers messages."""
import argparse
import json
from pathlib import Path
import sys
from .common import store
from .. import fb
from ..errors import ActionError


def main(argv=None, store_factory=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='strict')
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('push').add_argument('file')
    sub.add_parser('status')
    sub.add_parser('pending')
    sub.add_parser('open')
    result=sub.add_parser('result')
    result.add_argument('attempt_id')
    result.add_argument('outcome',choices=('sent','failed'))
    result.add_argument('reason',nargs='?',default='')
    args=parser.parse_args(argv)
    try:
        data=None
        if args.command=='push':
            data=json.loads(Path(args.file).read_text(encoding='utf-8'))
            fb.validate(data)
        db=(store_factory or store)()
        if args.command=='push': out=fb.push(db,data)
        elif args.command=='status': out=fb.status(db)
        elif args.command=='pending': out=fb.pending(db)
        elif args.command=='open': out=fb.awaiting(db)
        else: out=fb.record_result(db,args.attempt_id,args.outcome,args.reason)
        print(json.dumps(out,ensure_ascii=False))
        return 0
    except ActionError as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False))
        return 1
    except (OSError,ValueError):
        print(json.dumps({'error':'輸入檔案或 JSON 格式不正確'},ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({'error':'資料庫操作失敗，請由管理者查核連線與資料表'},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
