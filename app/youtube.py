"""One-click YouTube upload (Data API v3, OAuth2 web flow) — regular videos and Shorts.

Setup: OAuth client (Web application) in the same GCP project, "YouTube Data API v3" enabled,
redirect URI https://YOUR-DOMAIN/youtube/oauth2callback, client JSON saved as
data/secrets/youtube_client_secret.json.  See /guide in the app for click-by-click steps.

Channels: one default channel, plus optional per-language channels (e.g. a Hindi channel and an
English channel) — uploads in a language go to that language's channel if one is connected.
Videos are always declared "made for kids" (COPPA).
"""
from __future__ import annotations

import json
import secrets
import re
import os
from pathlib import Path

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.force-ssl"]
CATEGORY_EDUCATION = "27"
DEFAULT_SLOT = "default"


def _token_path(slot: str) -> Path:
    return config.YOUTUBE_TOKEN if slot == DEFAULT_SLOT else config.SECRETS_DIR / f"youtube_token_{slot}.json"


def _info_path(slot: str) -> Path:
    return config.SECRETS_DIR / f"youtube_channel_{slot}.json"


def is_configured() -> bool:
    return config.YOUTUBE_CLIENT_SECRETS.exists() or config.MOCK_AI


def is_authorised(slot: str = DEFAULT_SLOT) -> bool:
    return _token_path(slot).exists()


def slot_for(lang: str) -> str | None:
    """Channel used for a language: its own channel if connected, else the default one."""
    if is_authorised(lang):
        return lang
    return DEFAULT_SLOT if is_authorised(DEFAULT_SLOT) else None


def channels() -> dict[str, dict]:
    out = {}
    for p in config.SECRETS_DIR.glob("youtube_channel_*.json"):
        slot = p.stem.removeprefix("youtube_channel_")
        if is_authorised(slot):
            out[slot] = json.loads(p.read_text())
    if is_authorised(DEFAULT_SLOT) and DEFAULT_SLOT not in out:
        out[DEFAULT_SLOT] = {"title": "connected"}
    return out


def disconnect(slot: str) -> None:
    _token_path(slot).unlink(missing_ok=True)
    _info_path(slot).unlink(missing_ok=True)


def _flow(code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(str(config.YOUTUBE_CLIENT_SECRETS), scopes=SCOPES,
                                         redirect_uri=config.YOUTUBE_REDIRECT)
    if code_verifier:
        flow.code_verifier = code_verifier
    return flow


def auth_url(slot: str = DEFAULT_SLOT) -> str:
    """Google sign-in URL. The PKCE verifier the library generates must survive until the callback
    (a new Flow object there would not know it → 'Missing code verifier'), so it is kept on disk."""
    flow = _flow()
    slot = _safe_slot(slot)
    nonce = secrets.token_urlsafe(16)
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                        include_granted_scopes="true", state=f"{slot}:{nonce}")
    _write_secret(config.SECRETS_DIR / f"youtube_pkce_{slot}.txt", flow.code_verifier or "")
    _write_secret(config.SECRETS_DIR / f"youtube_state_{slot}.txt", nonce)
    return url


def _clean(text: str) -> str:
    return (text or "").replace("<", "").replace(">", "")       # YouTube rejects angle brackets


def _utf8_cap(text: str, max_bytes: int) -> str:
    """YouTube's description limit is 5000 BYTES: Devanagari/Tamil are 3 bytes a character."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _tag_cap(tags: list[str], limit: int = 500) -> list[str]:
    """Total tag budget is 500 characters counting commas and the quotes around multi-word tags."""
    out, used = [], 0
    for t in tags[:40]:
        cost = len(t) + (3 if " " in t else 1)
        if used + cost > limit:
            break
        out.append(t)
        used += cost
    return out[:30]


def _safe_slot(slot: str) -> str:
    if not re.match(r"^[a-z]{2,3}$|^default$", slot or ""):
        raise ValueError("bad channel slot")
    return slot


def _write_secret(path, text: str) -> None:
    path.write_text(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def finish_auth(code: str, state: str | None) -> str:
    slot, _, nonce = (state or "").partition(":")
    slot = _safe_slot(slot or DEFAULT_SLOT)
    state_file = config.SECRETS_DIR / f"youtube_state_{slot}.txt"
    if not nonce or not state_file.exists() or not secrets.compare_digest(nonce, state_file.read_text().strip()):
        raise ValueError("sign-in state does not match (start the YouTube connection again)")
    state_file.unlink(missing_ok=True)
    pkce = config.SECRETS_DIR / f"youtube_pkce_{slot}.txt"
    flow = _flow(pkce.read_text() if pkce.exists() else None)
    flow.fetch_token(code=code)
    pkce.unlink(missing_ok=True)
    _write_secret(_token_path(slot), flow.credentials.to_json())
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=flow.credentials, cache_discovery=False)
        ch = (yt.channels().list(part="snippet", mine=True).execute().get("items") or [{}])[0]
        _info_path(slot).write_text(json.dumps({"id": ch.get("id"), "title": ch.get("snippet", {}).get("title")}))
    except Exception:  # noqa: BLE001 - name is cosmetic
        _info_path(slot).write_text(json.dumps({"title": "connected"}))
    return slot


def _service(slot: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    path = _token_path(slot)
    creds = Credentials.from_authorized_user_info(json.loads(path.read_text()), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _write_secret(path, creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video: Path, title: str, description: str, tags: list[str], lang: str,
           thumbnail: Path | None = None, captions: Path | None = None,
           privacy: str | None = None, short: bool = False, publish_at: str | None = None) -> dict:
    slot = slot_for(lang)
    if not slot:
        raise RuntimeError("Connect a YouTube channel first")
    privacy = privacy or config.YOUTUBE_PRIVACY
    if short:
        title = title if "#shorts" in title.lower() else (title[:90] + " #Shorts")
        description = description + "\n#Shorts"
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": True,
              # cartoon animation is not "realistic altered content" under YouTube's disclosure policy
              "containsSyntheticMedia": False}
    if publish_at:  # scheduled publishing: YouTube requires private + publishAt
        status.update(privacyStatus="private", publishAt=publish_at)
    body = {
        "snippet": {"title": _clean(title)[:100], "description": _utf8_cap(_clean(description), 5000), "tags": _tag_cap(tags),
                    "categoryId": CATEGORY_EDUCATION, "defaultLanguage": lang, "defaultAudioLanguage": lang},
        "status": status,
    }
    if config.MOCK_AI:
        vid = "mock" + ("short" if short else "") + lang
        url = f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}"
        return {"video_id": vid, "url": url, "privacy": status["privacyStatus"],
                "channel": slot, "short": short, "publish_at": publish_at}

    from googleapiclient.http import MediaFileUpload
    yt = _service(slot)
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk(num_retries=5)     # 5xx / network blips resume from the last byte
    vid = resp["id"]
    if thumbnail and thumbnail.exists() and not short:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumbnail))).execute()
        except Exception:  # noqa: BLE001 - needs a phone-verified channel
            pass
    if captions and captions.exists():
        try:
            yt.captions().insert(
                part="snippet",
                body={"snippet": {"videoId": vid, "language": lang, "name": "Lyrics", "isDraft": False}},
                media_body=MediaFileUpload(str(captions), mimetype="application/octet-stream"),
            ).execute()
        except Exception:  # noqa: BLE001
            pass
    url = f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}"
    return {"video_id": vid, "url": url, "privacy": status["privacyStatus"], "channel": slot,
            "short": short, "publish_at": publish_at}
