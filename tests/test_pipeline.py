"""End-to-end test in MOCK_AI mode: no credentials, exercises script → render → API."""
import os
import subprocess
import time

os.environ["MOCK_AI"] = "1"
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", ".test_data"))
os.environ["VIDEO_W"] = "640"
os.environ["VIDEO_H"] = "360"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, costs, script_gen  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_estimate_and_scene_count():
    assert script_gen.scene_count("images", 120) == 10
    assert script_gen.scene_count("veo", 120) == 15
    est = costs.estimate(10, "images", ["en", "hi"])
    assert est["total_inr"] > 0
    assert est["by_kind"]["image"]["quantity"] == 11
    veo = costs.estimate(10, "veo", ["en"])
    assert veo["total_usd"] > est["total_usd"]


def test_characters_seeded_and_sheet():
    cs = client.get("/api/characters").json()
    assert len(cs) >= 4
    r = client.post(f"/api/characters/{cs[0]['id']}/sheet?mode=2d").json()
    assert r["sheet_url"].endswith("sheet.png")
    # second call is a cache hit → free
    r2 = client.post(f"/api/characters/{cs[0]['id']}/sheet?mode=2d").json()
    assert r2["cost"]["total_usd"] == 0


def test_full_job_both_languages():
    cs = client.get("/api/characters").json()
    body = {"mode": "2d", "engine": "images", "languages": ["en", "hi"],
            "characters": [cs[0]["id"], cs[1]["id"]], "topic": "stars", "target_seconds": 60}
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
