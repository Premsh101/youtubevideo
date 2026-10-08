"""End-to-end job: script → keyframes (→ Veo clips) → voice → music → EN/HI mp4s + costs."""
from __future__ import annotations

import json
import shutil
import time
import traceback
import uuid
from pathlib import Path

from . import characters, config, elevenlabs, languages, metadata, presets, render, retry, script_gen, toddler, tts
from .costs import CostLedger
from .gemini_client import generate_image, generate_video_clip
from .music import get_music

LEAD_IN = 0.6       # silence before each line
TAIL = 0.9          # breathing room after each line
MIN_SCENE_SEC = 5.0


class Job:
    PAUSE_SCHEDULE = [120, 300, 600, 900]  # seconds to wait between auto-resumes after quota exhaustion

    def __init__(self, params: dict, job_id: str | None = None):
        self.id = job_id or (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        self.dir = config.OUTPUT_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.params = params
        self.ledger = CostLedger()
        existing = load_job(self.id) if job_id else None
        self.state = existing or {
            "id": self.id, "status": "queued", "step": "queued", "progress": 0.0,
            "params": params, "script": None, "outputs": {}, "cost": None, "error": None,
            "created": time.time(), "attempts": 0, "spent_before_inr": 0.0,
        }
        if existing:  # resuming: keep what earlier attempts spent in the total
            self.params = self.state["params"]
            prev = (existing.get("cost") or {}).get("total_inr", 0.0)
            self.state["spent_before_inr"] = existing.get("spent_before_inr", 0.0) + prev
            self.state["error"] = None
        self.save()

    @classmethod
    def load(cls, job_id: str) -> "Job":
        return cls.resume(job_id)

    @classmethod
    def resume(cls, job_id: str) -> "Job":
        old = load_job(job_id)
        if not old:
            raise KeyError(job_id)
        return cls(old["params"], job_id=job_id)

    # ---------------------------------------------------------------- helpers
    def save(self) -> None:
        self.state["cost"] = self.ledger.summary()
        self.state["cost"]["total_inr_all_attempts"] = round(
            self.state["cost"]["total_inr"] + self.state.get("spent_before_inr", 0.0), 2)
        (self.dir / "job.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def step(self, name: str, progress: float) -> None:
        self.state["step"] = name
        self.state["progress"] = round(progress, 3)
        self.save()

    # ---------------------------------------------------------------- pipeline
    def run(self, target=None) -> None:
        """Run to completion. On Google quota exhaustion the job pauses and auto-resumes;
        everything already generated is cached, so a resume only pays for what is missing."""
        target = target or self._run
        retry.set_status_callback(lambda msg: self.step(msg, self.state["progress"]))
        pause_idx = 0
        while True:
            self.state["attempts"] = self.state.get("attempts", 0) + 1
            try:
                self.state["status"] = "running"
                target()
                self.state["status"] = "done"
                self.step("done", 1.0)
                return
            except retry.QuotaExhausted as exc:
                if pause_idx >= len(self.PAUSE_SCHEDULE):
                    self.state["status"] = "paused"
                    self.state["error"] = (f"{exc}\n\nAuto-resume gave up after {pause_idx} waits. Nothing is lost: "
                                           "press Resume later (all finished parts are cached and free).")
                    self.save()
                    return
                wait = self.PAUSE_SCHEDULE[pause_idx]
                pause_idx += 1
                self.state["status"] = "waiting"
                self.step(f"Google quota exhausted — paused, auto-resuming in {wait // 60} min "
                          f"(nothing already generated is lost)", self.state["progress"])
                time.sleep(wait)
            except Exception as exc:  # surfaced to the UI
                self.state["status"] = "error"
                self.state["error"] = f"{exc}\n{traceback.format_exc()[-1500:]}"
                self.save()
                return

    def _run(self) -> None:
        p = self.params
        mode = p.get("mode", "2d")
        engine = p.get("engine", "images")
        langs = p.get("languages") or ["en", "hi"]
        vocals = p.get("vocals", "tts")  # tts = spoken rhyme (cheap) | sung = ElevenLabs song
        if p.get("preset"):  # famous public-domain rhyme chosen in the UI
            pr = presets.get(p["preset"])
            if pr:
                p["poem"] = p.get("poem") or pr.get("poem")
                p["topic"] = p.get("topic") or pr.get("topic") or pr["title"]
        # 1. casting: Gemini reuses library characters that fit, invents new ones only if needed.
        #    Sheets are generated once per character and reused forever.
        if self.state.get("cast"):  # resuming: never re-roll the cast (would change script + images)
            self.step("Resuming with the same characters", 0.03)
            chars = [characters.get(c["id"]) for c in self.state["cast"]]
            for c in chars:
                characters.ensure_sheet(c["id"], mode, self.ledger)
        elif p.get("character_mode") == "describe" and p.get("character_description"):
            self.step("Gemini is designing your described character", 0.03)
            chars = characters.design_from_description(p["character_description"], mode, self.ledger)
        elif p.get("characters"):
            self.step("Preparing characters from the library", 0.03)
            chars = [characters.get(c) for c in p["characters"]]
            for c in chars:
                characters.ensure_sheet(c["id"], mode, self.ledger)
        else:
            self.step("Gemini is casting the characters", 0.03)
            chars = characters.cast_with_gemini(p.get("topic"), p.get("poem"), mode, self.ledger)
        self.state["cast"] = [{"id": c["id"], "name": c["name"], "species": c["species"]} for c in chars]
        sheets = [characters.sheet_path(c["id"]) for c in chars]

        # 2. poem + scene plan (EN + HI in one call)
        self.step("Writing the rhyme and scene plan with Gemini", 0.08)
        script = self.state.get("script") or script_gen.generate_script(p.get("topic"), p.get("poem"), chars, mode, engine,
                                            int(p.get("target_seconds") or config.TARGET_SECONDS), self.ledger)
        self.state["script"] = script
        scenes = script["scenes"]
        for lang in langs:  # en + hi come with the script; any other language is adapted by Gemini
            self._ensure_lyrics(lang)
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

        # 4. vocals per language → scene durations
        voices: dict[str, list[dict]] = {}
        songs: dict[str, Path] = {}
        min_len = float(config.VEO_CLIP_SECONDS) if engine == "veo" else MIN_SCENE_SEC
        if vocals == "sung":
            # fixed, bar-aligned scene lengths; the song is composed to that plan
            per = max(min_len, int(p.get("target_seconds") or config.TARGET_SECONDS) / len(scenes))
            durations = render.snap_to_bars([per] * len(scenes), config.CROSSFADE_SEC if engine != "veo" else 0.4,
                                            toddler.MUSIC["bpm"])
            for li, lang in enumerate(langs):
                self.step(f"Composing the {lang.upper()} song with ElevenLabs", 0.47 + 0.06 * li)
                songs[lang], voices[lang] = self._sung(lang, durations)
        else:
            for li, lang in enumerate(langs):
                self.step(f"Recording {lang.upper()} narration", 0.47 + 0.06 * li)
                voices[lang] = tts.synthesize_scenes(scenes, lang, self.ledger)
            durations = []
            for i in range(len(scenes)):
                need = max(v[i]["duration"] for v in voices.values()) + LEAD_IN + TAIL + config.CROSSFADE_SEC
                durations.append(max(min_len, need))
            if engine != "veo":  # Veo clips are a fixed 8 s; keyframe scenes can follow the beat
                durations = render.snap_to_bars(durations, config.CROSSFADE_SEC, toddler.MUSIC["bpm"])

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
        # the timeline is what lets any language be added later on top of the same visuals
        self.state["timeline"] = {"durations": durations, "starts": starts, "total": total, "xfade": xf}
        self.state["keyframes"] = [k.name for k in keyframes]
        for c in clips:
            c.unlink(missing_ok=True)
        self.save()

        # 7. per-language audio + mux + captions + thumbnail + viral metadata
        for li, lang in enumerate(langs):
            self.step(f"Mixing {languages.name(lang)} audio", 0.86 + 0.06 * li)
            self._finish_language(lang, voices[lang], songs.get(lang))

    # ------------------------------------------------------------ languages
    def _ensure_lyrics(self, lang: str) -> None:
        script = self.state["script"]
        key = f"line_{lang}"
        if all(s.get(key) for s in script["scenes"]):
            return
        self.step(f"Gemini is adapting the lyrics into {languages.name(lang)}", self.state["progress"])
        for s, line in zip(script["scenes"], metadata.translate_lines(script, lang, self.ledger)):
            s[key] = line
        self.save()

    def _sung(self, lang: str, durations: list[float]) -> tuple[Path, list[dict]]:
        scenes = self.state["script"]["scenes"]
        song = elevenlabs.compose(scenes, durations, lang, self.params.get("mode", "2d"), self.ledger)
        voices = [{"path": None, "duration": d - 1.0, "text": s[f"line_{lang}"], "chorus": bool(s.get("is_chorus"))}
                  for s, d in zip(scenes, durations)]
        return song, voices

    def _finish_language(self, lang: str, voices: list[dict], song: Path | None) -> None:
        tl = self.state["timeline"]
        starts, total = tl["starts"], tl["total"]
        silent = self.dir / "video_silent.mp4"
        if song is not None:
            audio = render.song_to_track(song, total, self.dir / f"audio_{lang}.wav")
        else:
            music = get_music(total, self.state["script"]["title_en"])  # same seed → same music in every language
            audio = render.build_audio(voices, starts, total, music, self.dir / f"audio_{lang}.wav", LEAD_IN)
        final = render.mux(silent, audio, self.dir / f"final_{lang}.mp4")
        srt = render.write_srt(voices, starts, self.dir / f"captions_{lang}.srt", LEAD_IN)
        thumb = render.thumbnail(final, self.dir / f"thumb_{lang}.jpg", at=min(3.0, total / 2))
        self.step(f"Writing {languages.name(lang)} title, description & hashtags", self.state["progress"])
        meta = metadata.viral_metadata(self.state["script"], lang, self.state.get("cast") or [], self.ledger)
        self.state["outputs"][lang] = {
            "video": final.name, "captions": srt.name, "thumbnail": thumb.name,
            "duration": round(total, 1), "language": languages.name(lang),
            **meta, "youtube": None,
        }
        self.save()

    def _add_language(self, lang: str) -> None:
        """New language on an existing video: only lyrics, voice and metadata are generated."""
        if not self.state.get("timeline") or not (self.dir / "video_silent.mp4").exists():
            raise RuntimeError("This video was made before multi-language support; re-create it once to add languages.")
        self.step(f"Adding {languages.name(lang)} using the existing visuals", 0.1)
        self._ensure_lyrics(lang)
        durations = self.state["timeline"]["durations"]
        if self.params.get("vocals") == "sung":
            self.step(f"Composing the {languages.name(lang)} song", 0.4)
            song, voices = self._sung(lang, durations)
        else:
            self.step(f"Recording {languages.name(lang)} narration", 0.4)
            song = None
            voices = tts.synthesize_scenes(self.state["script"]["scenes"], lang, self.ledger)
            # the pictures are already timed; squeeze (max 1.35x) any line that runs longer than its scene
            for i, v in enumerate(voices):
                slot = durations[i] - LEAD_IN - self.state["timeline"]["xfade"] - 0.3
                if v["duration"] > slot:
                    fitted = self.dir / f"voice_{lang}_{i:02}.wav"
                    v["path"] = str(render.speed_up(Path(v["path"]), v["duration"] / slot, fitted))
                    v["duration"] = tts.media_duration(fitted)
        self.step(f"Mixing {languages.name(lang)} audio", 0.8)
        self._finish_language(lang, voices, song)
        langs = self.params.setdefault("languages", [])
        if lang not in langs:
            langs.append(lang)

    def add_language(self, lang: str) -> None:
        self.run(lambda: self._add_language(lang))


def delete_job(job_id: str) -> None:
    shutil.rmtree(config.OUTPUT_DIR / job_id, ignore_errors=True)


def load_job(job_id: str) -> dict | None:
    f = config.OUTPUT_DIR / job_id / "job.json"
    return json.loads(f.read_text()) if f.exists() else None


def list_jobs() -> list[dict]:
    jobs = []
    for d in sorted(config.OUTPUT_DIR.iterdir(), reverse=True):
        j = load_job(d.name)
        if j:
            jobs.append({k: j.get(k) for k in ("id", "status", "step", "progress", "created", "cost", "outputs")}
                        | {"title": (j.get("script") or {}).get("title_en"), "params": j.get("params"),
                           "languages": list((j.get("outputs") or {}).keys())})
    return jobs
