"""FastAPI app: web UI + JSON API for generating and publishing toddler rhyme videos."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import characters, config, costs, pipeline, script_gen, youtube
from .costs import CostLedger

app = FastAPI(title="Toddler Rhyme Studio")
STATIC = Path(__file__).parent / "static"
_jobs: dict[str, pipeline.Job] = {}

characters.ensure_seeded()


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
        "youtube": {"configured": youtube.is_configured(), "authorised": youtube.is_authorised(),
                    "privacy": config.YOUTUBE_PRIVACY},
    }


# ----------------------------------------------------------------- characters
class CharacterIn(BaseModel):
    name: str | None = None
    species: str | None = None
    personality: str | None = None
    colours: list[str] | None = None
    signature_item: str | None = None
    brief: str | None = Field(None, description="If given, Gemini designs the character from this brief")
    mode: Literal["2d", "3d"] = "2d"
    generate_sheet: bool = True


@app.get("/api/characters")
def api_characters() -> list[dict]:
    out = characters.list_characters()
    for c in out:
        c["sheet_url"] = f"/api/characters/{c['id']}/sheet.png" if c["has_sheet"] else None
    return out


@app.post("/api/characters")
def api_create_character(body: CharacterIn) -> dict:
    ledger = CostLedger()
    if body.brief:
        c = characters.design_with_gemini(body.brief, ledger)
    else:
        if not (body.name and body.species):
            raise HTTPException(400, "name and species are required (or give a brief)")
        c = characters.save({
            "name": body.name, "species": body.species,
            "personality": body.personality or "happy and friendly",
            "colours": body.colours or ["sunshine yellow", "sky blue"],
            "signature_item": body.signature_item or "a little red bow",
        })
    if body.generate_sheet:
        characters.ensure_sheet(c["id"], body.mode, ledger)
    c["has_sheet"] = characters.sheet_path(c["id"]) is not None
    return {"character": c, "cost": ledger.summary()}


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


@app.delete("/api/characters/{cid}")
def api_delete_character(cid: str) -> dict:
    characters.delete(cid)
    return {"ok": True}


# ----------------------------------------------------------------------- jobs
class JobIn(BaseModel):
    mode: Literal["2d", "3d"]
    engine: Literal["images", "veo"] = "images"
    languages: list[Literal["en", "hi"]] = ["en", "hi"]
    characters: list[str] = []
    topic: str | None = None
    poem: str | None = None
    target_seconds: int = Field(config.TARGET_SECONDS, ge=30, le=240)


@app.post("/api/estimate")
def api_estimate(body: JobIn) -> dict:
    n = script_gen.scene_count(body.engine, body.target_seconds)
    return {"scenes": n, **costs.estimate(n, body.engine, body.languages)}


@app.post("/api/jobs")
def api_create_job(body: JobIn) -> dict:
    if not body.languages:
        raise HTTPException(400, "pick at least one language")
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


# -------------------------------------------------------------------- youtube
@app.get("/youtube/auth")
def yt_auth() -> RedirectResponse:
    if not youtube.is_configured():
        raise HTTPException(400, f"Put your OAuth client JSON at {config.YOUTUBE_CLIENT_SECRETS}")
    return RedirectResponse(youtube.auth_url())


@app.get("/youtube/oauth2callback")
def yt_callback(code: str) -> RedirectResponse:
    youtube.finish_auth(code)
    return RedirectResponse("/?youtube=connected")


class PublishIn(BaseModel):
    privacy: Literal["private", "unlisted", "public"] | None = None
    title: str | None = None
    description: str | None = None


@app.post("/api/jobs/{job_id}/youtube/{lang}")
def api_publish(job_id: str, lang: str, body: PublishIn) -> dict:
    j = _jobs[job_id].state if job_id in _jobs else pipeline.load_job(job_id)
    if not j or lang not in j["outputs"]:
        raise HTTPException(404)
    if not youtube.is_authorised():
        raise HTTPException(401, "Connect YouTube first (/youtube/auth)")
    o = j["outputs"][lang]
    d = config.OUTPUT_DIR / job_id
    desc = (body.description or o["description"]) + "\n\n#nurseryrhymes #toddlers #kidssongs"
    res = youtube.upload(d / o["video"], body.title or o["title"], desc, o["tags"], lang,
                         thumbnail=d / o["thumbnail"], captions=d / o["captions"], privacy=body.privacy)
    o["youtube"] = res
    if job_id in _jobs:
        _jobs[job_id].save()
    else:
        (d / "job.json").write_text(__import__("json").dumps(j, ensure_ascii=False, indent=1))
    return res
