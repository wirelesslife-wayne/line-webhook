"""Desk-only server-side sessions; does not change the host Flask session settings."""
import hashlib
import hmac
import secrets
from datetime import timedelta
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
from .db import now

COOKIE = 'desk_session'
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(24))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Auth:
    def __init__(self, store, secret):
        self.store = store
        self.signer = URLSafeTimedSerializer(secret,salt='cs-desk-session')

    def login(self, username, password):
        username = username.strip()
        bucket = digest(username.casefold())
        with self.store.cursor() as cur:
            cur.execute('INSERT INTO desk_login_attempts(bucket) VALUES(%s) ON CONFLICT DO NOTHING',(bucket,))
            cur.execute('SELECT * FROM desk_login_attempts WHERE bucket=%s FOR UPDATE',(bucket,))
            attempts = cur.fetchone()
            if attempts['locked_until'] and attempts['locked_until'] > now():
                return None
            cur.execute('SELECT * FROM desk_users WHERE username=%s AND enabled=TRUE',(username,))
            user = cur.fetchone()
            verified = check_password_hash(user['password_hash'] if user else DUMMY_HASH,password)
            if not user or not verified:
                n = 1 if attempts['locked_until'] else attempts['failures']+1
                cur.execute('UPDATE desk_login_attempts SET failures=%s,locked_until=%s WHERE bucket=%s',
                    (n,now()+timedelta(minutes=15) if n>=5 else None,bucket))
                return None
            cur.execute('UPDATE desk_login_attempts SET failures=0,locked_until=NULL WHERE bucket=%s',(bucket,))
            token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
            cur.execute('INSERT INTO desk_sessions VALUES(%s,%s,%s,%s)',
                (digest(token),user['id'],csrf,now()+timedelta(hours=12)))
        return self.signer.dumps(token)

    def session(self, cookie):
        try:
            token = self.signer.loads(cookie or '',max_age=43200)
        except (BadSignature,SignatureExpired):
            return None
        if not isinstance(token,str):
            return None
        return self.store.one('''SELECT s.sid,s.csrf,u.id,u.username FROM desk_sessions s
          JOIN desk_users u ON u.id=s.user_id WHERE s.sid=%s AND s.expires_at>NOW() AND u.enabled=TRUE''',(digest(token),))

    def preauth(self):
        return self.signer.dumps({'login':secrets.token_urlsafe(24)})

    def check_preauth(self, cookie, value):
        if not value or not cookie or not hmac.compare_digest(cookie,value):
            return False
        try:
            return isinstance(self.signer.loads(value,max_age=900),dict)
        except (BadSignature,SignatureExpired):
            return False
