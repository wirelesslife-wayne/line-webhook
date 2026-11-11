"""Optional customer service desk; no network or database work at import time."""
import os


def install(app):
    secret = os.environ.get('DESK_SECRET_KEY','')
    if not secret:
        return
    if len(secret)<32:
        raise ValueError('DESK_SECRET_KEY must contain at least 32 random characters')
    from .db import Store
    from .routes import make_blueprint
    store = Store(os.environ.get('DATABASE_URL', ''))
    app.register_blueprint(make_blueprint(store), url_prefix='/desk')
    app.extensions['desk_store'] = store
    if os.environ.get('DESK_WORKER_ENABLED') == '1':
        from .worker import start_worker
        start_worker(store)
        from .fb import start_worker as start_fb_worker
        start_fb_worker(store)
