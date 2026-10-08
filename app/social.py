"""Facebook Page + Instagram (Reels) publishing via the Meta Graph API.

Setup (click-by-click in /guide): a Meta developer app of type "Business" with Facebook Login for
Business, redirect URI https://YOUR-DOMAIN/meta/oauth2callback, env META_APP_ID / META_APP_SECRET
(and META_LOGIN_CONFIG_ID if you created a login configuration).  The Instagram account must be a
Professional account linked to the Facebook Page.  The app can stay in Development mode: you are
its admin and only post to your own Page/Instagram, so no App Review is needed.

Meta downloads the video from our server, so PUBLIC_BASE_URL must be the app's https address.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.parse

import httpx

from . import config, retry

SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management",
          "instagram_basic", "instagram_content_publish"]
STORE = config.SECRETS_DIR / "meta.json"


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{path.lstrip('/')}"


def is_configured() -> bool:
    return bool(config.META_APP_ID and config.META_APP_SECRET) or config.MOCK_AI


def _load() -> dict:
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def status() -> dict:
    d = _load()
    page = selected_page(d)
    return {
        "configured": is_configured(),
        "connected": bool(page),
        "page": {"id": page["id"], "name": page["name"]} if page else None,
        "instagram": page.get("instagram") if page else None,
        "pages": [{"id": p["id"], "name": p["name"], "instagram": p.get("instagram")} for p in d.get("pages", [])],
    }


def selected_page(d: dict | None = None) -> dict | None:
    d = d if d is not None else _load()
    pages = d.get("pages") or []
    want = d.get("selected") or config.META_PAGE_ID
    return next((p for p in pages if p["id"] == want), None) or (pages[0] if pages else None)


def select_page(page_id: str) -> None:
    d = _load()
    if not any(p["id"] == page_id for p in d.get("pages", [])):
        raise KeyError(page_id)
    d["selected"] = page_id
    _save(d)


def disconnect() -> None:
    STORE.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- OAuth
def auth_url() -> str:
    state = secrets.token_urlsafe(16)
    (config.SECRETS_DIR / "meta_state.txt").write_text(state)
    params = {"client_id": config.META_APP_ID, "redirect_uri": config.META_REDIRECT, "state": state,
              "response_type": "code"}
    if config.META_LOGIN_CONFIG_ID:  # Facebook Login for Business uses a configuration instead of scopes
        params["config_id"] = config.META_LOGIN_CONFIG_ID
    else:
        params["scope"] = ",".join(SCOPES)
    return f"https://www.facebook.com/{config.META_GRAPH_VERSION}/dialog/oauth?" + urllib.parse.urlencode(params)


def finish_auth(code: str, state: str | None) -> dict:
    expected = config.SECRETS_DIR / "meta_state.txt"
    if not expected.exists() or not state or not secrets.compare_digest(expected.read_text(), state):
        raise RuntimeError("Login state mismatch — start again from 'Connect Facebook & Instagram'")
    expected.unlink(missing_ok=True)
    with httpx.Client(timeout=60) as http:
        short = http.get(_graph("oauth/access_token"), params={
            "client_id": config.META_APP_ID, "client_secret": config.META_APP_SECRET,
            "redirect_uri": config.META_REDIRECT, "code": code}).json()
        if "access_token" not in short:
            raise RuntimeError(f"Meta login failed: {short}")
        # long-lived user token → Page tokens derived from it do not expire
        long = http.get(_graph("oauth/access_token"), params={
            "grant_type": "fb_exchange_token", "client_id": config.META_APP_ID,
            "client_secret": config.META_APP_SECRET, "fb_exchange_token": short["access_token"]}).json()
        user_token = long.get("access_token", short["access_token"])
        accounts = http.get(_graph("me/accounts"), params={
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account{id,username}"}).json()
    pages = []
    for p in accounts.get("data", []):
        ig = p.get("instagram_business_account")
        pages.append({"id": p["id"], "name": p["name"], "access_token": p["access_token"],
                      "instagram": {"id": ig["id"], "username": ig.get("username")} if ig else None})
    if not pages:
        raise RuntimeError("No Facebook Pages were shared with the app. Run Connect again and tick your Page "
                           "(and its Instagram account) in the Meta dialog.")
    d = _load()
    d.update(pages=pages, connected_at=int(time.time()))
    # prefer a Page that has Instagram linked
    if not d.get("selected"):
        d["selected"] = next((p["id"] for p in pages if p["instagram"]), pages[0]["id"])
    _save(d)
    return status()


# --------------------------------------------------------------------------- publishing
def _check(r: httpx.Response) -> dict:
    data = r.json() if r.content else {}
    if r.status_code in (429, 503) or (data.get("error", {}).get("code") in (4, 17, 32, 613)):
        err = RuntimeError(f"Meta rate limit: {data}")
        err.status_code = 429
        raise err
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Meta API error: {data.get('error', data)}")
    return data


def post_facebook_video(video_url: str, title: str, description: str) -> dict:
    page = selected_page()
    if not page:
        raise RuntimeError("Connect Facebook & Instagram first")
    if config.MOCK_AI:
        return {"id": "mockfb", "url": f"https://www.facebook.com/{page['id']}/videos/mockfb", "page": page["name"]}

    def _post():
        with httpx.Client(timeout=600) as http:
            return _check(http.post(
                f"https://graph-video.facebook.com/{config.META_GRAPH_VERSION}/{page['id']}/videos",
                data={"file_url": video_url, "title": title[:250], "description": description[:5000],
                      "access_token": page["access_token"]}))
    data = retry.guarded("social", _post)
    vid = data["id"]
    return {"id": vid, "url": f"https://www.facebook.com/{page['id']}/videos/{vid}", "page": page["name"]}


def post_instagram_reel(video_url: str, caption: str, cover_url: str | None = None) -> dict:
    page = selected_page()
    ig = (page or {}).get("instagram")
    if not ig:
        raise RuntimeError("No Instagram Professional account is linked to the selected Facebook Page")
    if config.MOCK_AI:
        return {"id": "mockig", "url": "https://www.instagram.com/reel/mockig/", "account": ig.get("username")}
    token = page["access_token"]
    with httpx.Client(timeout=120) as http:
        params = {"media_type": "REELS", "video_url": video_url, "caption": caption[:2200],
                  "share_to_feed": "true", "access_token": token}
        if cover_url:
            params["cover_url"] = cover_url
        container = retry.guarded("social", lambda: _check(http.post(_graph(f"{ig['id']}/media"), data=params)))["id"]
        # Instagram downloads and processes the video; poll until it is ready (usually 30 s - 3 min)
        for _ in range(60):
            st = _check(http.get(_graph(container), params={"fields": "status_code,status", "access_token": token}))
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram could not process the video: {st.get('status')}")
            time.sleep(10)
        else:
            raise RuntimeError("Instagram is still processing after 10 minutes; try again later")
        media_id = retry.guarded("social", lambda: _check(http.post(
            _graph(f"{ig['id']}/media_publish"), data={"creation_id": container, "access_token": token})))["id"]
        link = _check(http.get(_graph(media_id), params={"fields": "permalink", "access_token": token}))
    return {"id": media_id, "url": link.get("permalink", "https://www.instagram.com/"), "account": ig.get("username")}
