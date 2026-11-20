"""No test may spend API funds or deliver an external message."""
import pytest
import requests


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('LINE_CHANNEL_ACCESS_TOKEN', raising=False)
    monkeypatch.setenv('DESK_TEST', '1')
    monkeypatch.delenv('DESK_MODEL', raising=False)
    monkeypatch.delenv('DESK_MONTHLY_USD', raising=False)
    def denied(*args, **kwargs):
        raise AssertionError('external HTTP forbidden in tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)



@pytest.fixture(autouse=True)
def _no_line_wait(monkeypatch):
    """Wayne 11/20: LINE drafts wait 15 minutes in opening hours; tests draft at once whatever the clock says."""
    from desk import fb
    monkeypatch.setattr(fb, 'LINE_WAIT_ON', False)
    monkeypatch.setattr(fb, 'LINE_DRAFTING', True)  # 11/20 LINE drafting is paused in production; tests cover the drafting
