"""Process-wide singletons: the configured sender and its rate-limited queue."""

from __future__ import annotations

import threading

from .config import get_settings
from .outreach.queue import OutreachQueue
from .outreach.replies import ReplyChecker, build_reply_checker
from .outreach.senders import build_sender

_lock = threading.Lock()
_queue: OutreachQueue | None = None


def get_queue() -> OutreachQueue:
    global _queue
    if _queue is None:
        with _lock:
            if _queue is None:
                settings = get_settings()
                _queue = OutreachQueue(build_sender(settings), settings=settings)
    return _queue


def set_queue(queue: OutreachQueue | None) -> None:
    global _queue
    with _lock:
        _queue = queue


def new_reply_checker() -> ReplyChecker:
    return build_reply_checker(get_settings())
