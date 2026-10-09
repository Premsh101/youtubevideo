"""Browser tests of the page itself (skipped if Playwright/Chromium aren't installed)."""
import glob
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

CHROME = next(iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))), None)
JOB = {"id": "20261009-000000-abcdef", "status": "done", "step": "done", "progress": 1, "created": 1, "error": None,
       "attempts": 1, "params": {"mode": "3d", "engine": "images", "vocals": "tts", "languages": ["hi"]},
       "script": {"title_en": "Machli", "title_hi": "मछली", "scenes": [{"line_en": "a", "line_hi": "b", "visual": "v"}]},
       "cast": [], "keyframes": [], "cost": {"total_inr": 1, "total_usd": 0.01, "usd_to_inr": 84, "by_kind": {}, "items": [],
                                              "total_inr_all_attempts": 1},
       "outputs": {"hi": {"video": "v.mp4", "video_main": "v.mp4", "video_clean": "v.mp4", "captions": "c.srt",
                          "thumbnail": "t.jpg", "duration": 8, "title": "Machli Jal Ki Rani Hai | Hindi Rhymes",
                          "description": "Line one\n\nLyrics \"quoted\" & <tag>", "tags": ["hindi rhymes", "balgeet"],
                          "hashtags": ["#hindirhymes", "#SunaveKids"], "youtube": None, "language": "Hindi"}}}


@pytest.fixture(scope="module")
def server():
    data = tempfile.mkdtemp()
    (Path(data) / "output" / JOB["id"]).mkdir(parents=True)
    (Path(data) / "output" / JOB["id"] / "job.json").write_text(json.dumps(JOB))
    env = dict(os.environ, MOCK_AI="1", DATA_DIR=data)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8767", "--log-level", "error"],
                           env=env, cwd=str(Path(__file__).resolve().parent.parent))
    time.sleep(4)
    yield
    srv.send_signal(signal.SIGINT)
    srv.wait(timeout=15)


@pytest.mark.skipif(not CHROME, reason="Chromium not installed")
@pytest.mark.parametrize("insecure", [False, True], ids=["https-or-localhost", "plain-http"])
def test_copy_buttons_work_even_on_plain_http(server, insecure):
    """Regression: navigator.clipboard does not exist on plain http://, so the copy buttons did nothing."""
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--host-resolver-rules=MAP fake.test 127.0.0.1"])
        ctx = b.new_context()
        url = "http://fake.test:8767/" if insecure else "http://127.0.0.1:8767/"
        if not insecure:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:8767")
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url)
        assert pg.evaluate("window.isSecureContext") is (not insecure)
        pg.click("#jobs a")
        pg.click("summary:has-text('Description')")
        pg.evaluate("(()=>{const t=document.createElement('textarea');t.id='paste';t.style.cssText='position:fixed;bottom:0;left:0;width:300px;height:80px;z-index:99999';document.body.appendChild(t)})()")
        for label, expected in (("Copy tags", "hindi rhymes, balgeet"),
                                ("Copy description + hashtags", 'Line one\n\nLyrics "quoted" & <tag>\n\n#hindirhymes #SunaveKids')):
            pg.click(f"button:has-text('{label}')")
            pg.wait_for_timeout(300)
            pg.fill("#paste", "")
            pg.click("#paste")
            pg.keyboard.press("Control+V")
            assert pg.input_value("#paste") == expected
        assert errors == []
        b.close()
