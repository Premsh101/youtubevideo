# 🧸 Toddler Rhyme Studio

Automated 2-minute nursery-rhyme / poem videos for toddlers (1–3 yrs), generated with
**Gemini on Vertex AI**, rendered in **English and Hindi from the same visuals**, with a
per-video cost readout in ₹ and one-click **YouTube** publishing.

```
Topic or your own poem ──► Gemini (script: EN+HI lyric per scene, visuals, camera)
Reusable characters   ──► reference sheet (made once) ─┐
                                                       ▼
                     Gemini Flash Image keyframes (ref sheets + previous frame = consistency)
                                                       │
                 ┌─────────────────────────────────────┴───────────────────────────┐
     cheap:  Ken-Burns motion + cross-fades (ffmpeg)         premium: Veo clips chained first→last frame
                 └─────────────────────────────────────┬───────────────────────────┘
                                                       ▼
          Cloud TTS (en-IN / hi-IN, slow + warm) + procedural lullaby, ducked under the voice
                                                       ▼
                 final_en.mp4 · final_hi.mp4 · captions · thumbnail · cost in ₹ · ▶ YouTube
```

## Quick start

```bash
pip install -r requirements.txt            # needs ffmpeg on PATH
cp .env.example .env                        # fill GOOGLE_APPLICATION_CREDENTIALS
./run.sh                                    # http://localhost:8000
```

No credentials yet? `MOCK_AI=1 ./run.sh` runs the whole pipeline with placeholder art and a
hummed voice so you can see the flow (costs are shown as *estimates*, nothing is billed).

### Vertex credentials
1. GCP console → IAM → Service account → key → **JSON**. Grant *Vertex AI User* and
   *Cloud Text-to-Speech* access; enable the Vertex AI and Text-to-Speech APIs.
2. `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` (project id is read from the file) —
   or put the JSON content in `VERTEX_SA_JSON`.

### YouTube
1. Same project → APIs → enable **YouTube Data API v3** → OAuth client (*Web application*),
   redirect URI `http://localhost:8000/youtube/oauth2callback`.
2. Save the client JSON as `data/secrets/youtube_client_secret.json`, click **Connect YouTube**
   in the header once. Uploads are flagged *made for kids* and default to **private** so you can
   review before publishing (`YOUTUBE_PRIVACY`). Captions (.srt) and a thumbnail are uploaded too.

## How the UI works
1. **Content** – let Gemini write an original rhyme on a topic, or paste your own poem
   (EN or HI); Gemini splits it into scenes and produces the other language.
2. **Look & feel** – pick **2D cartoon** or **3D rendered**, the motion engine, languages, length.
3. **Characters** – tick existing characters or type a brief ("a shy purple turtle") and Gemini
   designs one. Its reference sheet is generated **once** and reused in every scene of every
   future video — that is what keeps the character identical across videos.
4. **Create video** – live progress + running spend. Result shows both language players,
   lyrics, keyframes, download buttons, a cost table and *Post to YouTube*.

## Keeping cost low (and what a video costs)
| Item | Default model | Approx. per 2-min video (EN+HI) |
|---|---|---|
| Script (both languages, one call) | gemini-2.5-flash | ≈ ₹1 |
| 10–11 keyframes | gemini-2.5-flash-image ($0.039/img) | ≈ ₹35 |
| Voice, 2 languages | Cloud TTS Neural2 | ≈ ₹3 |
| Music | synthesised locally | ₹0 |
| **Keyframe engine total** | | **≈ ₹40** |
| Veo engine (15 × 8 s clips) | veo-3.1-fast ($0.15/s) | **+ ≈ ₹1 500** |

Reuse everywhere: script, every image, every TTS line and every Veo clip are cached by
content hash under `data/cache/`, so regenerating a video with the same poem/characters costs
₹0 for the parts that didn't change. The video track is rendered once and muxed twice (EN/HI).
Prices live in `app/config.py` / `PRICE_*` env vars; change `USD_TO_INR` for the exchange rate.

## Why scenes don't look stitched
* Every keyframe prompt receives the **character sheets + the previous keyframe** and is told to
  keep location, palette and lighting → one continuous story.
* Keyframe engine: continuous slow zoom/pan (alternating directions) and 0.8 s cross-fades, never
  a hard cut; fade-in/out at the ends.
* Veo engine: scene *N* is generated with first frame = keyframe *N* and last frame = keyframe
  *N+1*, so the end of one clip **is** the start of the next.
* Scene length follows the longer of the two narrations plus a breathing pause, so neither
  language ever runs over its picture.

## Toddler psychology baked in (`app/toddler.py`)
* **Colour**: saturated primaries (sunshine yellow, sky blue, apple red, grass green, candy pink),
  high contrast, big rounded shapes, oversized friendly eyes, uncluttered backgrounds.
* **Pacing**: ~12 s per scene, slow motion only, repetition in lyrics, 4–8 words per line.
* **Voice**: Indian-English / Hindi female voices at 88 % speed, +2.5 semitones, 0.6 s lead-in and
  1.3 s pause after each line.
* **Music**: 92 BPM major-pentatonic bell melody over soft pads (no dissonance), ducked −16 dB
  under the voice, loudness-normalised to −16 LUFS (no sudden loud sounds).
* **Safety**: negative prompt blocks scary/dark/text; uploads declared made-for-kids.

## Layout
```
app/config.py      env, models, price table      app/render.py    ffmpeg: Ken-Burns, xfade, ducking, mux
app/gemini_client  Vertex text/image/Veo + cache  app/pipeline.py  the job
app/characters.py  reusable character library     app/youtube.py   OAuth + upload
app/script_gen.py  poem + scene plan prompt       app/main.py      FastAPI API + static/index.html
app/tts.py         Cloud TTS per line             app/music.py     procedural lullaby
tests/             MOCK_AI end-to-end test (`MOCK_AI=1 pytest`)
```
Drop royalty-free `.mp3/.wav` into `data/music/` to use real tracks instead of the synthesiser.
