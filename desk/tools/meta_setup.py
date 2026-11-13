"""Meta App setup helper for the FB/IG real-time path. Reads secrets\\fb_cs_desk.env; never prints a secret.

  check      read-only: which settings exist (names only), whether the Page token works for the Page,
             the linked Instagram account, and which webhook fields the Page is subscribed to.
  subscribe  subscribe the Page (and its linked Instagram account) to this app's webhook fields.
             Do this only after the webhook callback URL is saved in the App Dashboard.
"""
import argparse
import json
import os
import sys
import requests
from .common import load_env, WORKSPACE

NAMES = ('FB_APP_ID', 'FB_APP_SECRET', 'FB_PAGE_ID', 'FB_PAGE_ACCESS_TOKEN', 'FB_VERIFY_TOKEN')
FIELDS = 'messages,message_echoes,messaging_postbacks,messaging_referrals'


def graph(method, path, **params):
    version = os.environ.get('DESK_META_GRAPH_VERSION', 'v27.0')
    params['access_token'] = os.environ.get('FB_PAGE_ACCESS_TOKEN', '')
    try:
        r = requests.request(method, f'https://graph.facebook.com/{version}/{path}', params=params, timeout=20, allow_redirects=False)
        data = r.json()
    except Exception:
        return {'error': {'message': '連不上 Meta'}}  # never echo the exception: its URL carries the token
    if isinstance(data, dict) and 'error' in data:
        e = data['error']
        return {'error': {k: e.get(k) for k in ('message', 'code', 'error_subcode', 'type')}}
    return data


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'subscribe'))
    args = parser.parse_args(argv)
    env = WORKSPACE / 'secrets' / 'fb_cs_desk.env'
    if env.exists():
        load_env(env)
    out = {'settings': {n: bool(os.environ.get(n)) for n in NAMES}}
    page = os.environ.get('FB_PAGE_ID', '')
    if not (page and os.environ.get('FB_PAGE_ACCESS_TOKEN')):
        out['error'] = '缺 FB_PAGE_ID 或 FB_PAGE_ACCESS_TOKEN'
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1
    if args.command == 'subscribe':
        out['subscribe'] = graph('POST', f'{page}/subscribed_apps', subscribed_fields=FIELDS)
    me = graph('GET', 'me', fields='id,name')
    out['token_page_matches'] = me.get('id') == page if 'error' not in me else me
    out['page_name'] = me.get('name', '')
    ig = graph('GET', page, fields='instagram_business_account{id,username}')
    out['instagram'] = ig.get('instagram_business_account', ig if 'error' in ig else None)
    subs = graph('GET', f'{page}/subscribed_apps')
    out['subscribed'] = [dict(app=a.get('name'), fields=a.get('subscribed_fields')) for a in subs.get('data', [])] if 'error' not in subs else subs
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
