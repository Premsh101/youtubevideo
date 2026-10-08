"""End-to-end job: script → keyframes (→ Veo clips) → voice → music → EN/HI mp4s + costs."""
from __future__ import annotations

import json
import shutil
import time
import traceback
import uuid
from pathlib import Path

from . import characters, config, render, script_gen, toddler, tts
from .costs import CostLedger
from .gemini_client import generate_image, generate_video_clip
from .music import get_music

LEAD_IN = 0.6       # silence before each line
TAIL = 1.3          # breathing room after each line (toddlers need the pause)
MIN_SCENE_SEC = 7.0


class Job:
    def __init__(self, params: dict):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = config.OUTPUT_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.params = params
        self.ledger = CostLedger()
        self.state = {
            "id": self.id, "status": "queued", "step": "queued", "progress": 0.0,
            "params": params, "script": None, "outputs": {}, "cost": None, "error": None,
            "created": time.time(),
        }
        self.save()

    # ---------------------------------------------------------------- helpers
    def save(self) -> None:
        self.state["cost"] = self.ledger.summary()
        (self.dir / "job.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def step(self, name: str, progress: float) -> None:
        self.state["step"] = name
        self.state["progress"] = round(progress, 3)
        self.save()

    # ---------------------------------------------------------------- pipeline
    def run(self) -> None:
        try:
            self.state["status"] = "running"
            self._run()
            self.state["status"] = "done"
            self.step("done", 1.0)
        except Exception as exc:  # surfaced to the UI
            self.state["status"] = "error"
            self.state["error"] = f"{exc}\n{traceback.format_exc()[-1500:]}"
            self.save()

    def _run(self) -> None:
        p = self.params
        mode = p.get("mode", "2d")
        engine = p.get("engine", "images")
        langs = p.get("languages") or ["en", "hi"]
        char_ids = p.get("characters") or [c["id"] for c in characters.list_characters()[:2]]
        chars = [characters.get(c) for c in char_ids]

        # 1. character sheets (paid once per character, reused forever)
        self.step("Preparing character reference sheets", 0.03)
        sheets = [characters.ensure_sheet(c["id"], mode, self.ledger) for c in chars]

        # 2. poem + scene plan (EN + HI in one call)
        self.step("Writing the rhyme and scene plan with Gemini", 0.08)
        script = script_gen.generate_script(p.get("topic"), p.get("poem"), chars, mode, engine,
                                            int(p.get("target_seconds") or config.TARGET_SECONDS), self.ledger)
        self.state["script"] = script
        scenes = script["scenes"]
        self.save()

        # 3. keyframes — one per scene (+1 ending frame for Veo chaining)
        n_frames = len(scenes) + (1 if engine == "veo" else 0)
        keyframes: list[Path] = []
        char_text = "; ".join(characters.describe(c) for c in chars)
        style = toddler.style_prompt(mode)
        for i in range(n_frames):
            self.step(f"Drawing keyframe {i + 1}/{n_frames}", 0.1 + 0.35 * i / n_frames)
            if i < len(scenes):
                s = scenes[i]
                visual = s["visual"]
                mood = s.get("mood_colour", "sky blue")
            else:
                visual = "The characters wave goodbye happily and settle down to rest, same place as before, soft warm evening light"
                mood = "soft purple"
            refs = list(sheets)
            continuity = ""
            if keyframes:
                refs.append(keyframes[-1])
                continuity = (" The LAST reference image is the previous scene: keep the same location, "
                              "art style, lighting and colour palette so the story feels continuous.")
            prompt = (f"{style}. Dominant colour accent: {mood}. Scene: {visual}. "
                      f"Characters: {char_text}.{continuity} Avoid: {toddler.NEGATIVE}.")
            kf = generate_image(prompt, self.ledger, f"keyframe {i + 1}", reference_images=refs)
            dest = self.dir / f"keyframe_{i:02}.png"
            shutil.copy(kf, dest)
            keyframes.append(dest)
        self.save()

        # 4. narration per language → scene durations
        voices: dict[str, list[dict]] = {}
        for li, lang in enumerate(langs):
            self.step(f"Recording {lang.upper()} narration", 0.47 + 0.06 * li)
            voices[lang] = tts.synthesize_scenes(scenes, lang, self.ledger)
        min_len = float(config.VEO_CLIP_SECONDS) if engine == "veo" else MIN_SCENE_SEC
        durations = []
        for i in range(len(scenes)):
            need = max(v[i]["duration"] for v in voices.values()) + LEAD_IN + TAIL + config.CROSSFADE_SEC
            durations.append(max(min_len, need))

        # 5. motion: Ken-Burns clips, or Veo clips chained first→last frame
        clips: list[Path] = []
        for i, s in enumerate(scenes):
            self.step(f"Animating scene {i + 1}/{len(scenes)}", 0.6 + 0.2 * i / len(scenes))
            clip = self.dir / f"clip_{i:02}.mp4"
            if engine == "veo":
                motion = (f"{style}. {s['visual']}. Camera: {s.get('camera', 'slow zoom in')}. "
                          "Very slow, gentle, smooth motion; characters move softly; no cuts.")
                raw = generate_video_clip(motion, keyframes[i], keyframes[i + 1], self.ledger, f"veo scene {i + 1}")
                render.fit_clip(raw, durations[i], clip)
            else:
                render.image_to_clip(keyframes[i], durations[i], s.get("camera", "slow zoom in"), clip)
            clips.append(clip)

        # 6. join visuals once
        self.step("Blending scenes together", 0.82)
        silent = self.dir / "video_silent.mp4"
        xf = 0.4 if engine == "veo" else config.CROSSFADE_SEC
        starts = render.crossfade_concat(clips, durations, xf, silent)
        total = starts[-1] + durations[-1]
        music = get_music(total, script["title_en"])

        # 7. per-language audio + mux + captions + thumbnail
        for li, lang in enumerate(langs):
            self.step(f"Mixing {lang.upper()} audio", 0.86 + 0.06 * li)
            audio = render.build_audio(voices[lang], starts, total, music, self.dir / f"audio_{lang}.wav", LEAD_IN)
            final = render.mux(silent, audio, self.dir / f"final_{lang}.mp4")
            srt = render.write_srt(voices[lang], starts, self.dir / f"captions_{lang}.srt", LEAD_IN)
            thumb = render.thumbnail(final, self.dir / f"thumb_{lang}.jpg", at=min(3.0, total / 2))
            self.state["outputs"][lang] = {
                "video": final.name, "captions": srt.name, "thumbnail": thumb.name,
                "duration": round(total, 1),
                "title": script.get(f"title_{lang}") or script["title_en"],
                "description": script.get(f"description_{lang}") or "",
                "tags": script.get("tags", []),
                "youtube": None,
            }
            self.save()
        for c in clips:
            c.unlink(missing_ok=True)
        self.state["keyframes"] = [k.name for k in keyframes]
        self.save()


def load_job(job_id: str) -> dict | None:
    f = config.OUTPUT_DIR / job_id / "job.json"
    return json.loads(f.read_text()) if f.exists() else None


def list_jobs() -> list[dict]:
    jobs = []
    for d in sorted(config.OUTPUT_DIR.iterdir(), reverse=True):
        j = load_job(d.name)
        if j:
            jobs.append({k: j.get(k) for k in ("id", "status", "step", "progress", "created", "cost", "outputs")}
                        | {"title": (j.get("script") or {}).get("title_en"), "params": j.get("params")})
    return jobs
