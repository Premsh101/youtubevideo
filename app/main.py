"""FastAPI app: web UI + JSON API for generating and publishing toddler rhyme videos."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

import base64
import secrets

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import tts
from . import (branding, characters, clips, cutout, config, costs, elevenlabs, languages, pipeline, presets, public_urls, publish,
               script_gen, social, youtube)
from .costs import CostLedger

app = FastAPI(title="Toddler Rhyme Studio")


class BasicAuth(BaseHTTPMiddleware):
    """Optional: only active when APP_PASSWORD is set. Off by default (single-user app)."""

    async def dispatch(self, request: Request, call_next):
        if not config.APP_PASSWORD:
            return await call_next(request)
        # Facebook/Instagram fetch videos with a signed, expiring link (no password possible there)
        if "/files/" in request.url.path and public_urls.is_valid(
                request.url.path, request.query_params.get("exp"), request.query_params.get("sig")):
            return await call_next(request)
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.APP_USER) and secrets.compare_digest(pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Login required", 401, headers={"WWW-Authenticate": 'Basic realm="Toddler Rhyme Studio"'})
        return await call_next(request)


app.add_middleware(BasicAuth)
STATIC = Path(__file__).parent / "static"
_jobs: dict[str, pipeline.Job] = {}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def get_config() -> dict:
    return {
        "mock": config.MOCK_AI,
        "project": config.GCP_PROJECT,
        "location": config.GCP_LOCATION,
        "models": {"text": config.TEXT_MODEL, "image": config.IMAGE_MODEL, "veo": config.VEO_MODEL,
                   "tts": config.TTS_VOICES},
        "prices_usd": config.PRICES,
        "usd_to_inr": config.USD_TO_INR,
        "elevenlabs": elevenlabs.is_configured(),
        "auth": bool(config.APP_PASSWORD),
        "public_base_url": config.PUBLIC_BASE_URL,
        "reel_max": config.REEL_MAX_SECONDS,
        "youtube": {"configured": youtube.is_configured(), "authorised": bool(youtube.channels()),
                    "privacy": config.YOUTUBE_PRIVACY},
    }


# ----------------------------------------------------------------- characters
@app.get("/api/characters")
def api_characters() -> list[dict]:
    out = characters.list_characters()
    for c in out:
        c["sheet_url"] = f"/api/characters/{c['id']}/sheet.png" if c["has_sheet"] else None
        sp = next(iter(sorted((config.CHARACTER_DIR / c["id"]).glob("sprite_*.png"))), None)
        c["sprite_url"] = f"/api/characters/{c['id']}/sprite.png" if sp else None
    return out


@app.post("/api/characters/{cid}/sheet")
def api_character_sheet(cid: str, mode: Literal["2d", "3d"] = "2d", regenerate: bool = False) -> dict:
    ledger = CostLedger()
    if regenerate:
        p = characters.sheet_path(cid)
        if p:
            p.unlink()
    characters.ensure_sheet(cid, mode, ledger)
    return {"sheet_url": f"/api/characters/{cid}/sheet.png", "cost": ledger.summary()}


@app.get("/api/characters/{cid}/sheet.png")
def api_character_sheet_png(cid: str) -> FileResponse:
    p = characters.sheet_path(cid)
    if not p:
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/characters/{cid}/sprite.png")
def api_character_sprite_png(cid: str) -> FileResponse:
    sp = next(iter(sorted((config.CHARACTER_DIR / cid).glob("sprite_*.png"))), None)
    if not sp:
        raise HTTPException(404)
    return FileResponse(sp, headers={"Cache-Control": "no-store"})


@app.post("/api/characters/{cid}/sprite")
def api_character_sprite(cid: str, mode: Literal["2d", "3d"] = "3d", regenerate: bool = False) -> dict:
    """The character's animated cut-out (used by the Animated cut-outs engine). Redo it if the cut looks bad."""
    ledger = CostLedger()
    try:
        characters.get(cid)
    except KeyError:
        raise HTTPException(404)
    characters.ensure_sheet(cid, mode, ledger)
    cutout.ensure_sprite(cid, mode, ledger, force=regenerate)
    return {"sprite_url": f"/api/characters/{cid}/sprite.png", "cost": ledger.summary()}


@app.delete("/api/characters/{cid}")
def api_delete_character(cid: str) -> dict:
    characters.delete(cid)
    return {"ok": True}


@app.get("/api/languages")
def api_languages() -> list[dict]:
    return languages.listing()


@app.post("/api/jobs/{job_id}/lyrics")
def api_set_lyrics(job_id: str, on: bool = True, lang: str = "all") -> dict:
    """Add (or remove) animated on-screen lyrics on an existing video — any language, old videos too."""
    live = _jobs.get(job_id)
    if live and live.state["status"] in ("running", "waiting", "queued"):
        raise HTTPException(409, "this video is still being processed")
    try:
        job = pipeline.Job.load(job_id)
    except KeyError:
        raise HTTPException(404)
    outs = job.state.get("outputs") or {}
    langs = list(outs) if lang == "all" else [lang]
    if not langs or any(x not in outs for x in langs):
        raise HTTPException(404, "no such language version")
    for x in langs:
        if not (job.dir / outs[x]["captions"]).exists():
            raise HTTPException(409, f"captions for {x} are missing; lyrics need their timings")
    _jobs[job_id] = job
    threading.Thread(target=job.set_lyrics, args=(langs, on), daemon=True, name=f"lyrics-{job_id}").start()
    return {"ok": True, "languages": langs, "on": on}


@app.post("/api/jobs/{job_id}/languages/{lang}")
def api_add_language(job_id: str, lang: str) -> dict:
    """Voice an existing video in another language: reuses all visuals, only audio + text are new."""
    if lang not in languages.LANGUAGES:
        raise HTTPException(400, f"unsupported language {lang}")
    live = _jobs.get(job_id)
    if live and live.state["status"] in ("running", "waiting", "queued"):
        raise HTTPException(409, "this video is still being processed")
    try:
        job = pipeline.Job.load(job_id)
    except KeyError:
        raise HTTPException(404)
    if job.state.get("status") != "done" and not job.state.get("outputs"):
        raise HTTPException(409, "finish (or resume) the video first")
    _jobs[job_id] = job
    threading.Thread(target=job.add_language, args=(lang,), daemon=True, name=f"lang-{job_id}-{lang}").start()
    return {"ok": True, "job": job_id, "language": lang}


def _load_idle_job(job_id: str) -> pipeline.Job:
    live = _jobs.get(job_id)
    if live and live.state["status"] in ("running", "waiting", "queued"):
        raise HTTPException(409, "this video is still being processed")
    try:
        job = pipeline.Job.load(job_id)
    except KeyError:
        raise HTTPException(404)
    _jobs[job_id] = job
    return job


@app.post("/api/jobs/{job_id}/thumbnails")
def api_make_thumbnails(job_id: str, lang: str = "all") -> dict:
    """(Re)create the two thumbnail options from the video's keyframes. Free; works on old videos."""
    job = _load_idle_job(job_id)
    outs = job.state.get("outputs") or {}
    langs = list(outs) if lang == "all" else [lang]
    if not langs or any(x not in outs for x in langs):
        raise HTTPException(404, "no such language version")
    job.make_thumbnails(langs)
    if not any(outs[x].get("thumbnails") for x in langs):
        raise HTTPException(409, "this video's pictures are no longer on the server")
    return {"ok": True, "outputs": {x: outs[x] for x in langs}}


@app.post("/api/jobs/{job_id}/thumbnail/{lang}/{choice}")
def api_choose_thumbnail(job_id: str, lang: str, choice: str) -> dict:
    job = _load_idle_job(job_id)
    if lang not in (job.state.get("outputs") or {}):
        raise HTTPException(404)
    try:
        job.choose_thumbnail(lang, choice)
    except KeyError:
        raise HTTPException(404, "no such thumbnail option")
    return {"ok": True, "thumbnail": job.state["outputs"][lang]["thumbnail"], "choice": choice}


@app.get("/api/presets")
def api_presets() -> list[dict]:
    return [{"id": p["id"], "title": p["title"], "lang": p["lang"]} for p in presets.PRESETS]


# ----------------------------------------------------------------------- jobs
class JobIn(BaseModel):
    mode: Literal["2d", "3d"]
    engine: Literal["images", "veo", "cutout", "clips"] = "images"
    clips: list[str] = Field([], description="engine=clips: ids of uploaded clips, in the order they play")
    vocals: Literal["tts", "sung"] = "tts"
    languages: list[str] = ["en", "hi"]
    character_mode: Literal["auto", "library", "describe"] = "auto"
    characters: list[str] = Field([], description="library mode: ids of library characters to use")
    character_description: str | None = Field(None, description="describe mode: the character(s) in your words")
    topic: str | None = None
    poem: str | None = None
    preset: str | None = None
    target_seconds: int = Field(config.TARGET_SECONDS, ge=30, le=240)
    lyrics_on_screen: bool = True
    bookends: bool = True


def _clip_stats(ids: list[str]) -> tuple[int, float]:
    """(number of lyric lines, seconds of footage) for the chosen clips."""
    try:
        total = sum(clips.get(c)["duration"] for c in ids)
        return len(clips.plan_segments(ids)), total
    except KeyError:
        raise HTTPException(404, "one of the selected clips no longer exists")


@app.post("/api/estimate")
def api_estimate(body: JobIn) -> dict:
    if body.engine == "clips":
        if not body.clips:
            return {"scenes": 0, **costs.estimate(0, "clips", body.languages, vocals=body.vocals, seconds=0, clip_seconds=0)}
        n, total = _clip_stats(body.clips)
        return {"scenes": n, **costs.estimate(n, "clips", body.languages, vocals=body.vocals, seconds=int(total), clip_seconds=total)}
    n = script_gen.scene_count(body.engine, body.target_seconds)
    return {"scenes": n, **costs.estimate(n, body.engine, body.languages, vocals=body.vocals, seconds=body.target_seconds)}


# ------------------------------------------------------------------ your own clips (library)
@app.get("/api/clips")
def api_clips() -> list[dict]:
    return [{**m, "thumb_url": f"/api/clips/{m['id']}/thumb.jpg",
             "analyzed": (clips.DIR / m["id"] / "analysis.json").exists()} for m in clips.list_clips()]


@app.post("/api/clips")
async def api_upload_clip(file: UploadFile = File(...)) -> dict:
    """Upload one video into the clip library (the page sends several files one after another)."""
    import tempfile
    tmp = Path(tempfile.mkstemp(suffix=Path(file.filename or "x.mp4").suffix, dir=clips.DIR)[1])
    size = 0
    try:
        with open(tmp, "wb") as fh:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > clips.MAX_BYTES:
                    raise HTTPException(413, f"That file is larger than {clips.MAX_BYTES // (1024 * 1024)} MB")
                fh.write(chunk)
        try:
            meta = clips.add(tmp, file.filename or "clip.mp4")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    finally:
        tmp.unlink(missing_ok=True)
    return {**meta, "thumb_url": f"/api/clips/{meta['id']}/thumb.jpg", "analyzed": False}


@app.get("/api/clips/{cid}/thumb.jpg")
def api_clip_thumb(cid: str) -> FileResponse:
    try:
        clips.get(cid)
    except KeyError:
        raise HTTPException(404)
    return FileResponse(clips.thumb_path(cid))


@app.delete("/api/clips/{cid}")
def api_delete_clip(cid: str) -> dict:
    clips.delete(cid)   # videos already made keep their own copies of the footage
    return {"ok": True}


@app.post("/api/jobs")
def api_create_job(body: JobIn) -> dict:
    if not body.languages:
        raise HTTPException(400, "pick at least one language")
    unknown = [lang for lang in body.languages if lang not in languages.LANGUAGES]
    if unknown:
        raise HTTPException(400, f"unsupported language(s): {unknown}")
    if body.character_mode == "library" and not body.characters:
        raise HTTPException(400, "Pick at least one character from the library")
    if body.character_mode == "describe" and not (body.character_description or "").strip():
        raise HTTPException(400, "Describe the character first")
    if body.character_mode != "library":
        body.characters = []
    if body.vocals == "sung" and not elevenlabs.is_configured():
        raise HTTPException(400, "Sung vocals need ELEVENLABS_API_KEY on the server")
    if body.engine == "clips":
        if not body.clips:
            raise HTTPException(400, "Select at least one clip")
        n, total = _clip_stats(body.clips)
        if total > 600:
            raise HTTPException(400, f"{total:.0f} s of footage is too much for one video (max 10 minutes)")
    else:
        body.clips = []
    job = pipeline.Job(body.model_dump())
    _jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True, name=f"job-{job.id}").start()
    return job.state


@app.get("/api/jobs")
def api_jobs() -> list[dict]:
    return pipeline.list_jobs()


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    j = _jobs[job_id].state if job_id in _jobs else pipeline.load_job(job_id)
    if not j:
        raise HTTPException(404)
    return j


@app.get("/api/jobs/{job_id}/files/{name}")
def api_job_file(job_id: str, name: str, download: bool = False) -> FileResponse:
    p = (config.OUTPUT_DIR / job_id / Path(name).name)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=f"{job_id}_{p.name}" if download else None)


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: str) -> dict:
    live = _jobs.get(job_id)
    if live and live.state["status"] in ("running", "waiting", "queued"):
        return live.state
    try:
        job = pipeline.Job.resume(job_id)
    except KeyError:
        raise HTTPException(404)
    _jobs[job_id] = job
    threading.Thread(target=job.run, daemon=True, name=f"job-{job_id}").start()
    return job.state


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str) -> dict:
    live = _jobs.pop(job_id, None)
    if live and live.state["status"] in ("running", "waiting"):
        raise HTTPException(409, "job is still running")
    pipeline.delete_job(job_id)
    return {"ok": True}


# ---------------------------------------------------------------- publishing accounts
@app.get("/guide", response_class=HTMLResponse)
def guide() -> str:
    return (STATIC / "guide.html").read_text(encoding="utf-8")


@app.get("/api/branding")
def api_branding() -> dict:
    clips = {}
    for k in branding.CLIP_KINDS:
        p = branding.clip_path(k)
        clips[k] = {"url": f"/api/branding/clip/{k}.mp4" if p else None,
                    "source": None if p is None else ("bundled" if branding.BUNDLED in p.parents else "uploaded")}
    return {"logo": branding.has_logo(), "logo_url": "/api/branding/logo.png" if branding.has_logo() else None,
            "position": branding.POSITION, "channel_name": config.CHANNEL_NAME, "clips": clips}


@app.get("/api/branding/clip/{kind}.mp4")
def api_clip(kind: str) -> FileResponse:
    p = branding.clip_path(kind) if kind in branding.CLIP_KINDS else None
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@app.post("/api/branding/clip/{kind}")
async def api_upload_clip(kind: str, file: UploadFile = File(...)) -> dict:
    if kind not in branding.CLIP_KINDS:
        raise HTTPException(404)
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Clip too large (max 200 MB)")
    tmp = branding.DIR / f"_check_{kind}.mp4"
    tmp.write_bytes(data)
    try:
        if tts.media_duration(tmp) > 30:
            raise HTTPException(400, "Keep intro/outro under 30 seconds — long intros make toddlers (and YouTube) leave")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "That file isn't a readable video")
    finally:
        tmp.unlink(missing_ok=True)
    branding.save_clip(kind, data)
    return api_branding()


@app.delete("/api/branding/clip/{kind}")
def api_remove_clip(kind: str, restore_default: bool = False) -> dict:
    if kind not in branding.CLIP_KINDS:
        raise HTTPException(404)
    (branding.restore_default_clip if restore_default else branding.remove_clip)(kind)
    return api_branding()


@app.post("/api/branding/logo")
async def api_upload_logo(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "Logo file is too large (max 15 MB)")
    try:
        branding.save_logo(data)
    except Exception as exc:  # noqa: BLE001 - bad image
        raise HTTPException(400, f"Could not read that image: {exc}")
    return api_branding()


@app.get("/api/branding/logo.png")
def api_logo_png() -> FileResponse:
    if not branding.has_logo():
        raise HTTPException(404)
    return FileResponse(branding.LOGO, headers={"Cache-Control": "no-store"})


@app.delete("/api/branding/logo")
def api_delete_logo() -> dict:
    branding.remove_logo()
    return api_branding()


_rebrand_state = {"running": False, "done": 0, "total": 0}


@app.post("/api/branding/apply-all")
def api_apply_logo_all() -> dict:
    """Re-apply the current logo (or remove it, if none) on every finished video. ffmpeg only."""
    if _rebrand_state["running"]:
        raise HTTPException(409, "already applying")
    ids = [j["id"] for j in pipeline.list_jobs() if j.get("outputs") and j["status"] in ("done", "error", "paused")]
    ids = [i for i in ids if not (_jobs.get(i) and _jobs[i].state["status"] in ("running", "waiting", "queued"))]

    def _all():
        _rebrand_state.update(running=True, done=0, total=len(ids))
        try:
            for job_id in ids:
                job = pipeline.Job.load(job_id)
                _jobs[job_id] = job
                job.rebrand()
                _rebrand_state["done"] += 1
        finally:
            _rebrand_state["running"] = False
    threading.Thread(target=_all, daemon=True, name="rebrand-all").start()
    return {"ok": True, "videos": len(ids)}


@app.get("/api/branding/apply-all")
def api_apply_logo_all_status() -> dict:
    return _rebrand_state


@app.get("/api/social/status")
def api_social_status() -> dict:
    return {"youtube": {"configured": youtube.is_configured(), "channels": youtube.channels()},
            "meta": social.status(), "public_base_url": config.PUBLIC_BASE_URL}


@app.get("/youtube/auth")
def yt_auth(slot: str = youtube.DEFAULT_SLOT) -> RedirectResponse:
    if not youtube.is_configured():
        raise HTTPException(400, f"Put your OAuth client JSON at {config.YOUTUBE_CLIENT_SECRETS}")
    if slot != youtube.DEFAULT_SLOT and slot not in languages.LANGUAGES:
        raise HTTPException(400, "unknown language")
    return RedirectResponse(youtube.auth_url(slot))


@app.get("/youtube/oauth2callback")
def yt_callback(code: str, state: str | None = None) -> RedirectResponse:
    slot = youtube.finish_auth(code, state)
    return RedirectResponse(f"/?connected=youtube-{slot}")


@app.delete("/api/social/youtube/{slot}")
def yt_disconnect(slot: str) -> dict:
    youtube.disconnect(slot)
    return {"ok": True}


@app.get("/meta/auth")
def meta_auth() -> RedirectResponse:
    if not social.is_configured():
        raise HTTPException(400, "Set META_APP_ID and META_APP_SECRET first (see /guide)")
    return RedirectResponse(social.auth_url())


@app.get("/meta/oauth2callback")
def meta_callback(code: str | None = None, state: str | None = None, error_description: str | None = None):
    if not code:
        raise HTTPException(400, error_description or "Meta login was cancelled")
    social.finish_auth(code, state)
    return RedirectResponse("/?connected=meta")


@app.post("/api/social/meta/page/{page_id}")
def meta_select_page(page_id: str) -> dict:
    try:
        social.select_page(page_id)
    except KeyError:
        raise HTTPException(404)
    return social.status()


@app.delete("/api/social/meta")
def meta_disconnect() -> dict:
    social.disconnect()
    return {"ok": True}


class PublishIn(BaseModel):
    targets: list[Literal["youtube", "youtube_short", "facebook", "instagram"]] = ["youtube"]
    privacy: Literal["private", "unlisted", "public"] | None = None
    publish_at: str | None = Field(None, description="YouTube only: RFC3339 time to go public, e.g. 2026-10-10T07:30:00+05:30")


@app.post("/api/jobs/{job_id}/publish/{lang}")
def api_publish(job_id: str, lang: str, body: PublishIn) -> dict:
    """Post one language version to the chosen platforms in the background."""
    job = _jobs.get(job_id)
    if job is None:
        try:
            job = pipeline.Job.load(job_id)
        except KeyError:
            raise HTTPException(404)
        _jobs[job_id] = job
    if lang not in (job.state.get("outputs") or {}):
        raise HTTPException(404, "no such language version")
    if not body.targets:
        raise HTTPException(400, "choose at least one platform")
    if any(t.startswith("youtube") for t in body.targets) and not youtube.slot_for(lang):
        raise HTTPException(401, "Connect a YouTube channel first")
    if any(t in ("facebook", "instagram") for t in body.targets):
        st = social.status()
        if not st["connected"]:
            raise HTTPException(401, "Connect Facebook & Instagram first")
        if "instagram" in body.targets and not st["instagram"]:
            raise HTTPException(400, "The selected Facebook Page has no linked Instagram Professional account")
        if not config.PUBLIC_BASE_URL and not config.MOCK_AI:
            raise HTTPException(400, "Set PUBLIC_BASE_URL (your https app address) in Coolify first")
    pub = job.state["outputs"][lang].get("published") or {}
    busy = [t for t in body.targets if (pub.get(t) or {}).get("status") in ("queued", "uploading")]
    if busy:
        raise HTTPException(409, f"already publishing: {busy}")
    publish.start(job, lang, list(body.targets), body.privacy, body.publish_at)
    return {"ok": True, "targets": body.targets}
