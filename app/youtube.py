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
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                        include_granted_scopes="true", state=f"{slot}")
    (config.SECRETS_DIR / f"youtube_pkce_{slot}.txt").write_text(flow.code_verifier or "")
    return url


def finish_auth(code: str, state: str | None) -> str:
    slot = (state or DEFAULT_SLOT).strip() or DEFAULT_SLOT
    pkce = config.SECRETS_DIR / f"youtube_pkce_{slot}.txt"
    flow = _flow(pkce.read_text() if pkce.exists() else None)
    flow.fetch_token(code=code)
    pkce.unlink(missing_ok=True)
    _token_path(slot).write_text(flow.credentials.to_json())
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
        path.write_text(creds.to_json())
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
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:30],
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
        _status, resp = req.next_chunk()
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
