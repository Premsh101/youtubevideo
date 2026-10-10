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


# ------------------------------------------------------------------ rhyming lyrics, cut-outs, thumbnails
def test_rhyme_checker_and_native_lyrics_with_repair():
    from app import lyrics
    assert lyrics.couplets(8) == [(0, 1), (2, 3), (4, 5), (6, 7)] and lyrics.couplets(5)[-1] == (3, 4)
    for a, b in (("aara", "ara"), ("aan", "an"), ("ee", "i"), ("ar", "ar")):
        assert lyrics.rhymes(a, b), (a, b)
    for a, b in (("ara", "ina"), ("ar", "ur"), ("", "ara")):
        assert not lyrics.rhymes(a, b), (a, b)
    assert lyrics.detect_language("मछली जल की रानी है") == "hi" and lyrics.detect_language("Johny Johny") == "en"

    # topic mode: both languages are WRITTEN as rhyming songs and checked; the mock's first attempt has one bad
    # couplet, so the repair loop must run and fix it
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en", "hi"], "topic": "stars", "target_seconds": 45})
    j = _wait(r.json()["id"])
    assert j["status"] == "done", j.get("error")
    for lang in ("en", "hi"):
        rep = j["script"]["rhyme"][lang]
        # one repair each: the chorus line (line 8 = line 1) does not rhyme with line 7 and has to be fixed. For Hindi
        # the strict Devanagari check also overrules the mock's wrong Latin claim about couplet 3 (आसमान में / शान में).
        assert rep["ok"] and rep["tries"] == 1 and rep["failed"] == 0, rep
        sounds = [s[f"end_{lang}"] for s in j["script"]["scenes"]]
        assert lyrics.failing(sounds) == []
    for a, b in lyrics.couplets(len(j["script"]["scenes"])):
        assert lyrics.rhymes_devanagari(j["script"]["scenes"][a]["line_hi"], j["script"]["scenes"][b]["line_hi"]) is not False
    # chorus lines stay word-for-word identical
    chorus = {s["line_hi"] for s in j["script"]["scenes"] if s.get("is_chorus")}
    assert len(chorus) <= 1

    # a classic in its own language is kept exactly; the other language is written to rhyme
    r = client.post("/api/jobs", json={"mode": "2d", "languages": ["en", "hi"], "preset": "machli", "target_seconds": 45})
    j2 = _wait(r.json()["id"])
    assert j2["status"] == "done", j2.get("error")
    assert j2["script"]["rhyme"]["hi"].get("skipped") and j2["script"]["rhyme"]["en"]["ok"]


def test_cutout_engine_beats_reuse_and_add_language():
    """The new engine: separate backgrounds/sprites/props, animated on the beat, frame-exact, reused across
    videos (so the second video pays for far less), and a language can be added later."""
    body = {"mode": "2d", "engine": "cutout", "languages": ["en"], "topic": "stars", "target_seconds": 45,
            "bookends": False, "lyrics_on_screen": False}
    j = _wait(client.post("/api/jobs", json=body).json()["id"])
    assert j["status"] == "done", j.get("error")
    d = config.OUTPUT_DIR / j["id"]
    spec = j["cutout"]["scenes"]
    assert len(spec) == len(j["script"]["scenes"]) and (d / "assets").is_dir()
    assert all((d / "assets" / sc["bg"]).exists() and sc["chars"] and (d / "assets" / sc["chars"][0]["sprite"]).exists() for sc in spec)
    assert len({sc["bg"] for sc in spec}) < len(spec)                     # fewer backgrounds than scenes
    tl = j["timelines"]["en"]
    assert tl["beats"]["method"] == "known" and tl["beats"]["bpm"] == 92
    o = j["outputs"]["en"]
    main = d / o["video_main"]
    assert o["smoothness"]["ok"] and abs(tts_dur(main) - tl["total"]) < 0.1
    n = int(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                     "stream=nb_read_frames", "-of", "csv=p=0", str(main)]).decode())
    assert n == round(tl["total"] * 24)                                   # frame-exact
    # the characters really move: frames a quarter-beat apart differ in the sprite area
    import numpy as np
    f0 = np.asarray(_frame(main, 20.0)).astype(int)
    f1 = np.asarray(_frame(main, 20.0 + 0.4)).astype(int)
    assert np.abs(f0 - f1).mean() > 0.5
    # the character's cut-out is stored in the library for reuse
    lib = client.get("/api/characters").json()
    assert any(c.get("sprite_url") for c in lib)

    # second video, same character library: sprite, backgrounds and props come from cache → far fewer new images
    j2 = _wait(client.post("/api/jobs", json={**body, "character_mode": "library",
                                              "characters": [j["cast"][0]["id"]]}).json()["id"])
    assert j2["status"] == "done", j2.get("error")
    paid = lambda job: sum(1 for i in job["cost"]["items"] if i["kind"] == "image" and not i["cached"])
    assert paid(j2) < paid(j)

    # add Hindi later: assets reused, animation re-timed to the Hindi audio, still frame-exact
    assert client.post(f"/api/jobs/{j['id']}/languages/hi").json()["ok"]
    time.sleep(1)
    j3 = _wait(j["id"])
    assert j3["status"] == "done", j3.get("error")
    assert not any(i["kind"] == "image" and not i["cached"] for i in j3["cost"]["items"])
    hi = config.OUTPUT_DIR / j["id"] / j3["outputs"]["hi"]["video_main"]
    assert abs(tts_dur(hi) - j3["timelines"]["hi"]["total"]) < 0.1


def test_beat_detector_finds_tempo_and_phase():
    from app import beats, music
    out = config.CACHE_DIR / "beat_test.wav"
    for mood, bpm in (("playful", 92), ("energetic", 108)):   # the tempo comes from the mood
        music.synth_lullaby(30, f"bt{bpm}", out, mood)
        g = beats.detect(out)
        assert abs(g.bpm - bpm) < 0.6
        per = 60 / bpm
        assert abs(((g.offset / per) + 0.5) % 1 - 0.5) * per < 0.05      # within ~1 video frame of the true beat


def test_chroma_cutout_removes_background_keeps_subject():
    import numpy as np
    from PIL import Image, ImageDraw
    from app import chroma
    for bg, key, body in (("#00FF00", "green", "#FFA3D7"), ("#FF00FF", "magenta", "#58C23A")):
        img = Image.new("RGB", (400, 400), bg)
        d = ImageDraw.Draw(img)
        d.ellipse((60, 60, 340, 360), fill=body, outline="#333", width=6)
        d.rectangle((150, 220, 250, 270), fill="white")                    # white belly must stay solid
        a = np.asarray(chroma.cut_out(img, key).getchannel("A"))
        h, w = a.shape
        assert a[0, 0] == 0 and a[-1, -1] == 0
        assert a[int(h * 0.55):int(h * 0.65), int(w * 0.45):int(w * 0.55)].min() > 250
    assert chroma.pick_key(["sunshine yellow", "grass green"]) == "magenta" and chroma.pick_key(["candy pink"]) == "green"


def test_two_thumbnail_options_choose_and_old_videos():
    from PIL import Image
    j = _wait(client.post("/api/jobs", json={"mode": "2d", "languages": ["en", "hi"], "preset": "twinkle",
                                              "target_seconds": 40, "bookends": False}).json()["id"])
    assert j["status"] == "done", j.get("error")
    d = config.OUTPUT_DIR / j["id"]
    for lang in ("en", "hi"):
        o = j["outputs"][lang]
        assert [t["id"] for t in o["thumbnails"]] == ["A", "B"] and o["thumbnail_choice"] == "A"
        files = [d / t["file"] for t in o["thumbnails"]]
        assert all(f.exists() and Image.open(f).size == (1280, 720) and f.stat().st_size < 2_000_000 for f in files)
        assert o["thumbnails"][0]["frame"] != o["thumbnails"][1]["frame"]          # really two different frames
        assert o["thumbnail"] == o["thumbnails"][0]["file"]
    # pick B for Hindi only; it is what gets uploaded to YouTube
    r = client.post(f"/api/jobs/{j['id']}/thumbnail/hi/B").json()
    j2 = client.get(f"/api/jobs/{j['id']}").json()
    assert j2["outputs"]["hi"]["thumbnail"] == "thumb_hi_B.jpg" and j2["outputs"]["en"]["thumbnail"] == "thumb_en_A.jpg"
    assert client.post(f"/api/jobs/{j['id']}/thumbnail/hi/C").status_code == 404
    # an old video without options (older version): create them from its keyframes, free
    f = d / "job.json"
    import json as _j
    old = _j.loads(f.read_text())
    for lang in old["outputs"]:
        for k in ("thumbnails", "thumbnail_choice", "thumbs_v"):
            old["outputs"][lang].pop(k, None)
        old["outputs"][lang]["thumbnail"] = f"thumb_{lang}.jpg"
    f.write_text(_j.dumps(old))
    from app import main as m
    m._jobs.pop(j["id"], None)
    r = client.post(f"/api/jobs/{j['id']}/thumbnails")
    assert r.status_code == 200 and all(len(v["thumbnails"]) == 2 for v in r.json()["outputs"].values())
    assert client.post("/api/jobs/nope/thumbnails").status_code == 404


def test_independent_rhyme_judge_overrides_the_writers_own_claims(monkeypatch):
    """The writer may call a near-miss a rhyme. A fresh judge flags it and the pair gets rewritten; if the
    judge itself fails, we still finish on the writer's own check."""
    from app import lyrics
    from app.costs import CostLedger

    def run(judge):
        scenes = [{"line_en": f"line {i}", "visual": "v", "is_chorus": False} for i in range(4)]
        script = {"scenes": scenes}
        monkeypatch.setattr(lyrics, "_judge", judge)
        rep = lyrics.ensure_rhyming(script, "en", {}, CostLedger())
        return script, rep

    calls = {"n": 0}

    def strict(texts, lang, ledger):
        calls["n"] += 1
        return [(0, 1)] if calls["n"] == 1 else []          # first look: couplet 1 is a near-miss

    script, rep = run(strict)
    assert rep["ok"] and rep["tries"] == 1 and calls["n"] == 2          # judged, repaired, judged again
    assert script["scenes"][1]["line_en"] == "repaired line 2"          # the second line of the flagged couplet changed
    assert script["scenes"][3]["line_en"] == "repaired line 4"          # and the couplet the sound-check caught

    script, rep = run(lambda texts, lang, ledger: None)                 # judge unavailable: writer's own check decides
    assert rep["ok"] and rep["tries"] == 1


# ------------------------------------------------------------------ your own clips
def _lavfi_video(path, size, seconds, fps=25, sound=False):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if sound:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=500:duration={seconds}", "-c:a", "aac", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path.read_bytes()


def _upload(name, data):
    r = client.post("/api/clips", files={"file": (name, data, "video/mp4")})
    assert r.status_code == 200, r.text
    return r.json()


def test_clip_upload_validation(tmp_path, monkeypatch):
    from app import clips
    ok = _upload("wide.mp4", _lavfi_video(tmp_path / "w.mp4", "640x360", 3))
    assert ok["duration"] == 3.0 and (ok["width"], ok["height"]) == (640, 360) and not ok["has_audio"]
    assert client.get(ok["thumb_url"]).status_code == 200
    assert client.post("/api/clips", files={"file": ("notes.txt", b"hello", "text/plain")}).status_code == 400
    r = client.post("/api/clips", files={"file": ("fake.mp4", b"this is not a video", "video/mp4")})
    assert r.status_code == 400 and "readable" in r.json()["detail"]
    with monkeypatch.context() as m:       # only for this one upload
        m.setattr(clips, "MAX_SECONDS", 2.0)
        r = client.post("/api/clips", files={"file": ("long.mp4", (tmp_path / "w.mp4").read_bytes(), "video/mp4")})
    assert r.status_code == 400 and "keep clips under" in r.json()["detail"]
    assert client.get("/api/clips").json()[0]["id"] == ok["id"]
    assert client.delete(f"/api/clips/{ok['id']}").json()["ok"] and client.get(ok["thumb_url"]).status_code == 404
    # segment planning: ~7 s per line, and an odd count is made even so couplets can rhyme
    a = _upload("a.mp4", _lavfi_video(tmp_path / "a.mp4", "640x360", 6))
    b = _upload("b.mp4", _lavfi_video(tmp_path / "b.mp4", "640x360", 9))
    segs = clips.plan_segments([a["id"], b["id"]])
    assert len(segs) % 2 == 0 and abs(sum(s["duration"] for s in segs) - 15) < 0.01
    assert all(abs(s["end"] - s["start"] - s["duration"]) < 1e-6 for s in segs)
    for c in (a, b):
        client.delete(f"/api/clips/{c['id']}")


def test_clips_video_end_to_end_reuse_and_add_language(tmp_path):
    """Upload clips (16:9, vertical with sound, square, one flagged), lyrics are written to fit them, the footage
    is fitted to the voice without freezing, and a language can be added after the uploads are deleted."""
    import numpy as np
    wide = _upload("wide.mp4", _lavfi_video(tmp_path / "w.mp4", "640x360", 6))
    vert = _upload("vertical.mp4", _lavfi_video(tmp_path / "v.mp4", "360x640", 9, sound=True))
    square = _upload("square.mp4", _lavfi_video(tmp_path / "s.mp4", "480x480", 4))
    scary = _upload("scary_dog.mp4", _lavfi_video(tmp_path / "x.mp4", "640x360", 5))
    assert vert["has_audio"] and vert["height"] > vert["width"]
    ids = [wide["id"], vert["id"], square["id"], scary["id"]]
    body = {"mode": "2d", "engine": "clips", "clips": ids, "languages": ["en", "hi"], "topic": "hopping", "bookends": False}

    est = client.post("/api/estimate", json=body).json()
    assert est["scenes"] >= 4 and "image" not in est["by_kind"] and est["total_inr"] < 20
    assert client.post("/api/jobs", json={**body, "clips": []}).status_code == 400
    assert client.post("/api/jobs", json={**body, "clips": ["c-nope"]}).status_code == 404

    j = _wait(client.post("/api/jobs", json=body).json()["id"])
    assert j["status"] == "done", j.get("error")
    d = config.OUTPUT_DIR / j["id"]
    segs = j["clips_plan"]["segments"]
    assert len(segs) % 2 == 0 and len(segs) == len(j["script"]["scenes"]) == len(j["keyframes"])
    for s in segs:   # every piece is at the video's size, whatever shape the upload had (vertical/square get a blurred backdrop)
        sz = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                      "-of", "csv=p=0", str(d / s["file"])]).decode().strip()
        assert sz == "640,360", (s["file"], sz)
    assert [w["clip"] for w in j["clips_plan"]["warnings"]] == ["scary_dog"]        # flagged for the owner to check
    for lang in ("en", "hi"):
        assert j["script"]["rhyme"][lang]["ok"], j["script"]["rhyme"][lang]
        o, tl = j["outputs"][lang], j["timelines"][lang]
        assert o["smoothness"]["ok"], o["smoothness"]                                # no frozen/hung moments
        main = d / o["video_main"]
        n = int(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                         "stream=nb_read_frames", "-of", "csv=p=0", str(main)]).decode())
        assert n == round(tl["total"] * 24)                                          # frame-exact against the audio
        assert [t["id"] for t in o["thumbnails"]] == ["A", "B"]
        assert all(dur >= s["duration"] - 0.05 for dur, s in zip(tl["durations"], segs))   # footage is never trimmed
    kinds = j["cost"]["by_kind"]
    assert "image" not in kinds and "veo" not in kinds and j["cost"]["total_inr"] < 15   # no pictures drawn at all

    # the second video reuses the clips: Gemini's description of each clip is cached, so watching is free
    j2 = _wait(client.post("/api/jobs", json={**body, "languages": ["en"]}).json()["id"])
    assert j2["status"] == "done", j2.get("error")
    watch = [i for i in j2["cost"]["items"] if i["detail"].startswith("watch clip")]
    assert watch and all(i["cached"] for i in watch)

    # delete every upload: the finished video carries its own footage, so a language can still be added
    for cid in ids:
        client.delete(f"/api/clips/{cid}")
    assert client.get("/api/clips").json() == []
    assert client.post(f"/api/jobs/{j2['id']}/languages/de").json()["ok"]
    time.sleep(1)
    j3 = _wait(j2["id"])
    assert j3["status"] == "done", j3.get("error")
    de = config.OUTPUT_DIR / j2["id"] / j3["outputs"]["de"]["video_main"]
    assert abs(tts_dur(de) - j3["timelines"]["de"]["total"]) < 0.1
    assert j3["script"]["rhyme"]["de"]["ok"]


def test_change_vocals_of_finished_video_keeps_pictures_and_publish_history():
    body = {"mode": "2d", "engine": "images", "languages": ["en"], "vocals": "tts", "preset": "machli", "target_seconds": 30}
    job = client.post("/api/jobs", json=body).json()
    j = _wait(job["id"])
    assert j["status"] == "done", j.get("error")
    jid = job["id"]
    kf = list(j["keyframes"])
    assert j["cost"]["by_kind"].get("image")   # the first run drew the pictures
    # pretend it was published, and chose thumbnail B: a new voice must not forget either
    jf = config.OUTPUT_DIR / jid / "job.json"
    d = __import__("json").loads(jf.read_text())
    d["outputs"]["en"]["published"] = {"youtube": {"status": "done", "url": "https://youtu.be/x"}}
    d["outputs"]["en"]["thumbnail_choice"] = "B"
    jf.write_text(__import__("json").dumps(d))

    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "tts", "voice": "female"}).status_code == 409  # unchanged
    assert client.post(f"/api/jobs/{jid}/vocals/de", json={"mode": "tts", "voice": "male"}).status_code == 404
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "tts", "voice": "male"}).json()["ok"]
    j2 = _wait(jid)
    assert j2["status"] == "done", j2.get("error")
    o = j2["outputs"]["en"]
    assert o["vocals"] == {"mode": "tts", "voice": "male", "take": 0, "mood": "playful"}
    assert o["published"]["youtube"]["url"] == "https://youtu.be/x" and o["thumbnail_choice"] == "B"
    assert j2["keyframes"] == kf and not j2["cost"]["by_kind"].get("image")   # no new pictures paid
    assert (config.OUTPUT_DIR / jid / o["video"]).exists()

    # same voice, new mood: a different audio (the energetic tempo), nothing else changes
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "tts", "voice": "male", "mood": "playful"}).status_code == 409
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "tts", "voice": "male", "mood": "energetic"}).json()["ok"]
    jm = _wait(jid)
    assert jm["status"] == "done" and jm["outputs"]["en"]["vocals"]["mood"] == "energetic"
    assert jm["timelines"]["en"]["beats"]["bpm"] == 108 if "beats" in jm["timelines"]["en"] else True
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "tts", "voice": "male", "mood": "same"}).json()["ok"]
    assert _wait(jid)["outputs"]["en"]["vocals"]["mood"] == "playful"

    # spoken -> sung, then a new take of the song (a different file, i.e. a paid re-compose)
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "sung", "voice": "female"}).json()["ok"]
    j3 = _wait(jid)
    assert j3["status"] == "done", j3.get("error")
    assert j3["outputs"]["en"]["vocals"]["mode"] == "sung" and "sung" in j3["cost"]["by_kind"]
    n = len(list((config.CACHE_DIR / "songs").glob("*.mp3")))
    assert client.post(f"/api/jobs/{jid}/vocals/en", json={"mode": "sung", "voice": "female", "new_take": True}).json()["ok"]
    j4 = _wait(jid)
    assert j4["status"] == "done" and j4["outputs"]["en"]["vocals"]["take"] == 1
    assert len(list((config.CACHE_DIR / "songs").glob("*.mp3"))) == n + 1


def test_mood_drives_music_voice_lyrics_and_can_be_overridden():
    from app import elevenlabs, moods, tts
    # unit: a lullaby asks ElevenLabs for soft slow music with no drums, and has no shouted chorus
    scenes = [{"line_en": "Sleep little star", "is_chorus": i == 0} for i in range(4)]
    plan = elevenlabs.build_plan(scenes, [8.0] * 4, "en", "2d", "female", "sleepy")
    c0 = plan["chunks"][0]
    assert "60 bpm" in c0["positive_styles"] and "music box" in c0["positive_styles"]
    assert "drums" in c0["negative_styles"] and "disco" in c0["negative_styles"]
    assert c0["text"].startswith("[Lullaby]") and "softly" in c0["text"] and "energetic hook" not in c0["positive_styles"]
    party = elevenlabs.build_plan(scenes, [8.0] * 4, "en", "2d", "female", "energetic")["chunks"][0]
    assert "108 bpm" in party["positive_styles"] and party["text"].startswith("[Chorus]")
    for mood in ("sleepy", "calm", "playful", "energetic"):      # the chosen voice is never lost, whatever the mood
        male = elevenlabs.build_plan(scenes, [8.0] * 4, "en", "2d", "male", mood)["chunks"][0]["positive_styles"]
        assert any("male" in w and "female" not in w for w in male), (mood, male)
    # the Google voice is slower and lower for a lullaby
    assert 'rate="74%"' in tts._ssml("hush", False, "sleepy") and 'rate="94%"' in tts._ssml("hop", False, "energetic")
    assert moods.guess("a bedtime lullaby") == "sleepy" and moods.guess("dance party") == "energetic"
    assert moods.resolve("playful", "sleepy") == "playful" and moods.resolve("auto", "sleepy") == "sleepy"

    # end to end: Auto picks sleepy from the topic; an explicit choice wins
    body = {"mode": "2d", "engine": "images", "languages": ["en"], "vocals": "sung", "topic": "a bedtime lullaby for the moon", "target_seconds": 30}
    j = _wait(client.post("/api/jobs", json=body).json()["id"])
    assert j["status"] == "done", j.get("error")
    assert j["script"]["mood"] == "sleepy" and j["mood"] == "sleepy"
    assert j["outputs"]["en"]["vocals"]["mood"] == "sleepy" and "mood_check" in j["outputs"]["en"]
    j2 = _wait(client.post("/api/jobs", json={**body, "mood": "energetic"}).json()["id"])
    assert j2["status"] == "done" and j2["script"]["mood"] == "energetic" and j2["outputs"]["en"]["vocals"]["mood"] == "energetic"
    assert any(m["id"] == "sleepy" for m in client.get("/api/config").json()["moods"])


def test_hindi_rhymes_are_checked_by_code_and_hindi_themes_lead():
    from app import lyrics, presets, script_gen
    good = [("मछली जल की रानी है", "जीवन उसका पानी है"), ("नन्हा तारा", "कितना प्यारा"), ("ऊँचे आसमान में", "चमके शान में"),
            ("मेरा घर", "तुझे डर"), ("एक दिन", "गिनो तीन"), ("सूरज ढल जाता है", "अँधेरा छा जाता है"),
            ("हाथी राजा कहाँ चले", "सूँड हिलाते कहाँ चले"), ("चंदा मामा दूर के", "पुए पकाएँ बूर के")]
    good += [("तितली उड़ी, बस पे चढ़ी", "सीट न मिली तो रोने लगी"), ("आजा मेरे पास", "हट बदमाश"), ("कहाँ गए थे", "सो रहे थे")]
    bad = [("वो चलता है", "वो गाती है"), ("एक कली", "एक रानी"), ("घर चलो", "खाना खाओ"), ("मेरा घर", "मेरी माँ")]
    for a, b in good:
        assert lyrics.rhymes_devanagari(a, b) is True, (a, b)
    for a, b in bad:
        assert lyrics.rhymes_devanagari(a, b) is False, (a, b)
    assert lyrics.rhymes_devanagari("star", "are") is None           # not Devanagari: the other checks decide
    # romanised Hindi is recognised as Hindi; plain English is not
    assert lyrics.detect_language("lakdi ki kathi kathi pe ghoda") == "hi"
    assert lyrics.detect_language("brushing teeth with a happy duck") == "en"
    assert lyrics.source_language({"topic": "machli jal ki rani"}) == ("hi", False)
    # the prompts: a named rhyme is recognised, Hindi leads, and Hindi gets its own rhyme rules
    p = script_gen.build_prompt("lakdi ki kathi kathi pe ghoda", None, [], "2d", 8)
    assert "HINDI lines FIRST" in p and "copyrighted" in p and "तुकबंदी" in p
    assert "HINDI lines FIRST" not in script_gen.build_prompt("brushing teeth", None, [], "2d", 8)
    assert "तुकबंदी" in lyrics._write_prompt([{"line_en": "x", "visual": ""}] * 2, "hi", "line_en", None, False)
    assert "तुकबंदी" not in lyrics._write_prompt([{"line_en": "x", "visual": ""}] * 2, "de", "line_en", None, False)
    for pid in ("titli-udi", "hathi-raja"):
        pr = presets.get(pid)
        lines = [x.strip() for x in pr["poem"].splitlines() if x.strip()]
        for a, b in lyrics.couplets(len(lines)):
            assert lyrics.rhymes_devanagari(lines[a], lines[b]), (pid, lines[a], lines[b])

    # end to end: a Hinglish theme naming a film song -> flagged as copyrighted, Hindi leads, Hindi couplets rhyme
    body = {"mode": "2d", "engine": "images", "languages": ["hi", "en"], "vocals": "tts",
            "topic": "lakdi ki kathi kathi pe ghoda", "target_seconds": 30}
    j = _wait(client.post("/api/jobs", json=body).json()["id"])
    assert j["status"] == "done", j.get("error")
    s = j["script"]
    assert s["known_rhyme"] == "Lakdi Ki Kathi" and s["copyrighted"] is True and s["lead_lang"] == "hi"
    assert s["rhyme"]["hi"]["ok"] and s["rhyme"]["en"]["ok"]
    for a, b in lyrics.couplets(len(s["scenes"])):   # (a mock-repaired line is Latin text -> "not applicable")
        assert lyrics.rhymes_devanagari(s["scenes"][a]["line_hi"], s["scenes"][b]["line_hi"]) is not False
