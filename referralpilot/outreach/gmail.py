"""Gmail API OAuth2 (installed-app flow) with a cached, auto-refreshed token."""

from __future__ import annotations

import os
from pathlib import Path

from ..config import Settings, get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",  # reply / bounce / opt-out detection
]


class GmailAuthError(RuntimeError):
    pass


def _save_token(token_file: Path, creds) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass


def load_credentials(settings: Settings | None = None, *, interactive: bool = False):
    settings = settings or get_settings()
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailAuthError("Gmail support is not installed: pip install 'referralpilot[gmail]'") from exc

    token_file = Path(settings.gmail_token_file)
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES) if token_file.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(token_file, creds)
        return creds
    if not interactive:
        raise GmailAuthError("Gmail is not authorised yet - run `referralpilot gmail-auth` once")
    credentials_file = Path(settings.gmail_credentials_file)
    if not credentials_file.exists():
        raise GmailAuthError(f"OAuth client file not found: {credentials_file} (create a Desktop OAuth client)")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(token_file, creds)
    return creds


def build_service(settings: Settings | None = None, *, interactive: bool = False):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailAuthError("Gmail support is not installed: pip install 'referralpilot[gmail]'") from exc
    creds = load_credentials(settings, interactive=interactive)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
