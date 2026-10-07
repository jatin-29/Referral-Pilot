"""Process-wide singletons: the configured sender, its rate-limited queue and background work."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from typing import Any

from .config import get_settings
from .outreach.queue import OutreachQueue
from .outreach.replies import ReplyChecker, build_reply_checker
from .outreach.senders import build_sender

_lock = threading.Lock()
_queue: OutreachQueue | None = None
_background_runner: Callable[..., None] | None = None


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


def set_background_runner(runner: Callable[..., None] | None) -> None:
    """Replace the thread-based runner (the browser build has no threads and queues the work)."""
    global _background_runner
    _background_runner = runner


def run_to_completion(target: Callable[..., Any], *args: Any) -> None:
    result = target(*args)
    if inspect.isgenerator(result):  # stepwise jobs yield between units of work
        for _ in result:
            pass


def run_in_background(target: Callable[..., Any], *args: Any) -> None:
    """Run `target(*args)` outside the current request; generator jobs are stepped to the end."""
    if _background_runner is not None:
        _background_runner(target, *args)
        return
    threading.Thread(target=run_to_completion, args=(target, *args), daemon=True).start()
