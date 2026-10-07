"""Reply, opt-out and bounce detection (IMAP for SMTP mode, Gmail API, or none for dry-run)."""

from __future__ import annotations

import email
import imaplib
import re
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from email.policy import default as default_policy
from email.utils import parseaddr

from ..config import Settings, get_settings
from ..models import OutreachLog

OPT_OUT_RE = re.compile(
    r"\b(?:unsubscribe|opt[\s-]?out|remove me|stop (?:emailing|contacting)|do not (?:email|contact)|"
    r"don't (?:email|contact))\b",
    re.IGNORECASE,
)


@dataclass
class ReplyStatus:
    replied: bool = False
    opted_out: bool = False
    bounced: bool = False
    snippet: str = ""

    @property
    def any(self) -> bool:
        return self.replied or self.opted_out or self.bounced


class ReplyChecker(ABC):
    name = "none"

    @abstractmethod
    def check(self, log: OutreachLog) -> ReplyStatus: ...

    def close(self) -> None:  # noqa: B027 - optional hook, most checkers hold no connection
        pass


class NullReplyChecker(ReplyChecker):
    """Dry-run: replies are recorded by hand from the dashboard."""

    def check(self, log: OutreachLog) -> ReplyStatus:
        return ReplyStatus()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class IMAPReplyChecker(ReplyChecker):
    name = "imap"

    def __init__(self, host: str, port: int, username: str, password: str, *,
                 mailbox: str = "INBOX", timeout: float = 30.0, imap_factory=None):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.mailbox, self.timeout = mailbox, timeout
        self._factory = imap_factory
        self._conn: imaplib.IMAP4 | None = None

    def _connection(self) -> imaplib.IMAP4:
        if self._conn is None:
            if self._factory is not None:
                conn = self._factory()
            else:
                conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(),
                                         timeout=self.timeout)
            conn.login(self.username, self.password)
            conn.select(self.mailbox, readonly=True)
            self._conn = conn
        return self._conn

    def _search(self, *criteria: str) -> list[bytes]:
        typ, data = self._connection().search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _text(self, msg_id: bytes) -> tuple[str, str]:
        typ, data = self._connection().fetch(msg_id, "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return "", ""
        message = email.message_from_bytes(data[0][1], policy=default_policy)
        sender = parseaddr(str(message.get("From", "")))[1].lower()
        part = message.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part is not None else ""
        return sender, re.sub(r"\s+", " ", text)[:1500]

    def check(self, log: OutreachLog) -> ReplyStatus:
        if not log.sent_at:
            return ReplyStatus()
        since = (log.sent_at - timedelta(days=1)).strftime("%d-%b-%Y")
        ids: list[bytes] = []
        if log.message_id:
            ids += self._search("HEADER", "In-Reply-To", _quote(log.message_id))
            ids += self._search("HEADER", "References", _quote(log.message_id))
        ids += self._search("FROM", _quote(log.to_email), "SINCE", since)
        for msg_id in sorted(set(ids), key=int, reverse=True):
            sender, text = self._text(msg_id)
            if sender and sender == log.to_email.lower():
                # Only the part above the quoted original counts as their words.
                own = re.split(r"\bOn .{5,80} wrote:", text)[0]
                return ReplyStatus(replied=True, opted_out=bool(OPT_OUT_RE.search(own)), snippet=own[:300])
        bounces = self._search("FROM", '"mailer-daemon"', "SINCE", since, "BODY", _quote(log.to_email))
        bounces += self._search("FROM", '"postmaster"', "SINCE", since, "BODY", _quote(log.to_email))
        if bounces:
            return ReplyStatus(bounced=True, snippet="Delivery failure notice received")
        return ReplyStatus()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            self._conn = None


class GmailReplyChecker(ReplyChecker):
    name = "gmail_api"

    def __init__(self, service_factory, sender_email: str):
        self._service_factory = service_factory
        self._service = None
        self.sender_email = sender_email.lower()

    @property
    def service(self):
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    def check(self, log: OutreachLog) -> ReplyStatus:
        if not log.sent_at:
            return ReplyStatus()
        users = self.service.users()
        if log.thread_id and not log.thread_id.startswith("dry-"):
            thread = users.threads().get(userId="me", id=log.thread_id, format="metadata",
                                         metadataHeaders=["From"]).execute()
            for message in thread.get("messages", []):
                headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
                sender = parseaddr(headers.get("from", ""))[1].lower()
                if sender and sender != self.sender_email:
                    snippet = message.get("snippet", "")
                    return ReplyStatus(replied=True, opted_out=bool(OPT_OUT_RE.search(snippet)), snippet=snippet)
        after = int((log.sent_at - timedelta(days=1)).timestamp())
        found = users.messages().list(userId="me", q=f"from:{log.to_email} after:{after}", maxResults=1).execute()
        if found.get("messages"):
            message = users.messages().get(userId="me", id=found["messages"][0]["id"], format="metadata").execute()
            snippet = message.get("snippet", "")
            return ReplyStatus(replied=True, opted_out=bool(OPT_OUT_RE.search(snippet)), snippet=snippet)
        bounce = users.messages().list(userId="me", q=f"from:mailer-daemon {log.to_email} after:{after}",
                                       maxResults=1).execute()
        if bounce.get("messages"):
            return ReplyStatus(bounced=True, snippet="Delivery failure notice received")
        return ReplyStatus()


def build_reply_checker(settings: Settings | None = None) -> ReplyChecker:
    settings = settings or get_settings()
    if settings.email_backend == "smtp":
        username = settings.imap_username or settings.smtp_username
        password = settings.imap_password or settings.smtp_password
        if settings.imap_host and username and password:
            return IMAPReplyChecker(settings.imap_host, settings.imap_port, username, password)
        return NullReplyChecker()
    if settings.email_backend == "gmail_api":
        from .gmail import build_service

        return GmailReplyChecker(lambda: build_service(settings), settings.sender_email)
    return NullReplyChecker()
