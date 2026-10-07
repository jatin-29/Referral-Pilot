"""Gmail REST API for the browser build.

The page signs in with Google Identity Services and hands the worker a
short-lived access token (about an hour). The token only lives in memory:
when it expires, sending pauses until the user reconnects Gmail, and queued
emails simply wait - nothing is marked failed.
"""

from __future__ import annotations

import base64
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

from ..models import OutreachLog
from .replies import OPT_OUT_RE, ReplyChecker, ReplyStatus
from .senders import Sender, SendError, SendResult

API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"

_token: dict[str, object] = {}


def set_token(access_token: str | None, expires_in: float = 3600, email: str | None = None) -> None:
    _token.clear()
    if access_token:
        # Treat the token as expired a minute early so a send never starts with a dying token.
        _token.update(access_token=access_token, expires_at=time.time() + float(expires_in) - 60, email=email)


def current_token() -> str | None:
    if _token and float(_token["expires_at"]) > time.time():  # type: ignore[arg-type]
        return str(_token["access_token"])
    return None


def token_status() -> dict[str, object]:
    token = current_token()
    return {
        "connected": token is not None,
        "email": _token.get("email") if token else None,
        "expires_in": int(float(_token["expires_at"]) - time.time()) if token else 0,  # type: ignore[arg-type]
    }


def _client() -> httpx.Client:
    from ..fetch import build_client

    return build_client()


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message") or response.text[:200])
    except ValueError:
        return response.text[:200]


class GmailWebSender(Sender):
    name = "gmail_web"

    def blocked_reason(self) -> str | None:
        return None if current_token() else "Connect Gmail to start sending"

    @property
    def account_email(self) -> str | None:
        return str(_token["email"]) if current_token() and _token.get("email") else None

    def send(self, message: EmailMessage, *, thread_id: str | None = None) -> SendResult:
        token = current_token()
        if token is None:
            raise SendError("Gmail is not connected")
        body: dict = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        if thread_id and not thread_id.startswith("dry-"):
            body["threadId"] = thread_id
        auth = {"Authorization": f"Bearer {token}"}
        with _client() as client:
            try:
                response = client.post(f"{API}/messages/send", json=body, headers=auth)
            except httpx.HTTPError as exc:
                # The browser may have delivered the request before failing: never retry blindly.
                raise SendError(f"Gmail API request failed ({exc}) - check your Sent folder before retrying",
                                permanent=True) from exc
            if response.status_code == 401:
                set_token(None)  # pauses sending (blocked_reason) until the user reconnects
                raise SendError("Gmail sign-in expired - reconnect Gmail; the email was not sent")
            if response.status_code == 403:
                raise SendError(f"Gmail refused to send: {_error_detail(response)}", fatal=True)
            if response.status_code == 429 or response.status_code >= 500:
                raise SendError(f"Gmail API is busy ({response.status_code}); will retry")
            if response.status_code >= 400:
                raise SendError(f"Gmail API error {response.status_code}: {_error_detail(response)}",
                                permanent=True)
            sent = response.json()
            message_id = str(message["Message-ID"])
            try:
                meta = client.get(f"{API}/messages/{sent['id']}", headers=auth,
                                  params={"format": "metadata", "metadataHeaders": ["Message-ID", "Message-Id"]})
                if meta.status_code == 200:
                    headers = {h["name"].lower(): h["value"] for h in meta.json().get("payload", {}).get("headers", [])}
                    message_id = headers.get("message-id", message_id)
            except (httpx.HTTPError, ValueError, KeyError):
                pass  # the email went out; threading falls back to our own Message-ID
        return SendResult(message_id=message_id, thread_id=sent.get("threadId"), provider_id=sent.get("id"))


class GmailWebReplyChecker(ReplyChecker):
    name = "gmail_web"

    def __init__(self, sender_email: str = ""):
        self.sender_email = sender_email.lower()
        self._http: httpx.Client | None = None

    def _get(self, path: str, **params) -> dict:
        token = current_token()
        if token is None:
            raise RuntimeError("Gmail is not connected")
        if self._http is None:
            self._http = _client()
        response = self._http.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            set_token(None)
        response.raise_for_status()
        return response.json()

    def check(self, log: OutreachLog) -> ReplyStatus:
        if not log.sent_at or current_token() is None:
            return ReplyStatus()
        own = self.sender_email or str(_token.get("email") or "").lower()
        if log.thread_id and not log.thread_id.startswith("dry-"):
            thread = self._get(f"threads/{log.thread_id}", format="metadata", metadataHeaders="From")
            for message in thread.get("messages", []):
                headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
                sender = parseaddr(headers.get("from", ""))[1].lower()
                if sender and sender != own:
                    snippet = message.get("snippet", "")
                    return ReplyStatus(replied=True, opted_out=bool(OPT_OUT_RE.search(snippet)), snippet=snippet)
        after = int((log.sent_at - timedelta(days=1)).timestamp())
        found = self._get("messages", q=f"from:{log.to_email} after:{after}", maxResults=1)
        if found.get("messages"):
            message = self._get(f"messages/{found['messages'][0]['id']}", format="metadata")
            snippet = message.get("snippet", "")
            return ReplyStatus(replied=True, opted_out=bool(OPT_OUT_RE.search(snippet)), snippet=snippet)
        bounce = self._get("messages", q=f"from:mailer-daemon {log.to_email} after:{after}", maxResults=1)
        if bounce.get("messages"):
            return ReplyStatus(bounced=True, snippet="Delivery failure notice received")
        return ReplyStatus()

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
