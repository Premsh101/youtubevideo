"""Signed, expiring public links to finished videos.

Facebook and Instagram fetch the video from our server themselves, so they need a URL that works
without the (optional) app password.  Links are HMAC-signed and expire, so nothing else is exposed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import config

_KEY_FILE = config.SECRETS_DIR / "url_signing_key"


def _key() -> bytes:
    if not _KEY_FILE.exists():
        _KEY_FILE.write_text(secrets.token_hex(32))
    return _KEY_FILE.read_text().strip().encode()


def _sig(path: str, exp: int) -> str:
    return hmac.new(_key(), f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def file_url(job_id: str, name: str, ttl_seconds: int = 3 * 24 * 3600) -> str:
    if not config.PUBLIC_BASE_URL.startswith("https://") and not config.MOCK_AI:
        raise RuntimeError("Set PUBLIC_BASE_URL to this app's https address (e.g. https://rhymes.example.com) "
                           "in Coolify — Facebook/Instagram download the video from there.")
    path = f"/api/jobs/{job_id}/files/{name}"
    exp = int(time.time()) + ttl_seconds
    return f"{config.PUBLIC_BASE_URL or 'https://mock.local'}{path}?exp={exp}&sig={_sig(path, exp)}"


def is_valid(path: str, exp: str | None, sig: str | None) -> bool:
    if not exp or not sig or not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(_sig(path, int(exp)), sig)
