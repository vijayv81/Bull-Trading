import pytest

from trading_agent.notify import senders


@pytest.fixture
def resend_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_fake_key")


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(senders.requests, "post", _post)
    return calls


def test_send_email_no_destination_is_a_noop(monkeypatch, resend_env, fake_post):
    monkeypatch.delenv("NOTIFY_EMAIL_ADDRESS", raising=False)
    assert senders.send_email("subject", "body") is False
    assert fake_post == []


def test_send_email_sends_when_destination_set(monkeypatch, resend_env, fake_post):
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    assert senders.send_email("subject", "body") is True
    assert len(fake_post) == 1
    call = fake_post[0]
    assert call["url"] == senders.RESEND_URL
    assert call["json"]["to"] == ["me@example.com"]
    assert call["json"]["subject"] == "subject"
    assert call["json"]["text"] == "body"
    assert call["json"]["from"] == senders.DEFAULT_FROM_ADDRESS
    assert call["headers"]["Authorization"] == "Bearer re_fake_key"


def test_send_email_includes_html_when_given(monkeypatch, resend_env, fake_post):
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    senders.send_email("subject", "body", html_body="<p>body</p>")
    assert fake_post[0]["json"]["html"] == "<p>body</p>"


def test_send_email_uses_custom_from_address(monkeypatch, resend_env, fake_post):
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("RESEND_FROM_ADDRESS", "bot@mydomain.com")
    senders.send_email("subject", "body")
    assert fake_post[0]["json"]["from"] == "bot@mydomain.com"


def test_send_email_missing_api_key_raises(monkeypatch, fake_post):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    with pytest.raises(RuntimeError):
        senders.send_email("subject", "body")


def test_send_email_raises_on_http_error(monkeypatch, resend_env):
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setattr(senders.requests, "post", lambda *a, **k: FakeResponse(status_code=422))
    with pytest.raises(RuntimeError):
        senders.send_email("subject", "body")


def test_send_sms_no_destination_is_a_noop(monkeypatch, resend_env, fake_post):
    monkeypatch.delenv("SMS_GATEWAY_ADDRESS", raising=False)
    assert senders.send_sms("body") is False
    assert fake_post == []


def test_send_sms_sends_when_destination_set(monkeypatch, resend_env, fake_post):
    monkeypatch.setenv("SMS_GATEWAY_ADDRESS", "5551234567@tmomail.net")
    assert senders.send_sms("body") is True
    call = fake_post[0]
    assert call["json"]["to"] == ["5551234567@tmomail.net"]
    assert call["json"]["text"] == "body"
