"""UTF-8 JSON boundary for Claude browser operations; never delivers messages."""
import argparse
import json
from pathlib import Path
import sys
from .common import store, WORKSPACE
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
    sub.add_parser('faces').add_argument('--line',action='store_true',help='only the official LINE account')
    sub.add_parser('pending').add_argument('--line',action='store_true',help='only the official LINE account')
    sub.add_parser('open').add_argument('--line',action='store_true',help='only the official LINE account')
    sub.add_parser('unstar',help='FB/IG conversations Wayne marked 不發送: remove their 持續追蹤 star')
    sub.add_parser('unstarred').add_argument('thread_id')
    sub.add_parser('recheck',help='closed by itself in the last 7 days (only ad buttons): compare the last line in Meta')
    sub.add_parser('pics',help='photos LINE customers sent that the desk still lacks, with a 40-minute upload ticket (Wayne 11/17)')
    for name in ('lock','unlock'):  # one round at a time per inbox (scheduled rounds and WL-DeskSync)
        p=sub.add_parser(name)
        p.add_argument('what',choices=('line','fb'))
        p.add_argument('owner')
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
        if args.command=='push':
            from .. import meta
            skipped=0
            if meta.stored_mode(db)=='on':
                # Private messages now come through the Meta webhook; a browser copy would be a second conversation.
                kept=[r for r in data if not (isinstance(r,dict) and r.get('channel') in ('fb_message','ig_message'))]
                skipped,data=len(data)-len(kept),kept
            out=fb.push(db,data)
            out['skipped_private']=skipped
            from .. import fb_shots
            saved=0
            for row in fb.validate(data):  # optional "shot": local screenshot of the conversation column
                path=row.get('shot')
                if isinstance(path,str) and path:
                    try: saved+=fb_shots.save(db,row['id'],Path(path).read_bytes())
                    except OSError: pass
            out['screenshots']=saved
            from .. import fb_avatars
            out['avatars']=fb_avatars.collect(db,fb.validate(data))  # optional "avatar": picture address or saved file
        elif args.command=='faces':
            from .. import fb_avatars
            from .. import fb_pics
            out=dict(have_picture=fb_avatars.have(db,args.line))
            if not args.line: out['ticket']=fb_pics.ticket(db)  # Wayne 11/17: the Meta page uploads pictures itself
        elif args.command=='status':
            from .. import meta
            out=fb.status(db)
            out['meta_api']=meta.stored_mode(db)  # 'on': read and send only comments in the browser
            out['api_new_last_hour']=db.one("""SELECT COUNT(*) AS n FROM desk_fb_threads WHERE (key LIKE 'psid:%%' OR key LIKE 'igsid:%%')
              AND status=ANY(%s) AND last_at > NOW()-INTERVAL '65 minutes'""",(list(fb.TODO),))['n']
        elif args.command=='pending':
            from .. import fb_media
            out=fb.pending(db,args.line)
            for item in out:  # the browser attaches these local files together with the text
                item['files']=fb_media.export(db,item['attempt_id'],WORKSPACE/'AI客服訓練'/'fb_media')
        elif args.command=='open': out=fb.awaiting(db,args.line)
        elif args.command=='unstar': out=fb.to_unstar(db)
        elif args.command=='unstarred': out=fb.unstarred(db,args.thread_id)
        elif args.command=='recheck': out=fb.recheck(db)
        elif args.command=='pics':
            from .. import fb_pics
            from .. import fb_vids
            fb_pics.cleanup(db);fb_vids.cleanup(db)
            todo=fb_pics.todo(db)+fb_vids.todo(db)
            out=dict(todo=todo,ticket=fb_pics.ticket(db) if todo else '')
        elif args.command=='lock': out=fb.run_lock(db,args.what,args.owner)
        elif args.command=='unlock': out=fb.run_unlock(db,args.what,args.owner)
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
