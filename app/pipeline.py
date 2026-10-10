"""End-to-end job: script → keyframes (→ Veo clips) → voice → music → EN/HI mp4s + costs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback
import uuid
from pathlib import Path

from . import align, beats, branding, characters, clips, cutout, lyrics, moods, lyrics_overlay, thumbnails, config, elevenlabs, languages, metadata, presets, render, retry, script_gen, toddler, tts
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
        if self.params.get("engine") == "clips":   # footage the user uploaded: no drawing, lyrics are written to fit it
            return self._run_clips()
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
                                            int(p.get("target_seconds") or config.TARGET_SECONDS), self.ledger, p.get("mood"))
        self.state["script"] = script
        scenes = script["scenes"]
        for lang in langs:  # en + hi come with the script; any other language is adapted by Gemini
            self._ensure_lyrics(lang)
        self.save()

        # 3. keyframes — one per scene (+1 ending frame for Veo chaining)
        n_frames = len(scenes) + (1 if engine == "veo" else 0)
        keyframes: list[Path] = []
        if engine == "cutout":   # backgrounds + cut-outs instead of finished pictures; stills double as keyframes
            keyframes = self._build_cutout(chars, mode)
            n_frames = 0
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
            look = moods.get(self._video_mood())["visual"]   # e.g. night-time moonlit blues for a lullaby
            prompt = (f"{style}.{f' {look}.' if look else ''} Dominant colour accent: {mood}. Scene: {visual}. "
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
        """Lyrics of this language: written natively so the couplets rhyme (not translated line by line),
        then checked; done once per language (script["rhyme"][lang] records the result)."""
        script = self.state["script"]
        if lang in (script.get("rhyme") or {}):
            return
        self.step(f"Writing the {languages.name(lang)} lyrics so they rhyme", self.state["progress"])
        report = lyrics.ensure_rhyming(script, lang, self.params, self.ledger)
        self.step(f"{languages.name(lang)} lyrics: " + ("every couplet rhymes" if report["ok"] else
                  f"{report['failed']} of {report['couplets']} couplets could not be made to rhyme"), self.state["progress"])
        self.save()

    def _xfade(self) -> float:
        """Cross-fade length, a whole number of frames (so scene starts stay on frame boundaries)."""
        xf = 0.4 if self.params.get("engine") in ("veo", "clips") else config.CROSSFADE_SEC   # short fades keep real footage on screen
        return round(xf * config.FPS) / config.FPS

    def _voice_lead(self) -> float:
        """Line starts when the cross-fade is ~60 % done: the new picture is clearly on screen,
        but there is no visible gap before the voice (that gap read as 'video first, audio late')."""
        return round(self._xfade() * 0.6, 3)

    def _timeline_spoken(self, voices: list[dict], lang: str | None = None) -> tuple[list[float], list[float], float]:
        xf, lead = self._xfade(), self._voice_lead()
        min_len = float(config.VEO_CLIP_SECONDS) if self.params.get("engine") == "veo" else MIN_SCENE_SEC
        natural = self._natural_lengths()   # your footage: never shorter than the clip piece itself (no trimming)
        durations = [max(natural[i] if natural else min_len, lead + v["duration"] + TAIL + xf) for i, v in enumerate(voices)]
        if self.params.get("engine") not in ("veo", "clips"):  # follow the music's bars
            durations = render.snap_to_bars(durations, xf, self._mood(lang)["bpm"])
        durations = render.frame_exact(durations, xf, config.FPS)
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
        durations = render.frame_exact(durations, xf, config.FPS)
        starts = render.scene_starts(durations, xf)
        return durations, starts, starts[-1] + durations[-1], line_t, method

    # ------------------------------------------------------------ your own clips
    def _natural_lengths(self) -> list[float] | None:
        plan = self.state.get("clips_plan")
        if self.params.get("engine") != "clips" or not plan:
            return None
        return [max(4.5, s["duration"]) for s in plan["segments"]]

    def _build_segments(self, ids: list[str], analyses: list[dict]) -> dict:
        """Cut every clip into lyric-sized pieces inside the job folder (so the job survives deleting the upload)."""
        segs = clips.plan_segments(ids)
        by_id = {cid: a for cid, a in zip(ids, analyses)}
        seg_dir = self.dir / "segments"
        seg_dir.mkdir(exist_ok=True)
        for i, s in enumerate(segs):
            s["file"] = f"segments/seg_{i:02}.mp4"
            s["describe"] = clips.describe_segment(s, by_id[s["clip"]])
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(len(segs), os.cpu_count() or 2))) as pool:
            list(pool.map(lambda i: clips.cut_segment(segs[i]["clip"], segs[i]["start"], segs[i]["end"],
                                                      self.dir / segs[i]["file"], config.VIDEO_W, config.VIDEO_H, config.FPS),
                          range(len(segs))))
        warnings, seen = [], set()
        for cid, a in zip(ids, analyses):
            if cid in seen:
                continue
            seen.add(cid)
            name = clips.get(cid)["name"]
            if a.get("kid_safe") is False:
                warnings.append({"clip": name, "note": a.get("kid_safe_notes") or "may not be suitable for toddlers"})
            if a.get("has_text_or_logo"):
                warnings.append({"clip": name, "note": "contains text or a logo (another channel's brand?)"})
        return {"segments": segs, "warnings": warnings}

    def _run_clips(self) -> None:
        p = self.params
        langs = p.get("languages") or ["en", "hi"]
        ids = p.get("clips") or []
        if not ids and not self.state.get("clips_plan"):
            raise RuntimeError("No clips were selected")
        self.state["cast"] = []
        if not self.state.get("clips_plan"):
            analyses = []
            for k, cid in enumerate(ids):
                self.step(f"Gemini is watching clip {k + 1}/{len(ids)}", 0.03 + 0.12 * k / len(ids))
                analyses.append(clips.analyze(cid, self.ledger))
            self.step("Cutting your clips into pieces that fit the lyrics", 0.17)
            self.state["clips_plan"] = self._build_segments(ids, analyses)
            self.save()
        plan = self.state["clips_plan"]
        self.step("Writing lyrics that fit your footage", 0.25)
        script = self.state.get("script") or script_gen.generate_clip_script(plan["segments"], p.get("topic"), p.get("poem"), self.ledger, p.get("mood"))
        self.state["script"] = script
        self.save()
        for lang in langs:
            self._ensure_lyrics(lang)
        keyframes = []
        for i, s in enumerate(plan["segments"]):   # a frame from each piece: thumbnails are made from these
            dest = self.dir / f"keyframe_{i:02}.png"
            if not dest.exists():
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{s['duration'] * 0.4:.2f}", "-i",
                                str(self.dir / s["file"]), "-frames:v", "1", str(dest)], check=True)
            keyframes.append(dest.name)
        self.state["keyframes"] = keyframes
        self.save()
        for li, lang in enumerate(langs):
            self._produce_language(lang, 0.35 + 0.6 * li / len(langs))

    def _render_clips(self, lang: str, durations: list[float]) -> Path:
        """Fit each piece of footage to the time its lyric line needs (speed up / slow down smoothly, never a freeze)."""
        segs = self.state["clips_plan"]["segments"]
        outs = [self.dir / f"clip_{lang}_{i:02}.mp4" for i in range(len(segs))]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(len(segs), os.cpu_count() or 2))) as pool:
            list(pool.map(lambda i: render.fit_clip(self.dir / segs[i]["file"], durations[i], outs[i]), range(len(segs))))
        silent = self.dir / f"video_silent_{lang}.mp4"
        render.crossfade_concat(outs, durations, self._xfade(), silent)
        for c in outs:
            c.unlink(missing_ok=True)
        return silent

    def _build_cutout(self, chars: list[dict], mode: str) -> list[Path]:
        """Animated cut-outs engine: fetch / draw backgrounds, cut-out sprites and props (library-cached),
        then save each scene at rest as its keyframe (used for thumbnails)."""
        self.state["cutout"] = cutout.prepare(self.dir, self.state["script"], chars, mode, self.id, self.ledger,
                                              self.step)
        self.save()
        out = []
        for i, sc in enumerate(self.state["cutout"]["scenes"]):
            dest = self.dir / f"keyframe_{i:02}.png"
            cutout.still(sc, self.dir / "assets", config.VIDEO_W, config.VIDEO_H).save(dest)
            out.append(dest)
        return out

    def _beat_grid(self, lang: str, total: float, song: Path | None) -> beats.Grid:
        """When the music hits, in this language's video: known for our own lullaby, measured otherwise."""
        bpm = self._mood(lang)["bpm"]
        rng = {"lo": bpm * 0.75, "hi": bpm * 1.35}      # songs follow the mood's tempo roughly; a lullaby is below 70
        if song is not None:
            return beats.detect(song, **rng)
        music = get_music(total, self.state["script"]["title_en"], self._mood_id(lang))
        if config.CACHE_DIR / "music" in music.parents:   # our synthesised lullaby: exact tempo, starts on the beat
            return beats.Grid(bpm=bpm, offset=0.0, method="known")
        return beats.detect(music, **rng)

    def _render_cutout(self, lang: str, durations: list[float], song: Path | None) -> Path:
        scenes = self.state["script"]["scenes"]
        tl = self.state["timelines"][lang]
        grid = self._beat_grid(lang, tl["total"], song)
        tl["beats"] = {"bpm": round(grid.bpm, 2), "offset": round(grid.offset, 3), "method": grid.method}
        W, H, fps = config.VIDEO_W, config.VIDEO_H, config.FPS
        clips = [self.dir / f"clip_{lang}_{i:02}.mp4" for i in range(len(scenes))]
        lead, lines, files = tl["voice_lead"], tl["line_seconds"], tl["voice_files"]

        def one(i: int) -> None:
            cutout.render_scene_clip(self.state["cutout"]["scenes"][i], self.dir / "assets", W, H, fps,
                                     round(durations[i] * fps), grid, tl["starts"][i], lead, lines[i], files[i], i, clips[i])
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(len(scenes), os.cpu_count() or 2))) as pool:
            list(pool.map(one, range(len(scenes))))
        silent = self.dir / f"video_silent_{lang}.mp4"
        render.crossfade_concat(clips, durations, self._xfade(), silent)
        for c in clips:
            c.unlink(missing_ok=True)
        return silent

    def _render_visuals(self, lang: str, durations: list[float], song: Path | None = None) -> Path:
        """Re-time the (already paid for) keyframes / Veo clips to this language's audio. ffmpeg only."""
        if self.params.get("engine") == "cutout":
            return self._render_cutout(lang, durations, song)
        if self.params.get("engine") == "clips":
            return self._render_clips(lang, durations)
        scenes = self.state["script"]["scenes"]
        clips = [self.dir / f"clip_{lang}_{i:02}.mp4" for i in range(len(scenes))]

        def one(i: int) -> None:
            if self.params.get("engine") == "veo":
                render.fit_clip(Path(self.state["veo_clips"][i]), durations[i], clips[i])
            else:
                render.image_to_clip(self.dir / self.state["keyframes"][i], durations[i],
                                     scenes[i].get("camera", "slow zoom in"), clips[i])
        # scenes are independent ffmpeg jobs: use all CPU cores (motion interpolation is heavy)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(len(scenes), os.cpu_count() or 2))) as pool:
            list(pool.map(one, range(len(scenes))))
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
        vocal = self._vocal(lang)
        md = moods.get(vocal["mood"])
        self.state["mood"] = self._video_mood()
        previous = dict((self.state.get("outputs") or {}).get(lang) or {})
        if vocal["mode"] == "sung":
            self.step(f"Composing the {name} song", progress)
            natural = self._natural_lengths()
            if natural:   # footage keeps its own length; the song is composed to it
                planned = [round(d * config.FPS) / config.FPS for d in natural]
            else:
                per = max(MIN_SCENE_SEC, int(self.params.get("target_seconds") or config.TARGET_SECONDS) / len(scenes))
                planned = render.snap_to_bars([per] * len(scenes), self._xfade(), md["bpm"])
            song = elevenlabs.compose(scenes, planned, lang, self.params.get("mode", "2d"), self.ledger,
                                      voice=vocal["voice"], take=vocal["take"], mood=vocal["mood"])
            self.step(f"Finding where each {name} line is sung", progress + 0.05)
            durations, starts, total, line_t, method = self._timeline_sung(lang, song, planned)
            voices = [{"path": None, "text": s[f"line_{lang}"], "chorus": bool(s.get("is_chorus")),
                       "duration": max(0.8, (line_t[i + 1] if i + 1 < len(line_t) else total) - line_t[i] - 0.3)}
                      for i, s in enumerate(scenes)]
        else:
            self.step(f"Recording {name} narration", progress)
            voices = tts.synthesize_scenes(scenes, lang, self.ledger, voice=vocal["voice"], mood=vocal["mood"])
            durations, starts, total = self._timeline_spoken(voices, lang)
            song, method = None, "exact"
        self.state.setdefault("timelines", {})[lang] = {
            "durations": durations, "starts": starts, "total": total, "xfade": self._xfade(),
            "voice_lead": lead, "sync": method,
            "line_seconds": [round(v["duration"], 3) for v in voices], "voice_files": [v["path"] for v in voices]}
        self.step(f"Cutting the pictures to the {name} audio" if self.params.get("engine") != "cutout"
                  else f"Animating the characters to the {name} audio", progress + 0.1)
        silent = self._render_visuals(lang, durations, song)
        self.step(f"Mixing {name} audio", progress + 0.2)
        if song is not None:
            audio = render.song_to_track(song, total, self.dir / f"audio_{lang}.wav")
        else:
            music = get_music(total, self.state["script"]["title_en"], vocal["mood"])
            audio = render.build_audio(voices, starts, total, music, self.dir / f"audio_{lang}.wav", lead,
                                       music_db=md["volume_db"], harmony=md["id"] not in ("sleepy", "calm"))
        final = render.mux(silent, audio, self.dir / f"final_{lang}.mp4")
        silent.unlink(missing_ok=True)
        srt = render.write_srt(voices, starts, self.dir / f"captions_{lang}.srt", lead)
        thumb = render.thumbnail(final, self.dir / f"thumb_{lang}.jpg", at=min(3.0, total / 2))
        self.step(f"Writing {name} title, description & hashtags", progress + 0.25)
        meta = metadata.viral_metadata(self.state["script"], lang, self.state.get("cast") or [], self.ledger)
        self.state["outputs"][lang] = {
            "video": final.name, "video_clean": final.name, "lyrics_on_screen": bool(self.params.get("lyrics_on_screen", True)),
            "captions": srt.name, "thumbnail": thumb.name,
            "duration": round(total, 1), "language": name, "sync": method, "vocals": vocal,
            "mood_check": self._mood_check(song if song is not None else music, vocal["mood"]),
            **meta, "youtube": None,
        }
        for k in ("youtube", "published", "thumbnail_choice"):   # a new voice must not forget what was already published
            if previous.get(k) is not None:
                self.state["outputs"][lang][k] = previous[k]
        if "lyrics_on_screen" in previous:
            self.state["outputs"][lang]["lyrics_on_screen"] = previous["lyrics_on_screen"]
        for k in ("title", "description", "tags", "hashtags", "title_en"):   # keep the text you may have edited/published
            if previous.get(k):
                self.state["outputs"][lang][k] = previous[k]
        self.step(f"Adding {name} lyrics & logo", progress + 0.28)
        self._rebuild_output(lang)

    def _rebuild_output(self, lang: str) -> None:
        """Final look of one language from its clean video: on-screen lyrics (if on) + logo watermark
        (if a logo is uploaded). ffmpeg only, no API cost. Works on videos from any older version."""
        o = self.state["outputs"][lang]
        clean = o.get("video_clean") or o["video"]  # old videos: their video is the clean one
        o["video_clean"] = clean
        lyrics, logo = bool(o.get("lyrics_on_screen")), branding.LOGO if branding.has_logo() else None
        for stale in self.dir.glob(f"{Path(clean).stem}_*vertical_*.mp4"):  # Shorts/Reels are re-cut on demand
            stale.unlink()
        if not lyrics and logo is None:
            main = clean
        else:
            out = self.dir / clean.replace(".mp4", "_lyrics.mp4" if lyrics else "_branded.mp4")
            lyrics_overlay.finalize(self.dir / clean, self.dir / o["captions"] if lyrics else None, lang, logo, out)
            main = out.name
        o["video_main"] = main       # rhyme only: used for Shorts/Reels (no intro/outro there)
        frozen = render.freezes(self.dir / main)
        o["smoothness"] = {"frozen": frozen, "ok": not frozen}
        o["logo"] = logo is not None
        # channel intro + outro around the full video (YouTube, Facebook, download)
        intro, outro = branding.clip_path("intro"), branding.clip_path("outro")
        if o.get("bookends", self.params.get("bookends", True)) and (intro or outro):
            full = self.dir / main.replace(".mp4", "_full.mp4")
            offset = render.bookend(self.dir / main, intro, outro, full, self.state["script"]["title_en"])
            o["video"], o["intro_seconds"], o["bookends"] = full.name, round(offset, 3), True
            o["captions_upload"] = render.shift_srt(self.dir / o["captions"], offset,
                                                    self.dir / f"captions_{lang}_full.srt").name
        else:
            o["video"], o["intro_seconds"], o["bookends"] = main, 0.0, False
            o["captions_upload"] = o["captions"]
        o["duration_total"] = round(tts.media_duration(self.dir / o["video"]), 1)
        self._make_thumbnails(lang)
        self.save()

    def _make_thumbnails(self, lang: str) -> None:
        """Two thumbnail options from the keyframes (free). Keeps the user's choice; works for old videos too."""
        o = self.state["outputs"][lang]
        frames = [self.dir / k for k in (self.state.get("keyframes") or []) if (self.dir / k).exists()]
        frames = frames or sorted(self.dir.glob("keyframe_*.png"))
        if not frames:
            return
        title = o.get("title") or (self.state.get("script") or {}).get(f"title_{lang}") or ""
        o["thumbnails"] = thumbnails.build(frames, title, lang, self.dir, branding.LOGO if branding.has_logo() else None)
        ids = [t["id"] for t in o["thumbnails"]]
        o["thumbnail_choice"] = o.get("thumbnail_choice") if o.get("thumbnail_choice") in ids else "A"
        o["thumbnail"] = next(t["file"] for t in o["thumbnails"] if t["id"] == o["thumbnail_choice"])
        o["thumbs_v"] = int(time.time())

    def make_thumbnails(self, langs: list[str] | None = None) -> None:
        for lang in langs or list((self.state.get("outputs") or {})):
            self._make_thumbnails(lang)
        self.save()

    def choose_thumbnail(self, lang: str, choice: str) -> None:
        o = self.state["outputs"][lang]
        pick = next((t for t in o.get("thumbnails") or [] if t["id"] == choice), None)
        if not pick:
            raise KeyError(choice)
        o["thumbnail_choice"], o["thumbnail"] = choice, pick["file"]
        self.save()

    def _set_lyrics(self, lang: str, on: bool) -> None:
        self.state["outputs"][lang]["lyrics_on_screen"] = on
        self._rebuild_output(lang)

    def rebrand(self) -> None:
        """Re-apply the current logo (and lyrics setting) to every language of this video."""
        def _do():
            langs = list(self.state.get("outputs") or {})
            for i, lang in enumerate(langs):
                self.step(f"Applying logo ({languages.name(lang)})", (i + 0.5) / max(1, len(langs)))
                self._rebuild_output(lang)
        self.run(_do)

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
        if self.params.get("engine") == "clips" and not self.state.get("clips_plan"):
            raise RuntimeError("This video has no saved footage pieces; it has to be re-created.")
        if self.params.get("engine") == "cutout" and not self.state.get("cutout"):
            raise RuntimeError("This video has no saved animation data; it has to be re-created.")
        if self.params.get("engine") == "veo" and len(self.state.get("veo_clips") or []) < len(self.state["script"]["scenes"]):
            self._veo_clips()  # cache hit for clips already paid for
        self._produce_language(lang, 0.1)
        langs = self.params.setdefault("languages", [])
        if lang not in langs:
            langs.append(lang)

    def add_language(self, lang: str) -> None:
        self.run(lambda: self._add_language(lang))

    def _vocal(self, lang: str) -> dict:
        """How this language is voiced: spoken (Google) or sung (ElevenLabs), female/male voice, and a 'take'
        number (a new take of a sung song is a fresh composition). Defaults to the video's own setting."""
        v = (self.state.get("vocals") or {}).get(lang) or {}
        return {"mode": v.get("mode") or self.params.get("vocals", "tts"), "voice": v.get("voice") or "female",
                "take": int(v.get("take") or 0), "mood": v.get("mood") or self._video_mood()}

    def _video_mood(self) -> str:
        """The mood of the whole video: your choice, else Gemini's pick from the topic. Old videos get a guess
        from their title (a "sleep" rhyme is sleepy), otherwise playful as before."""
        s = self.state.get("script") or {}
        return moods.resolve(self.params.get("mood"), s.get("mood"), " ".join([self.params.get("topic") or "", s.get("title_en") or ""]))

    def _mood_id(self, lang: str | None) -> str:
        return self._vocal(lang)["mood"] if lang else self._video_mood()

    def _mood(self, lang: str | None) -> dict:
        return moods.get(self._mood_id(lang))

    def _mood_check(self, audio: Path, mood: str) -> dict:
        """Does the finished audio really feel like the mood? Counts musical hits per second (a lullaby has few)."""
        limit = moods.get(mood)["max_onsets"]
        try:
            rate = round(beats.onset_rate(audio), 2)
        except Exception:
            return {"mood": mood, "ok": True, "measured": False}
        return {"mood": mood, "hits_per_second": rate, "ok": limit is None or rate <= limit, "measured": True}

    def _change_vocals(self, lang: str, mode: str, voice: str, take: int, mood: str | None = None) -> None:
        if lang not in (self.state.get("outputs") or {}):
            raise RuntimeError(f"There is no {lang} version of this video yet")
        self.state.setdefault("vocals", {})[lang] = {"mode": mode, "voice": voice, "take": take, "mood": mood}
        self._add_language(lang)

    def change_vocals(self, lang: str, mode: str, voice: str, take: int, mood: str | None = None) -> None:
        """Re-voice one language of a finished video. Pictures and lyrics are reused; if it fails the old
        video stays as it was."""
        old = dict((self.state.get("vocals") or {}).get(lang) or {})
        def go():
            try:
                self._change_vocals(lang, mode, voice, take, mood)
            except BaseException:
                if old:
                    self.state["vocals"][lang] = old
                else:
                    self.state.get("vocals", {}).pop(lang, None)
                raise
        self.run(go)


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
