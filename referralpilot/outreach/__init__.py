"""Module D: email composer, senders, rate-limited queue, follow-ups and reply detection."""

from .composer import EmailDraft, compose_followup, compose_initial
from .followups import scan_followups, scan_replies
from .queue import OutreachQueue, QueueStatus, SendPolicy, TickResult, is_paused, set_paused
from .replies import NullReplyChecker, ReplyChecker, ReplyStatus, build_reply_checker
from .senders import DryRunSender, GmailAPISender, Sender, SendError, SMTPSender, build_sender
from .service import (
    OutreachError,
    approve,
    cancel,
    create_draft,
    mark_bounced,
    mark_referred,
    mark_replied,
    opt_out,
    update_draft,
)

__all__ = [
    "DryRunSender",
    "EmailDraft",
    "GmailAPISender",
    "NullReplyChecker",
    "OutreachError",
    "OutreachQueue",
    "QueueStatus",
    "ReplyChecker",
    "ReplyStatus",
    "SMTPSender",
    "SendError",
    "SendPolicy",
    "Sender",
    "TickResult",
    "approve",
    "build_reply_checker",
    "build_sender",
    "cancel",
    "compose_followup",
    "compose_initial",
    "create_draft",
    "is_paused",
    "mark_bounced",
    "mark_referred",
    "mark_replied",
    "opt_out",
    "scan_followups",
    "scan_replies",
    "set_paused",
    "update_draft",
]
