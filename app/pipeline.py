"""End-to-end job: script → keyframes (→ Veo clips) → voice → music → EN/HI mp4s + costs."""
from __future__ import annotations

import json
import shutil
import time
import traceback
import uuid
from pathlib import Path

from . import align, characters, lyrics_overlay, config, elevenlabs, languages, metadata, presets, render, retry, script_gen, toddler, tts
from .costs import CostLedger
from .gemini_client import generate_image, generate_video_clip
from .music import get_music

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

        # 4. motion source per scene (Veo clips are generated once and re-timed per language)
        self.state["keyframes"] = [k.name for k in keyframes]
        if engine == "veo":
            self._veo_clips()
        self.save()

        # 5. each language: audio first, then the pictures are cut to where that audio's lines really are
        for li, lang in enumerate(langs):
            self._produce_language(lang, 0.6 + 0.38 * li / len(langs))

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

    def _xfade(self) -> float:
        return 0.4 if self.params.get("engine") == "veo" else config.CROSSFADE_SEC

    def _voice_lead(self) -> float:
        """Line starts when the cross-fade is ~60 % done: the new picture is clearly on screen,
        but there is no visible gap before the voice (that gap read as 'video first, audio late')."""
        return round(self._xfade() * 0.6, 3)

    def _timeline_spoken(self, voices: list[dict]) -> tuple[list[float], list[float], float]:
        xf, lead = self._xfade(), self._voice_lead()
        min_len = float(config.VEO_CLIP_SECONDS) if self.params.get("engine") == "veo" else MIN_SCENE_SEC
        durations = [max(min_len, lead + v["duration"] + TAIL + xf) for v in voices]
        if self.params.get("engine") != "veo":  # follow the music's bars
            durations = render.snap_to_bars(durations, xf, toddler.MUSIC["bpm"])
        starts = render.scene_starts(durations, xf)
        return durations, starts, starts[-1] + durations[-1]

    def _timeline_sung(self, lang: str, song: Path, planned_durations: list[float]) -> tuple[list[float], list[float], float, list[float], str]:
        xf, lead = self._xfade(), self._voice_lead()
        total = tts.media_duration(song)
        planned_starts = [t + 0.4 for t in render.scene_starts(planned_durations, xf)]
        lines = [s[f"line_{lang}"] for s in self.state["script"]["scenes"]]
        line_t, method = align.line_starts(song, lines, total, planned_starts, self.ledger)
        # scene i begins `lead` before its line is sung; scene 0 always starts at 0
        bounds = [0.0] + [max(0.0, t - lead) for t in line_t[1:]]
        durations = [b2 - b1 + xf for b1, b2 in zip(bounds, bounds[1:])] + [total - bounds[-1]]
        if min(durations) < xf + 0.8:  # alignment produced a degenerate scene: fall back to the plan
            bounds = [0.0] + [max(0.0, t - lead) for t in planned_starts[1:]]
            durations = [b2 - b1 + xf for b1, b2 in zip(bounds, bounds[1:])] + [max(xf + 1, total - bounds[-1])]
            line_t, method = planned_starts, "plan"
        starts = render.scene_starts(durations, xf)
        return durations, starts, starts[-1] + durations[-1], line_t, method

    def _render_visuals(self, lang: str, durations: list[float]) -> Path:
        """Re-time the (already paid for) keyframes / Veo clips to this language's audio. ffmpeg only."""
        scenes = self.state["script"]["scenes"]
        clips: list[Path] = []
        for i, s in enumerate(scenes):
            clip = self.dir / f"clip_{lang}_{i:02}.mp4"
            if self.params.get("engine") == "veo":
                render.fit_clip(Path(self.state["veo_clips"][i]), durations[i], clip)
            else:
                render.image_to_clip(self.dir / self.state["keyframes"][i], durations[i],
                                     s.get("camera", "slow zoom in"), clip)
            clips.append(clip)
        silent = self.dir / f"video_silent_{lang}.mp4"
        render.crossfade_concat(clips, durations, self._xfade(), silent)
        for c in clips:
            c.unlink(missing_ok=True)
        return silent

    def _produce_language(self, lang: str, progress: float) -> None:
        self._ensure_lyrics(lang)
        name = languages.name(lang)
        scenes = self.state["script"]["scenes"]
        lead = self._voice_lead()
        if self.params.get("vocals") == "sung":
            self.step(f"Composing the {name} song", progress)
            per = max(MIN_SCENE_SEC, int(self.params.get("target_seconds") or config.TARGET_SECONDS) / len(scenes))
            planned = render.snap_to_bars([per] * len(scenes), self._xfade(), toddler.MUSIC["bpm"])
            song = elevenlabs.compose(scenes, planned, lang, self.params.get("mode", "2d"), self.ledger)
            self.step(f"Finding where each {name} line is sung", progress + 0.05)
            durations, starts, total, line_t, method = self._timeline_sung(lang, song, planned)
            voices = [{"path": None, "text": s[f"line_{lang}"], "chorus": bool(s.get("is_chorus")),
                       "duration": max(0.8, (line_t[i + 1] if i + 1 < len(line_t) else total) - line_t[i] - 0.3)}
                      for i, s in enumerate(scenes)]
        else:
            self.step(f"Recording {name} narration", progress)
            voices = tts.synthesize_scenes(scenes, lang, self.ledger)
            durations, starts, total = self._timeline_spoken(voices)
            song, method = None, "exact"
        self.state.setdefault("timelines", {})[lang] = {
            "durations": durations, "starts": starts, "total": total, "xfade": self._xfade(),
            "voice_lead": lead, "sync": method}
        self.step(f"Cutting the pictures to the {name} audio", progress + 0.1)
        silent = self._render_visuals(lang, durations)
        self.step(f"Mixing {name} audio", progress + 0.2)
        if song is not None:
            audio = render.song_to_track(song, total, self.dir / f"audio_{lang}.wav")
        else:
            music = get_music(total, self.state["script"]["title_en"])
            audio = render.build_audio(voices, starts, total, music, self.dir / f"audio_{lang}.wav", lead)
        final = render.mux(silent, audio, self.dir / f"final_{lang}.mp4")
        silent.unlink(missing_ok=True)
        srt = render.write_srt(voices, starts, self.dir / f"captions_{lang}.srt", lead)
        thumb = render.thumbnail(final, self.dir / f"thumb_{lang}.jpg", at=min(3.0, total / 2))
        self.step(f"Writing {name} title, description & hashtags", progress + 0.25)
        meta = metadata.viral_metadata(self.state["script"], lang, self.state.get("cast") or [], self.ledger)
        self.state["outputs"][lang] = {
            "video": final.name, "video_clean": final.name, "lyrics_on_screen": False,
            "captions": srt.name, "thumbnail": thumb.name,
            "duration": round(total, 1), "language": name, "sync": method,
            **meta, "youtube": None,
        }
        if self.params.get("lyrics_on_screen", True):
            self.step(f"Drawing animated {name} lyrics on screen", progress + 0.28)
            self._set_lyrics(lang, True)
        self.save()

    def _set_lyrics(self, lang: str, on: bool) -> None:
        """Turn the animated on-screen lyrics on/off for one language. Re-encodes the video only (ffmpeg,
        no API cost); the clean version is kept so it can be switched back. Works on old videos too."""
        o = self.state["outputs"][lang]
        clean = o.get("video_clean") or o["video"]  # old videos: their video is the clean one
        o["video_clean"] = clean
        if on:
            out = lyrics_overlay.burn(self.dir / clean, self.dir / o["captions"], lang,
                                      self.dir / clean.replace(".mp4", "_lyrics.mp4"))
            o["video"], o["lyrics_on_screen"] = out.name, True
        else:
            o["video"], o["lyrics_on_screen"] = clean, False
        self.save()

    def set_lyrics(self, langs: list[str], on: bool) -> None:
        def _do():
            for i, lang in enumerate(langs):
                self.step(f"{'Adding' if on else 'Removing'} on-screen lyrics ({languages.name(lang)})",
                          (i + 0.5) / len(langs))
                self._set_lyrics(lang, on)
        self.run(_do)

    def _veo_clips(self) -> None:
        """Veo clip per scene. Same prompt + same keyframes → content-hash cache hit, so calling this
        again for an existing video costs nothing."""
        scenes = self.state["script"]["scenes"]
        style = toddler.style_prompt(self.params.get("mode", "2d"))
        kf = [self.dir / k for k in self.state["keyframes"]]
        raw = []
        for i, s in enumerate(scenes):
            self.step(f"Animating scene {i + 1}/{len(scenes)} with Veo", 0.45 + 0.15 * i / len(scenes))
            motion = (f"{style}. {s['visual']}. Camera: {s.get('camera', 'slow zoom in')}. "
                      "Very slow, gentle, smooth motion; characters move softly; no cuts.")
            raw.append(str(generate_video_clip(motion, kf[i], kf[i + 1] if i + 1 < len(kf) else None,
                                               self.ledger, f"veo scene {i + 1}")))
        self.state["veo_clips"] = raw

    def _add_language(self, lang: str) -> None:
        """New language on an existing video: lyrics only if missing, then voice/song and YouTube text.
        Pictures are never regenerated; the existing keyframes (or Veo clips) are re-timed to the new audio.
        Works for videos made by older versions too: their keyframe files are picked up from disk."""
        if not self.state.get("script"):
            raise RuntimeError("This video has no script saved; it has to be re-created.")
        if not self.state.get("keyframes"):
            found = sorted(p.name for p in self.dir.glob("keyframe_*.png"))
            if len(found) < len(self.state["script"]["scenes"]):
                raise RuntimeError("The pictures of this video are missing on the server; it has to be re-created.")
            self.state["keyframes"] = found
        if self.params.get("engine") == "veo" and len(self.state.get("veo_clips") or []) < len(self.state["script"]["scenes"]):
            self._veo_clips()  # cache hit for clips already paid for
        self._produce_language(lang, 0.1)
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
