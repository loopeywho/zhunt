import asyncio
import email
import imaplib
from email.message import EmailMessage
from unittest.mock import patch

import pytest

from zhunt.agent import email_tools as E
from zhunt.agent.tools import ToolError

ACCOUNT = E.EmailAccount("me@example.com", "app-pass-1234", "imap.example.com:993", "smtp.example.com:465")


def raw_message(subject="Lunch?", body="Are you free on Friday?", html=None, msg_id="<abc@example.com>"):
    msg = EmailMessage()
    msg["From"] = "Sam <sam@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Sun, 11 Oct 2026 10:00:00 +0000"
    msg["Message-ID"] = msg_id
    if html:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    instances: list = []
    fail_login = False

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        self.appended = []
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if FakeIMAP.fail_login:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.calls.append(("login", user))
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "search":
            return "OK", [b"7 8"]
        if command == "fetch":
            uid, what = args
            if b"HEADER.FIELDS" in what.encode() if isinstance(what, str) else False:
                flags = b"8 (UID 8 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {60}" if uid == b"8" else b"7 (UID 7 FLAGS () BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {60}"
                header = raw_message(subject="Seen one" if uid == b"8" else "Lunch?").split(b"\n\n")[0]
                return "OK", [(flags, header), b")"]
            return "OK", [(b"7 (UID 7 BODY[] {300}", raw_message()), b")"]
        raise AssertionError(command)

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"']

    def append(self, mailbox, flags, date, message):
        self.appended.append((mailbox, flags, message))
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def starttls(self, context=None):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def fakes():
    FakeIMAP.instances.clear()
    FakeIMAP.fail_login = False
    FakeSMTP.sent.clear()
    with patch("imaplib.IMAP4_SSL", FakeIMAP), patch("smtplib.SMTP_SSL", FakeSMTP), patch("smtplib.SMTP", FakeSMTP):
        yield


def tools():
    return {t.name: t for t in E.email_tools(ACCOUNT)}


def test_listing_never_marks_mail_read_and_shows_unread():
    out = asyncio.run(tools()["list_emails"].run(limit=5))
    imap = FakeIMAP.instances[-1]
    assert ("select", '"INBOX"', True) in imap.calls  # read-only mailbox
    fetches = [c for c in imap.calls if c[:2] == ("uid", "fetch")]
    assert fetches and all("PEEK" in c[3] for c in fetches)
    assert "id 7 | UNREAD" in out and "Lunch?" in out and "Seen one" in out and "id 8 | UNREAD" not in out


def test_reading_returns_headers_and_text():
    out = asyncio.run(tools()["read_email"].run(id="7"))
    assert "From: Sam <sam@example.com>" in out and "Are you free on Friday?" in out
    assert "PEEK" in [c for c in FakeIMAP.instances[-1].calls if c[:2] == ("uid", "fetch")][0][3]


def test_html_only_messages_are_read_as_text():
    msg = email.message_from_bytes(raw_message(html="<p>Hello <b>there</b></p><script>x()</script>"))
    assert E.body_text(msg) == "Hello there"


def test_a_draft_goes_to_the_drafts_folder_and_is_not_sent():
    out = asyncio.run(tools()["draft_email"].run(to="sam@example.com", subject="Re: Lunch?", body="Friday works.", reply_to_id="7"))
    appended = [a for i in FakeIMAP.instances for a in i.appended]
    assert appended and appended[0][0] == '"[Gmail]/Drafts"' and "\\Draft" in appended[0][1]
    draft = email.message_from_bytes(appended[0][2])
    assert draft["In-Reply-To"] == "<abc@example.com>" and "Friday works." in draft.get_payload()
    assert "Nothing was sent" in out and not FakeSMTP.sent


def test_sending_uses_tls_and_refuses_bad_recipients():
    out = asyncio.run(tools()["send_email"].run(to="sam@example.com", subject="Hi", body="Hello"))
    assert FakeSMTP.sent and FakeSMTP.sent[0]["To"] == "sam@example.com" and "Sent to" in out
    with pytest.raises(ToolError, match="recipient"):
        asyncio.run(tools()["send_email"].run(to="not an address", subject="x", body="y"))


def test_send_and_draft_need_approval_reading_does_not():
    risks = {t.name: t.risk for t in E.email_tools(ACCOUNT)}
    assert risks == {"list_emails": "read", "read_email": "read", "draft_email": "write", "send_email": "send"}


def test_a_refused_login_is_a_readable_error():
    FakeIMAP.fail_login = True
    with pytest.raises(ToolError, match="refused the login"):
        E.check_login(ACCOUNT)
