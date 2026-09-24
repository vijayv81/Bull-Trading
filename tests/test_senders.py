import pytest

from trading_agent.notify import senders


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, to_addrs, msg))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(senders.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_send_email_no_destination_is_a_noop(monkeypatch, smtp_env):
    monkeypatch.delenv("NOTIFY_EMAIL_ADDRESS", raising=False)
    assert senders.send_email("subject", "body") is False
    assert FakeSMTP.instances == []


def test_send_email_sends_when_destination_set(monkeypatch, smtp_env):
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    assert senders.send_email("subject", "body") is True
    assert len(FakeSMTP.instances) == 1
    to_addrs = FakeSMTP.instances[0].sent[0][1]
    assert to_addrs == ["me@example.com"]


def test_send_email_missing_smtp_creds_raises(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("NOTIFY_EMAIL_ADDRESS", "me@example.com")
    with pytest.raises(RuntimeError):
        senders.send_email("subject", "body")


def test_send_sms_no_destination_is_a_noop(monkeypatch, smtp_env):
    monkeypatch.delenv("SMS_GATEWAY_ADDRESS", raising=False)
    assert senders.send_sms("body") is False
    assert FakeSMTP.instances == []


def test_send_sms_sends_when_destination_set(monkeypatch, smtp_env):
    monkeypatch.setenv("SMS_GATEWAY_ADDRESS", "5551234567@tmomail.net")
    assert senders.send_sms("body") is True
    to_addrs = FakeSMTP.instances[0].sent[0][1]
    assert to_addrs == ["5551234567@tmomail.net"]
