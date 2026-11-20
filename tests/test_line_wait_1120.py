"""Wayne 11/20: in opening hours a LINE message waits 15 quiet minutes for a colleague before the AI drafts."""
from datetime import datetime, timedelta, timezone
from desk import fb

TPE = timezone(timedelta(hours=8))


class Store:
    def __init__(self, rows):
        self.rows = rows

    def all(self, sql, args=()):
        return self.rows


def ev(role, text, at, note=''):
    return dict(role=role, note=note, content=text, event_at=at, received_at=at)


def wait(rows, at, **c):
    return fb.line_wait_until(Store(rows), dict(dict(id='x', channel='line_message'), **c), at)


def test_opening_hours_waits_15_quiet_minutes_capped_at_30():
    t = datetime(2026, 11, 20, 14, 0, tzinfo=TPE)   # a Friday, open 10-20
    rows = [ev('page', '您好', t - timedelta(hours=1)), ev('user', '尾門多少錢', t - timedelta(minutes=5))]
    assert wait(rows, t) == t + timedelta(minutes=10)
    many = rows[:1] + [ev('user', '請問', t - timedelta(minutes=28)), ev('user', '在嗎', t - timedelta(minutes=1))]
    assert wait(many, t) == t + timedelta(minutes=2)          # 30 minutes after the first unanswered line
    assert wait(rows, t + timedelta(minutes=11)) is None       # quiet long enough: draft


def test_no_wait_at_night_on_sunday_for_urgent_words_and_when_asked():
    rows = [ev('user', '尾門多少錢', datetime(2026, 11, 20, 21, 0, tzinfo=TPE))]
    assert wait(rows, datetime(2026, 11, 20, 21, 5, tzinfo=TPE)) is None      # after 20:00
    sunday = datetime(2026, 11, 22, 14, 0, tzinfo=TPE)
    assert wait([ev('user', '尾門', sunday)], sunday + timedelta(minutes=1)) is None
    t = datetime(2026, 11, 20, 14, 0, tzinfo=TPE)
    assert wait([ev('user', '主機壞了不能用', t)], t + timedelta(minutes=1)) is None
    assert wait([ev('user', '尾門', t)], t + timedelta(minutes=1), draft_now=True) is None
    assert wait([ev('user', '尾門', t)], t + timedelta(minutes=1), channel='fb_message') is None
    digest = datetime(2026, 11, 20, 11, 57, tzinfo=TPE)                         # 3 minutes before the 12:00 digest
    assert wait([ev('user', '尾門', digest)], digest) is None
