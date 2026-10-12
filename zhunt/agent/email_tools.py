"""Email for the agent over IMAP/SMTP with an app password (Gmail, iCloud, Yahoo, or any server).

Reading never marks mail as read (BODY.PEEK). Saving a draft and sending both wait for the
user's approval; sending shows the full message first. Email content is untrusted data: the
agent's system prompt tells it never to follow instructions found in messages.
Credentials live in ~/.zhunt/env (owner-only) and are never returned by the API.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Any

from zhunt.agent.tools import MAX_RESULT_CHARS, Tool, ToolError, _TextExtractor

ADDRESS_ENV = "ZHUNT_EMAIL_ADDRESS"
PASSWORD_ENV = "ZHUNT_EMAIL_PASSWORD"
IMAP_ENV = "ZHUNT_EMAIL_IMAP"  # host:port
SMTP_ENV = "ZHUNT_EMAIL_SMTP"  # host:port (465 = SSL, otherwise STARTTLS)

PRESETS = {
    "gmail": {"name": "Gmail", "imap": "imap.gmail.com:993", "smtp": "smtp.gmail.com:465",
              "help": "https://myaccount.google.com/apppasswords",
              "note": "Needs 2-Step Verification on, then an app password (16 letters)."},
    "icloud": {"name": "iCloud Mail", "imap": "imap.mail.me.com:993", "smtp": "smtp.mail.me.com:587",
               "help": "https://account.apple.com",
               "note": "Sign-In and Security > App-Specific Passwords."},
    "yahoo": {"name": "Yahoo Mail", "imap": "imap.mail.yahoo.com:993", "smtp": "smtp.mail.yahoo.com:465",
              "help": "https://login.yahoo.com/account/security",
              "note": "Account Security > Generate app password."},
}
_HOSTPORT = re.compile(r"[A-Za-z0-9.-]{3,253}:\d{2,5}")
MAX_LIST = 30


@dataclass(frozen=True)
class EmailAccount:
    address: str
    password: str
    imap: str
    smtp: str

    @classmethod
    def from_env(cls) -> EmailAccount | None:
        values = [os.environ.get(k, "") for k in (ADDRESS_ENV, PASSWORD_ENV, IMAP_ENV, SMTP_ENV)]
        return cls(*values) if all(values) else None


def valid_hostport(value: str) -> bool:
    return bool(_HOSTPORT.fullmatch(value))


def _split(hostport: str) -> tuple[str, int]:
    host, _, port = hostport.rpartition(":")
    return host, int(port)


def open_imap(account: EmailAccount) -> imaplib.IMAP4_SSL:
    host, port = _split(account.imap)
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(account.address, account.password)
    except imaplib.IMAP4.error as error:
        client.logout()
        raise ToolError(f"the email server refused the login: {str(error)[:120]}") from None
    return client


def check_login(account: EmailAccount) -> None:
    """Raise ToolError unless the account logs in over IMAP."""

    try:
        client = open_imap(account)
    except (OSError, ssl.SSLError) as error:
        raise ToolError(f"could not reach {account.imap} ({type(error).__name__})") from None
    client.logout()


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (ValueError, UnicodeDecodeError, LookupError):
        return value


def body_text(msg: email.message.Message) -> str:
    """The readable text of a message: its text/plain part, else its HTML as text."""

    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html is None:
            html = text
    if plain:
        return plain.strip()
    if html:
        parser = _TextExtractor()
        parser.feed(html)
        return parser.text()
    return ""


def _drafts_folder(client: imaplib.IMAP4) -> str:
    """The mailbox flagged \\Drafts (special-use), else a folder named like 'Drafts'."""

    status, boxes = client.list()
    names = []
    for raw in boxes or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) "?(?P<sep>[^"]*)"? (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if "\\Drafts" in m.group("flags"):
            return name
        names.append(name)
    for name in names:
        if name.lower().endswith("drafts"):
            return name
    return "Drafts"


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def email_tools(account: EmailAccount) -> list[Tool]:
    def _list(folder: str, limit: int, unread_only: bool, query: str) -> str:
        client = open_imap(account)
        try:
            status, _ = client.select(_quote(folder or "INBOX"), readonly=True)
            if status != "OK":
                raise ToolError(f"no folder called {folder!r}")
            criteria = ["UNSEEN"] if unread_only else ["ALL"]
            if query:
                safe = query.replace('"', "")[:120]
                criteria = (["UNSEEN"] if unread_only else []) + ["TEXT", f'"{safe}"']
            status, data = client.uid("search", None, *criteria)
            uids = (data[0] or b"").split()[-max(1, min(int(limit or 15), MAX_LIST)):]
            rows = []
            for uid in reversed(uids):
                status, parts = client.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                header = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                flags = b" ".join(p[0] for p in parts if isinstance(p, tuple))
                msg = email.message_from_bytes(header)
                unread = b"\\Seen" not in flags
                rows.append(f"- id {uid.decode()} | {'UNREAD | ' if unread else ''}{_decode(msg.get('Date'))[:25]} | from {_decode(msg.get('From'))[:60]} | {_decode(msg.get('Subject'))[:100]}")
            return f"{folder or 'INBOX'}: {len(rows)} message(s)\n" + ("\n".join(rows) or "(none)")
        finally:
            client.logout()

    def _read(message_id: str, folder: str) -> str:
        client = open_imap(account)
        try:
            client.select(_quote(folder or "INBOX"), readonly=True)
            status, parts = client.uid("fetch", str(message_id).encode(), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if not raw:
                raise ToolError(f"no message with id {message_id} in {folder or 'INBOX'}")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            head = "\n".join(f"{k}: {_decode(msg.get(k))}" for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID") if msg.get(k))
            return f"{head}\n\n{body_text(msg)[:MAX_RESULT_CHARS]}"
        finally:
            client.logout()

    def _compose(to: str, subject: str, body: str, reply_to_id: str | None, folder: str) -> EmailMessage:
        recipients = [a for _, a in getaddresses([to]) if "@" in a]
        if not recipients:
            raise ToolError("no valid recipient address")
        msg = EmailMessage()
        msg["From"] = account.address
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject.strip()[:300] or "(no subject)"
        msg["Message-ID"] = make_msgid(domain=parseaddr(account.address)[1].split("@")[-1] or None)
        if reply_to_id:
            original = _read(reply_to_id, folder)
            ref = re.search(r"^Message-ID: (.+)$", original, re.M)
            if ref:
                msg["In-Reply-To"] = ref.group(1).strip()
                msg["References"] = ref.group(1).strip()
        msg.set_content(body)
        return msg

    def _draft(to: str, subject: str, body: str, reply_to_id: str | None, folder: str) -> str:
        msg = _compose(to, subject, body, reply_to_id, folder)
        client = open_imap(account)
        try:
            drafts = _drafts_folder(client)
            status, _ = client.append(_quote(drafts), "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status != "OK":
                raise ToolError("the server would not save the draft")
            return f"Draft saved in {drafts} (to {msg['To']}, subject {msg['Subject']!r}). Nothing was sent."
        finally:
            client.logout()

    def _send(to: str, subject: str, body: str, reply_to_id: str | None, folder: str) -> str:
        msg = _compose(to, subject, body, reply_to_id, folder)
        host, port = _split(account.smtp)
        context = ssl.create_default_context()
        try:
            if port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls(context=context)
            with server:
                server.login(account.address, account.password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise ToolError("the mail server refused the login for sending") from None
        except (smtplib.SMTPException, OSError) as error:
            raise ToolError(f"sending failed ({type(error).__name__})") from None
        return f"Sent to {msg['To']} (subject {msg['Subject']!r})."

    async def list_emails(folder: str = "INBOX", limit: int = 15, unread_only: bool = False, query: str = "") -> str:
        return await asyncio.to_thread(_list, folder, limit, unread_only, query)

    async def read_email(id: str, folder: str = "INBOX") -> str:  # noqa: A002 - the model's parameter name
        return await asyncio.to_thread(_read, id, folder)

    async def draft_email(to: str, subject: str, body: str, reply_to_id: str = "", folder: str = "INBOX") -> str:
        return await asyncio.to_thread(_draft, to, subject, body, reply_to_id or None, folder)

    async def send_email(to: str, subject: str, body: str, reply_to_id: str = "", folder: str = "INBOX") -> str:
        return await asyncio.to_thread(_send, to, subject, body, reply_to_id or None, folder)

    compose = {"type": "object", "properties": {
        "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
        "subject": {"type": "string"}, "body": {"type": "string", "description": "Plain-text message."},
        "reply_to_id": {"type": "string", "description": "Optional: id of the message this replies to."},
        "folder": {"type": "string"}}, "required": ["to", "subject", "body"]}
    return [
        Tool("list_emails", "List recent emails (newest first) with id, sender, subject, date and whether unread. Optional text search. Never marks mail read.",
             {"type": "object", "properties": {"folder": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST},
                                               "unread_only": {"type": "boolean"}, "query": {"type": "string"}}}, "read", list_emails),
        Tool("read_email", "Read one email by id (from list_emails). Never marks it read.",
             {"type": "object", "properties": {"id": {"type": "string"}, "folder": {"type": "string"}}, "required": ["id"]}, "read", read_email),
        Tool("draft_email", "Save a draft (e.g. a reply) in the Drafts folder without sending. The user must approve.", compose, "write", draft_email),
        Tool("send_email", "Send an email now. The user must approve and sees the whole message first.", compose, "send", send_email),
    ]


def status() -> dict[str, Any]:
    account = EmailAccount.from_env()
    return {"connected": account is not None, "address": account.address if account else None,
            "presets": {k: {key: v[key] for key in ("name", "help", "note")} for k, v in PRESETS.items()}}
