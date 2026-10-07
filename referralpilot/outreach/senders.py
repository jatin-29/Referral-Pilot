"""Email transport: dry-run (.eml files), authenticated SMTP, or the Gmail API."""

from __future__ import annotations

import base64
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from ..config import Settings, get_settings
from ..models import utcnow
from ..textutil import slugify


class SendError(RuntimeError):
    """`permanent` failures are not retried; `fatal` ones pause the whole queue (bad credentials...)."""

    def __init__(self, message: str, *, permanent: bool = False, fatal: bool = False, bounced: bool = False):
        super().__init__(message)
        self.permanent = permanent or fatal or bounced
        self.fatal = fatal
        self.bounced = bounced


@dataclass
class SendResult:
    message_id: str
    thread_id: str | None = None
    provider_id: str | None = None


@dataclass
class Attachment:
    path: Path
    filename: str


def new_message_id(sender_email: str) -> str:
    domain = sender_email.split("@", 1)[-1] if "@" in sender_email else "referralpilot.local"
    return make_msgid(idstring="rp", domain=domain)


def build_message(
    *,
    sender_name: str,
    sender_email: str,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[Attachment] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    # Opt-out tag: one-click "unsubscribe" reply in clients that support it.
    msg["List-Unsubscribe"] = f"<mailto:{sender_email}?subject=unsubscribe>"
    msg.set_content(body)
    for attachment in attachments or []:
        msg.add_attachment(attachment.path.read_bytes(), maintype="application", subtype="pdf",
                           filename=attachment.filename)
    return msg


def resume_attachment(path: str | None, full_name: str) -> Attachment | None:
    if not path or not Path(path).exists():
        return None
    name = slugify(full_name).replace("-", "_").title() or "Resume"
    return Attachment(Path(path), f"{name}_Resume.pdf")


class Sender(ABC):
    name: str = "sender"
    dry_run: bool = False

    @abstractmethod
    def send(self, message: EmailMessage, *, thread_id: str | None = None) -> SendResult: ...


class DryRunSender(Sender):
    """Writes each message to exports/outbox/*.eml instead of sending it."""

    name = "dry_run"
    dry_run = True

    def __init__(self, outbox_dir: Path):
        self.outbox_dir = outbox_dir

    def send(self, message: EmailMessage, *, thread_id: str | None = None) -> SendResult:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        recipient = parseaddr(str(message["To"]))[1] or str(message["To"])
        path = self.outbox_dir / f"{stamp}_{slugify(recipient, 50)}.eml"
        path.write_bytes(message.as_bytes())
        message_id = str(message["Message-ID"])
        return SendResult(message_id=message_id, thread_id=thread_id or f"dry-{message_id.strip('<>')}",
                          provider_id=str(path))


class SMTPSender(Sender):
    name = "smtp"

    def __init__(self, host: str, port: int, username: str = "", password: str = "", *,
                 use_ssl: bool = False, timeout: float = 30.0):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.use_ssl, self.timeout = use_ssl, timeout

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=self.timeout)
        server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        elif self.username:
            server.quit()
            raise SendError(f"{self.host} does not offer STARTTLS; refusing to send credentials in clear text",
                            fatal=True)
        return server

    def send(self, message: EmailMessage, *, thread_id: str | None = None) -> SendResult:
        try:
            server = self._connect()
        except SendError:
            raise
        except (OSError, smtplib.SMTPException) as exc:
            raise SendError(f"SMTP connection to {self.host}:{self.port} failed: {exc}") from exc
        try:
            if self.username:
                server.login(self.username, self.password)
            refused = server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(f"SMTP login rejected: {exc.smtp_error!r}", fatal=True) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendError(f"Recipient refused: {exc.recipients}", bounced=True) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(f"Sender refused: {exc.smtp_error!r}", fatal=True) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise SendError(f"SMTP send failed: {exc}") from exc
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                pass
        if refused:
            raise SendError(f"Recipient refused: {refused}", bounced=True)
        message_id = str(message["Message-ID"])
        return SendResult(message_id=message_id, thread_id=thread_id or message_id)


class GmailAPISender(Sender):
    """Sends through users.messages.send so mail appears in Sent and threads natively."""

    name = "gmail_api"

    def __init__(self, service_factory):
        self._service_factory = service_factory
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    def send(self, message: EmailMessage, *, thread_id: str | None = None) -> SendResult:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        body: dict = {"raw": raw}
        if thread_id and not thread_id.startswith("dry-"):
            body["threadId"] = thread_id
        try:
            sent = self.service.users().messages().send(userId="me", body=body).execute()
            meta = self.service.users().messages().get(
                userId="me", id=sent["id"], format="metadata", metadataHeaders=["Message-ID", "Message-Id"]
            ).execute()
        except Exception as exc:  # googleapiclient.errors.HttpError and transport errors
            status = getattr(getattr(exc, "resp", None), "status", None)
            raise SendError(f"Gmail API send failed: {exc}", fatal=status in (401, 403)) from exc
        headers = {h["name"].lower(): h["value"] for h in (meta.get("payload") or {}).get("headers", [])}
        return SendResult(
            message_id=headers.get("message-id", str(message["Message-ID"])),
            thread_id=sent.get("threadId"),
            provider_id=sent.get("id"),
        )


def build_sender(settings: Settings | None = None) -> Sender:
    settings = settings or get_settings()
    if settings.email_backend == "smtp":
        return SMTPSender(settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password,
                          use_ssl=settings.smtp_use_ssl)
    if settings.email_backend == "gmail_api":
        from .gmail import build_service

        return GmailAPISender(lambda: build_service(settings))
    return DryRunSender(settings.outbox_dir)
