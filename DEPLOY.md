# Step-by-step: from zero to your first published video

Five parts: **A** Google Cloud (Gemini/Veo/TTS), **B** ElevenLabs (sung vocals, optional),
**C** YouTube OAuth, **D** deploy on your KVM with Coolify, **E** what to click in the app.
Budget 45–60 minutes the first time.

---

## A · Google Cloud — Vertex AI service account (≈10 min)

1. Go to <https://console.cloud.google.com> → top bar project picker → **New project** → name it
   (e.g. `rhyme-studio`) → **Create** → select it.
2. **Billing** (left menu) → link a billing account (Vertex, Veo and TTS are pay-as-you-go).
3. Enable APIs: left menu **APIs & Services → Library**, search and click **Enable** for each:
   - *Vertex AI API*
   - *Cloud Text-to-Speech API*
   - *YouTube Data API v3* (used in part C)
4. Service account: **IAM & Admin → Service Accounts → + Create service account**
   - Name `rhyme-studio-bot` → **Create and continue**
   - Grant roles: **Vertex AI User** and **Cloud Speech Client**  → **Done**
5. Key file: click the new account → **Keys** tab → **Add key → Create new key → JSON → Create**.
   A file like `rhyme-studio-xxxx.json` downloads. **Keep it secret** — this is what the app
   needs as `GOOGLE_APPLICATION_CREDENTIALS` / `VERTEX_SA_JSON`.
6. Veo / image models: **Vertex AI → Model Garden** → search *Veo 3.1* and *Gemini 2.5 Flash Image*
   → **Enable** if a button shows (some accounts need to request access once).

## B · ElevenLabs — real sung vocals (optional, ≈3 min)

1. <https://elevenlabs.io> → sign up → pick a paid plan (Music API needs credits; roughly
   ₹25 per 2-min song per language, check your plan's credit rate).
2. Click your avatar (bottom-left) → **API Keys → Create API key** → copy it → this is
   `ELEVENLABS_API_KEY`. Skip this part entirely to use only Google TTS (spoken rhymes).

## C · YouTube — OAuth client (≈5 min)

1. In the same GCP project: **APIs & Services → OAuth consent screen** → *External* → fill
   app name / your email → **Save**. Under **Test users → + Add users** add the Google account
   that owns your YouTube channel (needed while the app is in "Testing").
2. **APIs & Services → Credentials → + Create credentials → OAuth client ID**
   - Application type **Web application**, name `rhyme-studio`
   - **Authorised redirect URIs → + Add URI**: `https://YOUR-DOMAIN/youtube/oauth2callback`
     (and `http://localhost:8000/youtube/oauth2callback` if you also run it locally)
   - **Create → Download JSON**. You'll upload this file in part D as
     `/srv/data/secrets/youtube_client_secret.json`.
3. Make sure your YouTube channel exists (youtube.com → avatar → *Create a channel*).

## D · Deploy on your KVM 4 with Coolify (≈15 min)

Pre-req: Coolify installed on the server (`curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash`),
and a DNS **A record** for e.g. `rhymes.yourdomain.com` → your server IP.

1. Coolify dashboard → **Projects → + Add** → name `rhyme-studio` → open its *production* env.
2. **+ New Resource → Public Repository** (or *Private Repository (with GitHub App)* if private)
   - Repository URL: `https://github.com/Premsh101/youtubevideo`, branch: the one with this code
   - **Build Pack: Docker Compose** → it finds `docker-compose.yml` → **Continue**.
3. In the resource → **General**: set **Domains** for service `studio` to
   `https://rhymes.yourdomain.com` (Coolify issues the HTTPS certificate automatically).
4. **Environment Variables** tab → add (use *Developer view* to paste many at once):
   ```
   VERTEX_SA_JSON=<paste the ENTIRE content of the JSON key from part A5, one line>
   GOOGLE_CLOUD_LOCATION=us-central1
   APP_USER=admin
   APP_PASSWORD=<a long password — this is the login for the whole app>
   ELEVENLABS_API_KEY=<from part B, or leave empty>
   YOUTUBE_REDIRECT_URI=https://rhymes.yourdomain.com/youtube/oauth2callback
   YOUTUBE_PRIVACY=private
   USD_TO_INR=84
   ```
   Tip: to paste the JSON as one line run `tr -d '\n' < rhyme-studio-xxxx.json` locally.
5. **Storages** tab → the compose file already declares volume `studio_data` → `/srv/data`
   (characters, cache, videos, YouTube token persist across redeploys). Add a **File mount**:
   - Destination path `/srv/data/secrets/youtube_client_secret.json`
   - Content: paste the OAuth client JSON from part C2.
6. Click **Deploy** (top-right). First build ≈3–5 min (installs ffmpeg + Python deps).
   Watch **Deployments → logs** until it shows *Application started*.
7. Open `https://rhymes.yourdomain.com` → browser asks for username/password → `admin` / your
   `APP_PASSWORD`. You should see the studio header with a **Connect YouTube** button.
8. Click **Connect YouTube** → Google sign-in → choose the channel's account → *Continue* on
   the "unverified app" screen (it's your own app) → allow → you land back on the studio with
   **YouTube ✓ connected**. This is done once; the token is stored in the volume.

Running locally instead: `cp .env.example .env`, fill it, `./run.sh`, open <http://localhost:8000>.

## E · In the app — what to click (per video, ≈3 min of clicks + 3–6 min of generation)

The page is one screen: left column = settings 1-2-3, right column = progress / result / history.

**1 · What should the rhyme be about?**
- `⭐ Famous rhyme` (default) → pick one from the dropdown — these are public-domain versions
  of the most-watched rhymes (Johny Johny, Wheels on the Bus, Machli Jal Ki Rani, Chanda Mama…).
- `Let Gemini write it` → type a topic ("brushing teeth") or leave blank.
- `I have a poem` → paste your own lyrics (English or Hindi); Gemini makes the other language.

**2 · Look & feel**
- *Animation mode*: `🧊 3D rendered` (what Cocomelon / Little Treehouse look like — default) or `🎨 2D cartoon`.
- *Motion engine*: `💸 Keyframes + smooth camera` (≈₹55 per video, default) or `🎬 Veo video clips` (≈₹1,500).
- *Vocals*: `🗣 Spoken rhyme` (Google TTS, ≈₹3) or `🎤 Sung song` (ElevenLabs, ≈₹25 per language;
  greyed out until `ELEVENLABS_API_KEY` is set).
- *Output videos*: tick **English**, **हिंदी** or both — each produces its own MP4.
- *Target length* slider: 60–180 s (default 120).

**3 · Characters** — nothing to do. Gemini casts from your library (reusing familiar faces) and
invents a new character only if the rhyme needs one; it is saved for next time. Tap a card to pin one.

Below the settings the grey line shows the **estimated cost in ₹** for exactly these choices.

**✨ Create video** → confirm the dialog → the right column shows a progress bar with the current
step ("Gemini is casting the characters", "Drawing keyframe 4/17", "Composing the HI song…") and
the **running spend**. Wait ~3–6 minutes (longer with Veo).

**Result card**
- Two players side by side: **🇬🇧 English** and **🇮🇳 हिंदी**, each with
  `⬇ Download MP4`, `⬇ Captions` (SRT) and `📤 Post to YouTube`.
- `📤 Post to YouTube` → choose `private` / `unlisted` / `public` in the prompt → wait for
  "Uploaded: https://youtu.be/…" → the button becomes `▶ On YouTube`. Title, description,
  tags, thumbnail and captions are set automatically; the video is declared *made for kids*.
  Start with **private**, watch it on YouTube Studio, then switch it to public there.
- *Cast*, *Lyrics* (EN + HI side by side), the keyframe strip, and **Cost of this video** in ₹
  broken down by Gemini text / images / voice or song / Veo, with cached (free) items marked.

**Previous videos** (bottom right) lists every past job with status and cost — click a title to
reopen it, download again or publish the other language later.

### Costs at a glance (defaults, ₹84/$)
| Choice | per video (EN + HI) |
|---|---|
| Keyframes + spoken TTS | ≈ ₹60 |
| Keyframes + sung (ElevenLabs) | ≈ ₹105 |
| Veo + sung | ≈ ₹1,600 |
| Re-running the same rhyme/characters | ₹0 for every cached part |
