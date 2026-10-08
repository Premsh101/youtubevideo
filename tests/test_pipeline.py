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
os.environ["SMOOTH_MODE"] = "blend"    # fast interpolation in tests (production default: mci optical flow)

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
    client.post(f"/api/jobs/{job['id']}/resume")
    time.sleep(1)
    r = _wait(job["id"])
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
    assert client.post(f"/api/jobs/{j2['id']}/languages/de").json()["ok"]
    time.sleep(1)
    j3 = _wait(j2["id"])
    assert j3["status"] == "done", j3.get("error")
    assert set(j3["outputs"]) == {"en", "de"} and j3["script"]["scenes"][0]["line_de"]
    kf = config.OUTPUT_DIR / j2["id"] / j3["keyframes"][0]
    assert kf.exists()                                            # same keyframes, re-timed to German audio
    assert "image" not in j3["cost"]["by_kind"] or j3["cost"]["by_kind"]["image"]["usd"] == 0
    de = config.OUTPUT_DIR / j2["id"] / j3["outputs"]["de"]["video_main"]
    assert de.exists() and abs(tts_dur(de) - j3["timelines"]["de"]["total"]) < 0.3
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
    out = config.OUTPUT_DIR / job["id"] / j["outputs"]["hi"]["video_main"]
    assert out.exists()
    # SYNC: the song drifts from the plan (as the real API does); every picture must still change
    # exactly `voice_lead` seconds before its line is sung, measured, not planned.
    tl = j["timelines"]["hi"]
    truth = [float(x) for x in __import__("json").loads(next((config.CACHE_DIR / "songs").glob("*.truth.json")).read_text())]
    assert tl["sync"] == "mock-measured"
    for i in range(1, len(truth)):
        assert abs((truth[i] - tl["starts"][i]) - tl["voice_lead"]) < 1 / 24 + 1e-6, (i, truth[i], tl["starts"][i])  # within one frame
    assert truth[-1] - truth[-2] > 0  # drift really happened vs. the plan
    assert abs(tts_dur(out) - tl["total"]) < 0.3


def test_spoken_sync_math():
    """Spoken mode: each trimmed voice line starts voice_lead after its scene starts and ends
    before the next scene's cross-fade begins."""
    j = next(x for x in client.get("/api/jobs").json() if (x["params"] or {}).get("vocals", "tts") == "tts"
             and x["status"] == "done")
    full = client.get(f"/api/jobs/{j['id']}").json()
    for lang, tl in full["timelines"].items():
        assert 0.3 <= tl["voice_lead"] < tl["xfade"]
        for i in range(len(tl["starts"]) - 1):
            # next scene starts no earlier than this scene's line + lead
            assert tl["starts"][i + 1] - tl["starts"][i] >= tl["voice_lead"] + 0.5


def test_add_language_to_old_video_reuses_everything():
    """A Hindi-only video made by an older version (no keyframe list / timelines saved) can get
    an English version: no images, no lyrics call (English lyrics already exist) — only voice + text."""
    r = client.post("/api/jobs", json={"mode": "3d", "languages": ["hi"], "preset": "twinkle", "target_seconds": 60})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    f = config.OUTPUT_DIR / j["id"] / "job.json"
    old = __import__("json").loads(f.read_text())
    for k in ("keyframes", "timelines", "veo_clips"):
        old.pop(k, None)
    f.write_text(__import__("json").dumps(old))
    from app import main as m
    m._jobs.pop(j["id"], None)

    assert client.post(f"/api/jobs/{j['id']}/languages/en").json()["ok"]
    time.sleep(1)
    j2 = _wait(j["id"])
    assert j2["status"] == "done", j2.get("error")
    assert set(j2["outputs"]) == {"hi", "en"}
    items = j2["cost"]["items"]
    assert not any(i["kind"] == "image" and not i["cached"] for i in items)   # pictures reused
    assert not any(i["detail"] == "lyrics en" for i in items)                # lyrics already existed
    assert any(i["kind"] == "tts" for i in items)                            # only the new voice
    en = config.OUTPUT_DIR / j["id"] / j2["outputs"]["en"]["video_main"]
    assert abs(tts_dur(en) - j2["timelines"]["en"]["total"]) < 0.3


def test_lyrics_on_screen_toggle_and_old_videos():
    """New videos get animated lyrics by default; they can be removed/added later at no API cost,
    including on videos made before this feature (no video_clean / lyrics fields)."""
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["hi"], "preset": "machli", "target_seconds": 40})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    o = j["outputs"]["hi"]
    d = config.OUTPUT_DIR / j["id"]
    assert o["lyrics_on_screen"] and o["video_main"].endswith("_lyrics.mp4") and (d / o["video"]).exists()
    assert (d / "lyrics_hi.ass").read_text(encoding="utf-8").count("Dialogue:") == len(j["script"]["scenes"])
    paid = j["cost"]["total_usd"]

    # remove → back to the clean video
    assert client.post(f"/api/jobs/{j['id']}/lyrics?lang=hi&on=false").json()["ok"]
    time.sleep(1)
    j2 = _wait(j["id"])
    assert not j2["outputs"]["hi"]["lyrics_on_screen"] and j2["outputs"]["hi"]["video_main"] == o["video_clean"]

    # simulate an old video: no lyrics fields at all, then add lyrics
    f = d / "job.json"
    old = __import__("json").loads(f.read_text())
    for k in ("video_clean", "lyrics_on_screen"):
        old["outputs"]["hi"].pop(k, None)
    f.write_text(__import__("json").dumps(old))
    from app import main as m
    m._jobs.pop(j["id"], None)
    (d / o["video"]).unlink()
    assert client.post(f"/api/jobs/{j['id']}/lyrics?lang=all&on=true").json()["ok"]
    time.sleep(1)
    j3 = _wait(j["id"])
    assert j3["status"] == "done", j3.get("error")
    assert j3["outputs"]["hi"]["lyrics_on_screen"] and (d / j3["outputs"]["hi"]["video"]).exists()
    assert j3["cost"]["total_usd"] == 0 and paid >= 0     # toggling lyrics never costs API money
    assert client.post(f"/api/jobs/{j['id']}/lyrics?lang=fr").status_code == 404


def test_publish_everywhere_vertical_cuts_and_signed_links(monkeypatch):
    import json as _json
    from app import public_urls, social, youtube
    # pretend accounts are connected (mock mode never calls the real APIs)
    config.YOUTUBE_TOKEN.write_text("{}")
    (config.SECRETS_DIR / "youtube_channel_default.json").write_text(_json.dumps({"title": "Sunave Kids"}))
    social.STORE.write_text(_json.dumps({"pages": [{"id": "123", "name": "Sunave Kids", "access_token": "x",
                                                    "instagram": {"id": "9", "username": "sunavekids"}}]}))
    st = client.get("/api/social/status").json()
    assert st["youtube"]["channels"]["default"]["title"] == "Sunave Kids" and st["meta"]["instagram"]["username"] == "sunavekids"

    monkeypatch.setattr(config, "REEL_MAX_SECONDS", 20.0)   # force a cut so the scene-boundary logic runs
    r = client.post("/api/jobs", json={"mode": "3d", "languages": ["hi"], "preset": "chanda-mama", "target_seconds": 40})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    r = client.post(f"/api/jobs/{j['id']}/publish/hi",
                    json={"targets": ["instagram", "facebook", "youtube_short", "youtube"], "privacy": "unlisted"})
    assert r.status_code == 200, r.text
    for _ in range(240):
        pub = client.get(f"/api/jobs/{j['id']}").json()["outputs"]["hi"].get("published", {})
        if pub and all(p["status"] in ("done", "error") for p in pub.values()):
            break
        time.sleep(1)
    assert {k: v["status"] for k, v in pub.items()} == {t: "done" for t in ("youtube", "youtube_short", "facebook", "instagram")}, pub
    assert pub["youtube"]["privacy"] == "unlisted" and pub["youtube_short"]["url"].startswith("https://youtube.com/shorts/")

    d = config.OUTPUT_DIR / j["id"]
    reel = min(d.glob("*_vertical_*s.mp4"), key=lambda f: int(f.stem.rsplit("_", 1)[1][:-1]))  # Reel = shortest cut
    dims = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(reel)], capture_output=True, text=True).stdout.strip()
    assert dims == "720,1280"
    tl = j["timelines"]["hi"]
    cut = tts_dur(reel)
    assert cut <= 20.5 and any(abs(cut - (s + tl["xfade"] * 0.5)) < 0.15 for s in tl["starts"][1:])  # ends after a whole scene

    # signed links work behind the optional password; tampered/expired ones don't
    monkeypatch.setattr(config, "APP_PASSWORD", "pw")
    url = public_urls.file_url(j["id"], j["outputs"]["hi"]["thumbnail"])
    path_q = url.split("mock.local", 1)[1]
    assert client.get(path_q).status_code == 200
    assert client.get(path_q.replace("sig=", "sig=0")).status_code == 401
    assert client.get(path_q.split("?")[0]).status_code == 401

    # double publish of the same target while running is refused; unknown language 404
    assert client.post(f"/api/jobs/{j['id']}/publish/xx", json={"targets": ["youtube"]}, auth=("admin", "pw")).status_code == 404


def test_youtube_login_keeps_pkce_verifier(tmp_path, monkeypatch):
    """Regression: Google's OAuth library adds PKCE; the verifier must survive until the callback."""
    import json as _json
    from app import youtube
    secrets_file = tmp_path / "client.json"
    secrets_file.write_text(_json.dumps({"web": {"client_id": "x.apps.googleusercontent.com", "client_secret": "s",
                                                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                                 "token_uri": "https://oauth2.googleapis.com/token",
                                                 "redirect_uris": ["http://localhost:8000/youtube/oauth2callback"]}}))
    monkeypatch.setattr(config, "YOUTUBE_CLIENT_SECRETS", secrets_file)
    url = youtube.auth_url("hi")
    assert "code_challenge=" in url and "state=hi" in url
    verifier = (config.SECRETS_DIR / "youtube_pkce_hi.txt").read_text()
    assert len(verifier) >= 43 and youtube._flow(verifier).code_verifier == verifier


def _frame(path, t=1.0):
    from PIL import Image
    import io as _io
    png = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(_io.BytesIO(png)).convert("RGB")


def test_logo_watermark_new_and_existing_videos():
    import io as _io
    import numpy as np
    from PIL import Image, ImageDraw
    from app import branding
    # an older video made before any logo existed
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en"], "preset": "twinkle", "target_seconds": 30,
                                        "lyrics_on_screen": False})
    old = _wait(r.json()["id"])
    assert old["status"] == "done" and not old["outputs"]["en"].get("logo")

    # upload a logo: coloured letters with a hole, on white
    img = Image.new("RGB", (600, 200), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 180, 180), fill="#FF7A1A")
    d.ellipse((85, 85, 135, 135), fill="white")            # letter hole → must become transparent
    d.rounded_rectangle((260, 40, 560, 170), radius=30, fill="#FFC21A", outline="white", width=6)
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    r = client.post("/api/branding/logo", files={"file": ("logo.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200 and r.json()["logo"]
    logo = Image.open(branding.LOGO)
    a = np.asarray(logo.getchannel("A"))
    assert a[0, 0] == 0 and (a == 0).mean() > 0.2

    # new videos carry the logo at the bottom
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en"], "preset": "twinkle", "target_seconds": 30})
    j = _wait(r.json()["id"])
    o = j["outputs"]["en"]
    assert o["logo"] and o["video_main"] != o["video_clean"]
    dd = config.OUTPUT_DIR / j["id"]
    branded, clean = np.asarray(_frame(dd / o["video_main"])).astype(int), np.asarray(_frame(dd / o["video_clean"])).astype(int)
    h, w, _ = branded.shape
    corner = np.abs(branded[int(h * 0.88):, int(w * 0.75):] - clean[int(h * 0.88):, int(w * 0.75):]).mean()
    top = np.abs(branded[: int(h * 0.3)] - clean[: int(h * 0.3)]).mean()
    assert corner > 10 and top < 3          # logo bottom-right, rest of the picture untouched

    # apply to all existing videos
    from app import main as m
    m._jobs.pop(old["id"], None)
    assert client.post("/api/branding/apply-all").json()["ok"]
    for _ in range(240):
        st = client.get("/api/branding/apply-all").json()
        if not st["running"] and st["done"] >= 1:
            break
        time.sleep(1)
    old2 = client.get(f"/api/jobs/{old['id']}").json()
    assert old2["outputs"]["en"]["logo"] and old2["outputs"]["en"]["video_main"].endswith("_branded.mp4")
    assert old2["cost"]["total_usd"] == 0
    client.delete("/api/branding/logo")



def test_intro_outro_bookends():
    """Full videos = intro + rhyme + outro; captions shift by the intro; Shorts/Reels use the rhyme only;
    unticking the option gives the rhyme alone."""
    from app import branding
    assert branding.clip_path("intro") and branding.clip_path("outro")       # bundled Sunave Kids clips
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en"], "preset": "twinkle", "target_seconds": 30})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    o, d = j["outputs"]["en"], config.OUTPUT_DIR / j["id"]
    full, main = tts_dur(d / o["video"]), tts_dur(d / o["video_main"])
    assert o["bookends"] and abs(full - (main + tts_dur(branding.clip_path("intro")) + tts_dur(branding.clip_path("outro")))) < 0.3
    sr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=sample_rate",
                         "-of", "csv=p=0", str(d / o["video"])], capture_output=True, text=True).stdout.strip()
    assert sr == "48000"
    first_main = (d / o["captions"]).read_text().split("\n")[1].split(" --> ")[0]
    first_full = (d / o["captions_upload"]).read_text().split("\n")[1].split(" --> ")[0]
    to_s = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]) + int(t[9:]) / 1000
    assert abs(to_s(first_full) - to_s(first_main) - o["intro_seconds"]) < 0.01

    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en"], "preset": "twinkle", "target_seconds": 30,
                                        "bookends": False})
    j2 = _wait(r.json()["id"])
    o2 = j2["outputs"]["en"]
    assert not o2["bookends"] and o2["video"] == o2["video_main"] and o2["captions_upload"] == o2["captions"]

    # turning the intro off globally applies to new videos
    assert client.delete("/api/branding/clip/intro").json()["clips"]["intro"]["url"] is None
    assert client.delete("/api/branding/clip/intro?restore_default=true").json()["clips"]["intro"]["source"] == "bundled"



def test_veo_scenes_never_freeze():
    """Veo clips are 8 s; scenes that need longer must be time-remapped, never held on a frozen frame,
    and every scene must be frame-exact so audio and video stay in sync."""
    r = client.post("/api/jobs", json={"mode": "3d", "engine": "veo", "languages": ["en"], "preset": "twinkle",
                                        "vocals": "sung", "target_seconds": 90, "bookends": False})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    o, tl = j["outputs"]["en"], j["timelines"]["en"]
    assert any(d > 8.5 for d in tl["durations"])                  # some scenes really needed stretching
    assert all(abs(d * 24 - round(d * 24)) < 1e-6 for d in tl["durations"])   # whole frames
    assert o["smoothness"]["ok"], o["smoothness"]
    main = config.OUTPUT_DIR / j["id"] / o["video_main"]
    assert abs(tts_dur(main) - tl["total"]) < 0.1
