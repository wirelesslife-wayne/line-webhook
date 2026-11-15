"""LINE webhook trial run (Wayne 11/15 23:50, choice A): compare what the LINE webhook stored with what the desk got
from the browser run, without writing anything. Prints counts only (no message text, no names) unless --names.

Usage: python -m desk.tools.line_shadow [--days 3] [--names]"""
import argparse
import json
import sys
from collections import defaultdict
from .common import store


def norm(text):
    return ''.join(str(text or '').split())


def compare(db, days):
    hooks = db.all("""SELECT user_id, display_name, message_type, content, created_at FROM messages
      WHERE oa IS NULL AND created_at > NOW() - make_interval(days => %s) ORDER BY created_at""", (days,))
    threads = db.all("SELECT id, key, name FROM desk_fb_threads WHERE channel='line_message'")
    by_name = defaultdict(list)
    for t in threads:
        by_name[t['name']].append(t)
    ids_per_name = defaultdict(set)
    for h in hooks:
        ids_per_name[h['display_name']].add(h['user_id'])
    events = defaultdict(list)
    for e in db.all("""SELECT e.thread_id, e.content, e.received_at FROM desk_fb_events e JOIN desk_fb_threads t ON t.id=e.thread_id
                       WHERE t.channel='line_message' AND e.role='user'"""):
        events[e['thread_id']].append(e)
    out = dict(days=days, webhook_messages=len(hooks), webhook_customers=len({h['user_id'] for h in hooks}),
               in_desk=0, missing=0, same_name_two_people=sorted(n for n, ids in ids_per_name.items() if len(ids) > 1),
               two_desk_threads_same_name=sorted(n for n, ts in by_name.items() if len(ts) > 1),
               minutes_late=[], missing_list=[])
    for h in hooks:
        if h['message_type'] != 'text':
            continue   # pictures/stickers: the browser run writes 「[圖片]」, compare text only
        ts = by_name.get(h['display_name'], [])
        hit = None
        for t in ts:
            hit = next((e for e in events[t['id']] if norm(h['content']) and norm(h['content']) in norm(e['content'])), None)
            if hit:
                break
        if hit:
            out['in_desk'] += 1
            out['minutes_late'].append(round((hit['received_at'] - h['created_at']).total_seconds() / 60))
        else:
            out['missing'] += 1
            out['missing_list'].append(dict(name=h['display_name'], at=h['created_at'].isoformat()[:16], desk_has_customer=bool(ts)))
    late = sorted(out.pop('minutes_late'))
    out['median_minutes_late'] = late[len(late) // 2] if late else None
    out['worst_minutes_late'] = late[-1] if late else None
    return out


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days', type=int, default=3)
    p.add_argument('--names', action='store_true', help='also list who is missing (local use only)')
    a = p.parse_args(argv)
    out = compare(store(), a.days)
    if not a.names:
        out['missing_list'] = len(out['missing_list'])
        out['same_name_two_people'] = len(out['same_name_two_people'])
        out['two_desk_threads_same_name'] = len(out['two_desk_threads_same_name'])
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
