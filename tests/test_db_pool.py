"""Reused database connections (Wayne 11/12: the desk felt slow). Read-only queries, no tables touched."""
import os
import threading
import pytest
import psycopg2
from desk.db import Store


@pytest.fixture
def store():
    url = os.environ.get('DESK_TEST_DATABASE_URL')
    if not url:
        pytest.skip('set DESK_TEST_DATABASE_URL')
    s = Store(url)
    yield s
    if s._pool:
        s._pool.closeall()


def pid(store):
    return store.one('SELECT pg_backend_pid() AS p')['p']


def test_queries_reuse_one_connection(store):
    assert pid(store) == pid(store) == pid(store)


def test_a_failed_query_leaves_a_usable_connection(store):
    with pytest.raises(psycopg2.Error):
        store.one('SELECT * FROM no_such_table_for_pool_test')
    assert store.one('SELECT 1 AS x')['x'] == 1


def test_a_dead_connection_is_replaced(store):
    first = pid(store)
    with store.connect(fresh=True) as killer, killer.cursor() as cur:
        cur.execute('SELECT pg_terminate_backend(%s)', (first,))
    store._used.clear()  # treat it as idle so it is checked before reuse
    assert pid(store) != first


def test_session_locks_never_use_a_pooled_connection(store):
    pooled = pid(store)
    with store.lock('desk-pool-test') as got:
        assert got
        holder = store.one("SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=hashtext('desk-pool-test')::oid")
        assert holder and holder['pid'] != pooled
    assert store.one("SELECT COUNT(*) AS n FROM pg_locks WHERE locktype='advisory' AND objid=hashtext('desk-pool-test')::oid")['n'] == 0


def test_threads_share_the_pool(store):
    errors = []
    def work():
        try:
            for _ in range(5):
                assert store.one('SELECT 1 AS x')['x'] == 1
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=work) for _ in range(12)]  # more than POOL_MAX: extra ones fall back
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
