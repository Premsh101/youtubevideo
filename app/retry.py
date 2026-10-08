"""Quota-aware calling: rate limiting + exponential backoff for Google 429/503 errors.

Vertex projects start with low per-minute quotas (image models especially).  Instead of
failing the whole job and wasting what was already paid for, every paid call goes through
`call()`: it paces requests (IMAGE_RPM etc.) and, on RESOURCE_EXHAUSTED / UNAVAILABLE,
waits and retries with growing delays.  Only after MAX_ATTEMPTS it raises QuotaExhausted,
which the pipeline turns into a *paused* job that auto-resumes later.
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "6"))
BASE_DELAY = float(os.getenv("RETRY_BASE_SECONDS", "8"))    # 8, 16, 32, 64, 128 s (+ jitter)
MAX_DELAY = float(os.getenv("RETRY_MAX_SECONDS", "150"))

# Requests per minute we allow ourselves per model family; keep below your Vertex quota.
RPM = {
    "image": int(os.getenv("IMAGE_RPM", "5")),
    "text": int(os.getenv("TEXT_RPM", "10")),
    "veo": int(os.getenv("VEO_RPM", "2")),
    "tts": int(os.getenv("TTS_RPM", "60")),
}

_lock = threading.Lock()
_last_call: dict[str, float] = {}


class QuotaExhausted(RuntimeError):
    """Raised after all retries; the job is parked and resumed later instead of failing."""


def _is_quota_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 503):
        return True
    text = str(exc)
    return any(k in text for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "Quota exceeded", "rate limit"))


def _pace(kind: str) -> None:
    rpm = RPM.get(kind)
    if not rpm:
        return
    min_gap = 60.0 / rpm
    with _lock:
        wait = _last_call.get(kind, 0.0) + min_gap - time.monotonic()
        _last_call[kind] = max(time.monotonic(), _last_call.get(kind, 0.0) + min_gap)
    if wait > 0:
        time.sleep(wait)


def call(kind: str, fn: Callable[[], T], on_wait: Callable[[str], None] | None = None) -> T:
    """Run fn() under rate limiting; back off and retry on quota errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _pace(kind)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we inspect and re-raise below
            if not _is_quota_error(exc):
                raise
            if attempt == MAX_ATTEMPTS:
                raise QuotaExhausted(
                    f"Google quota still exhausted after {attempt} attempts ({kind}). "
                    "The job is paused and will resume automatically; nothing already generated is lost."
                ) from exc
            delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            if on_wait:
                on_wait(f"Quota limit hit ({kind}) — waiting {int(delay)} s before retry {attempt + 1}/{MAX_ATTEMPTS}")
            time.sleep(delay)
    raise AssertionError("unreachable")


_status_cb: Callable[[str], None] | None = None


def set_status_callback(cb: Callable[[str], None] | None) -> None:
    """Pipeline registers a callback so the UI shows 'waiting for quota…' live."""
    global _status_cb
    _status_cb = cb


def guarded(kind: str, fn: Callable[[], T]) -> T:
    return call(kind, fn, _status_cb)
