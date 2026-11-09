"""Create only new desk accounts; never resets an existing password."""
import json
import os
import secrets
from werkzeug.security import generate_password_hash,check_password_hash
from .common import WORKSPACE,store


def run():
    path=WORKSPACE/'secrets'/'cs_desk_users.env'
    db=store()
    if path.exists():
        # No password rotation on rerun. Verify all identities and hashes instead.
        values=dict(line.split('=',1) for line in path.read_text(encoding='utf-8').splitlines() if '=' in line and not line.startswith('#'))
        for key in ('WAYNE','XINRUO'):
            row=db.one('SELECT password_hash FROM desk_users WHERE username=%s',(values[key+'_USERNAME'],))
            if not row or not check_password_hash(row['password_hash'],values[key+'_PASSWORD']):
                raise RuntimeError('帳號檔與資料庫不一致，需人工查核；沒有重設密碼')
        print(json.dumps({'ok':True,'users_verified':2,'created':False}))
        return
    identities=[('WAYNE','Wayne'),('XINRUO','馨若')]
    if any(db.one('SELECT id FROM desk_users WHERE username=%s',(u,)) for _,u in identities):
        raise RuntimeError('帳號已存在但本機帳號檔缺少，沒有覆寫既有帳密')
    lines=['# Private: do not commit or print. Existing passwords are never reset.']
    passwords={u:secrets.token_urlsafe(24) for _,u in identities}
    with db.cursor() as cur:
        for key,u in identities:
            cur.execute('INSERT INTO desk_users(username,password_hash) VALUES(%s,%s)',(u,generate_password_hash(passwords[u])))
            lines += [key+'_USERNAME='+u,key+'_PASSWORD='+passwords[u]]
        # Exclusive create: refuse to overwrite secrets. DB rolls back on write failure.
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as f:
            f.write('\n'.join(lines)+'\n')
    print(json.dumps({'ok':True,'users_created':2,'passwords_logged':False}))


if __name__=='__main__':
    run()
