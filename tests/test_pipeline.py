"""End-to-end test in MOCK_AI mode: no credentials, exercises script → render → API."""
import os
import subprocess
import time

os.environ["MOCK_AI"] = "1"
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", ".test_data"))
os.environ["VIDEO_W"] = "640"
os.environ["VIDEO_H"] = "360"
os.environ["MOCK_FAIL_429"] = "2"       # first two image calls hit a simulated quota error
os.environ["RETRY_BASE_SECONDS"] = "0.2"
os.environ["IMAGE_RPM"] = "0"          # no pacing in tests

from fastapi.testclient import TestClient  # noqa: E402

from app import config, costs, script_gen  # noqa: E402
from app.tts import media_duration as tts_dur  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_estimate_and_scene_count():
    assert script_gen.scene_count("images", 120) == 17
    assert script_gen.scene_count("veo", 120) == 15
    est = costs.estimate(10, "images", ["en", "hi"])
    assert est["total_inr"] > 0
    assert est["by_kind"]["image"]["quantity"] == 11
    veo = costs.estimate(10, "veo", ["en"])
    assert veo["total_usd"] > est["total_usd"]
    sung = costs.estimate(10, "images", ["en"], vocals="sung", seconds=120)
    assert sung["by_kind"]["sung"]["quantity"] == 2.0


def test_basic_auth_blocks_when_password_set(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", auth=("admin", "s3cret")).status_code == 200
    assert client.get("/api/config", auth=("admin", "wrong")).status_code == 401


def test_full_job_both_languages_with_auto_casting():
    assert client.get("/api/characters").json() == []  # nothing pre-made
    body = {"mode": "2d", "engine": "images", "languages": ["en", "hi"],
            "topic": "stars", "target_seconds": 60}
    job = client.post("/api/jobs", json=body).json()
    for _ in range(240):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert j["status"] == "done", j.get("error")
    assert set(j["outputs"]) == {"en", "hi"}
    assert j["cost"]["total_inr"] >= 0
    assert j["script"]["scenes"][0]["line_hi"]
    assert j["cast"] and j["cast"][0]["name"] == "Tara"       # Gemini invented a character…
    lib = client.get("/api/characters").json()
    assert len(lib) == 1 and lib[0]["has_sheet"]              # …and it was saved for reuse
    r = client.post(f"/api/characters/{lib[0]['id']}/sheet?mode=2d").json()
    assert r["cost"]["total_usd"] == 0                        # sheet is cached → free next time
    d = config.OUTPUT_DIR / job["id"]
    for lang in ("en", "hi"):
        out = d / j["outputs"][lang]["video"]
        assert out.exists() and out.stat().st_size > 10_000
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
        assert "video" in probe and "audio" in probe
        assert (d / j["outputs"][lang]["captions"]).read_text(encoding="utf-8").strip()
    r = client.get(f"/api/jobs/{job['id']}/files/{j['outputs']['en']['video']}?download=1")
    assert r.status_code == 200
    assert client.get("/api/jobs").json()[0]["id"] == job["id"]
    assert j["attempts"] == 1  # the simulated 429s were absorbed by per-call retries, not a job restart
    # resume on a finished job is a no-op that costs nothing (everything cached)
    r = client.post(f"/api/jobs/{job['id']}/resume").json()
    for _ in range(120):
        r = client.get(f"/api/jobs/{job['id']}").json()
        if r["status"] == "done":
            break
        time.sleep(1)
    assert r["status"] == "done" and r["cost"]["total_usd"] == 0
    assert r["cost"]["total_inr_all_attempts"] >= j["cost"]["total_inr"]


def _wait(job_id):
    for _ in range(240):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error", "paused"):
            return j
        time.sleep(1)
    return j


def test_character_modes_and_deletes():
    # describe mode: Gemini designs from the user's words, saves to library
    r = client.post("/api/jobs", json={"mode": "3d", "languages": ["en"], "character_mode": "describe",
                                        "character_description": "a pink bunny with a yellow scarf",
                                        "preset": "twinkle", "target_seconds": 60})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    lib = {c["id"]: c for c in client.get("/api/characters").json()}
    assert j["cast"][0]["id"] in lib and lib[j["cast"][0]["id"]]["described_by_user"]
    # library mode requires a selection, and uses exactly that character
    assert client.post("/api/jobs", json={"mode": "3d", "languages": ["en"], "character_mode": "library"}).status_code == 400
    cid = j["cast"][0]["id"]
    r = client.post("/api/jobs", json={"mode": "3d", "languages": ["en"], "character_mode": "library",
                                        "characters": [cid], "preset": "twinkle", "target_seconds": 60})
    j2 = _wait(r.json()["id"])
    assert j2["status"] == "done" and [c["id"] for c in j2["cast"]] == [cid]
    # viral metadata generated per language
    o = j2["outputs"]["en"]
    assert o["hashtags"] and all(h.startswith("#") for h in o["hashtags"]) and o["tags"]
    assert sum(len(t) + 1 for t in o["tags"]) <= 500
    # add German later: visuals reused, only lyrics + voice + metadata are new
    silent = config.OUTPUT_DIR / j2["id"] / "video_silent.mp4"
    before = silent.stat().st_mtime
    assert client.post(f"/api/jobs/{j2['id']}/languages/de").json()["ok"]
    time.sleep(1)
    j3 = _wait(j2["id"])
    assert j3["status"] == "done", j3.get("error")
    assert set(j3["outputs"]) == {"en", "de"} and j3["script"]["scenes"][0]["line_de"]
    assert silent.stat().st_mtime == before                      # no re-render of visuals
    assert "image" not in j3["cost"]["by_kind"] or j3["cost"]["by_kind"]["image"]["usd"] == 0
    de = config.OUTPUT_DIR / j2["id"] / j3["outputs"]["de"]["video"]
    assert de.exists() and abs(tts_dur(de) - j3["timeline"]["total"]) < 0.5
    assert client.post(f"/api/jobs/{j2['id']}/languages/xx").status_code == 400
    # delete video + character
    assert client.delete(f"/api/jobs/{j2['id']}").json()["ok"]
    assert client.get(f"/api/jobs/{j2['id']}").status_code == 404
    assert not (config.OUTPUT_DIR / j2["id"]).exists()
    client.delete(f"/api/characters/{cid}")
    assert cid not in [c["id"] for c in client.get("/api/characters").json()]


def test_sung_vocals_job():
    body = {"mode": "3d", "engine": "images", "languages": ["hi"], "vocals": "sung",
            "preset": "machli", "target_seconds": 60}
    job = client.post("/api/jobs", json=body).json()
    for _ in range(240):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert j["status"] == "done", j.get("error")
    assert "sung" in j["cost"]["by_kind"] and "tts" not in j["cost"]["by_kind"]
    assert (config.OUTPUT_DIR / job["id"] / j["outputs"]["hi"]["video"]).exists()
