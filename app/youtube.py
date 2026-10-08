"""One-click YouTube upload (Data API v3, OAuth2 installed/web flow).

Setup: create an OAuth client (Web application) in the same GCP project, enable
"YouTube Data API v3", add http://localhost:8000/youtube/oauth2callback as redirect
URI and save the downloaded JSON as data/secrets/youtube_client_secret.json.
Videos are flagged "made for kids" (COPPA) and default to private so you can review.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.force-ssl"]
CATEGORY_EDUCATION = "27"


def is_configured() -> bool:
    return config.YOUTUBE_CLIENT_SECRETS.exists()


def is_authorised() -> bool:
    return config.YOUTUBE_TOKEN.exists()


def _flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(str(config.YOUTUBE_CLIENT_SECRETS), scopes=SCOPES,
                                         redirect_uri=config.YOUTUBE_REDIRECT)


def auth_url() -> str:
    url, _state = _flow().authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url


def finish_auth(code: str) -> None:
    flow = _flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    config.YOUTUBE_TOKEN.write_text(creds.to_json())


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_info(json.loads(config.YOUTUBE_TOKEN.read_text()), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        config.YOUTUBE_TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video: Path, title: str, description: str, tags: list[str], lang: str,
           thumbnail: Path | None = None, captions: Path | None = None,
           privacy: str | None = None) -> dict:
    from googleapiclient.http import MediaFileUpload
    yt = _service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:30],
            "categoryId": CATEGORY_EDUCATION,
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": privacy or config.YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": True,
        },
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    vid = resp["id"]
    if thumbnail and thumbnail.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumbnail))).execute()
        except Exception:  # channel may not be verified for custom thumbnails
            pass
    if captions and captions.exists():
        try:
            yt.captions().insert(
                part="snippet",
                body={"snippet": {"videoId": vid, "language": lang, "name": "Lyrics", "isDraft": False}},
                media_body=MediaFileUpload(str(captions), mimetype="application/octet-stream"),
            ).execute()
        except Exception:
            pass
    return {"video_id": vid, "url": f"https://youtu.be/{vid}", "privacy": body["status"]["privacyStatus"]}
