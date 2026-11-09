"""No test may spend API funds or deliver an external message."""
import pytest
import requests


@pytest.fixture(autouse=True)
def deny_external_http(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('DESK_MODEL', raising=False)
    monkeypatch.delenv('DESK_MONTHLY_USD', raising=False)
    def denied(*args, **kwargs):
        raise AssertionError('external HTTP forbidden in tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)
