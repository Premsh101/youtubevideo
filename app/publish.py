"""Publish one language version of a video to any mix of: YouTube (full), YouTube Short,
Facebook Page, Instagram Reel.  Runs in the background; progress and links are stored on the
video's outputs[lang]["published"][target] so the UI (and you, later) can see what went where.

Order matters: YouTube goes first so its link can be added to the Facebook / Instagram text.
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from . import config, public_urls, render, social, tts, youtube

TARGETS = ("youtube", "youtube_short", "facebook", "instagram")
_lock = threading.Lock()


def _cut_point(state: dict, lang: str, limit: float, total: float) -> float:
    """Longest prefix ≤ limit that ends right after a complete scene (never mid-line)."""
    if total <= limit:
        return total
    tl = (state.get("timelines") or {}).get(lang)
    if not tl:
        return limit
    best = 0.0
    for st in tl["starts"][1:]:
        end = st + tl["xfade"] * 0.5   # middle of the cross-fade into the next scene
        if end <= limit:
            best = end
    return best if best >= 15 else limit


def vertical_version(job, lang: str, limit: float) -> Path:
    o = job.state["outputs"][lang]
    src = job.dir / (o.get("video_main") or o["video"])   # Shorts/Reels: rhyme only, no intro/outro
    total = tts.media_duration(src)
    cut = _cut_point(job.state, lang, limit, total)
    out = job.dir / f"{src.stem}_vertical_{int(cut)}s.mp4"
    if not out.exists():
        render.make_vertical(src, out, cut)
    return out


def _social_text(o: dict, yt_url: str | None, limit: int) -> str:
    first = (o.get("description") or "").strip().split("\n\n")[0]
    parts = [o.get("title", ""), first]
    if yt_url:
        parts.append(f"▶ Full video: {yt_url}")
    parts.append(" ".join((o.get("hashtags") or [])[:5]))
    return "\n\n".join(p for p in parts if p)[:limit]


def _set(job, lang: str, target: str, **kw) -> None:
    with _lock:
        pub = job.state["outputs"][lang].setdefault("published", {})
        pub[target] = {**pub.get(target, {}), **kw, "updated": int(time.time())}
        if target == "youtube" and kw.get("status") == "done":
            job.state["outputs"][lang]["youtube"] = {k: kw[k] for k in ("url", "video_id", "privacy") if k in kw}
        job.save()


def run(job, lang: str, targets: list[str], privacy: str | None, publish_at: str | None) -> None:
    o = job.state["outputs"][lang]
    order = [t for t in TARGETS if t in targets]
    for t in order:
        _set(job, lang, t, status="queued", error=None)
    for t in order:
        _set(job, lang, t, status="uploading")
        try:
            hashtags = " ".join(o.get("hashtags") or ["#nurseryrhymes", "#kidssongs", "#toddlers"])
            yt_url = ((o.get("published") or {}).get("youtube") or {}).get("url") or (o.get("youtube") or {}).get("url")
            if t in ("youtube", "youtube_short"):
                short = t == "youtube_short"
                video = vertical_version(job, lang, config.SHORT_MAX_SECONDS) if short else job.dir / o["video"]
                desc = o["description"] + "\n\n" + hashtags  # first 3 hashtags show above the title
                if short and yt_url:
                    desc = f"▶ Full video: {yt_url}\n\n" + desc
                res = youtube.upload(video, o["title"], desc, o["tags"], lang, thumbnail=job.dir / o["thumbnail"],
                                     captions=None if short else job.dir / (o.get("captions_upload") or o["captions"]),
                                     privacy=privacy, short=short, publish_at=publish_at)
            elif t == "facebook":
                url = public_urls.file_url(job.id, o["video"])
                res = social.post_facebook_video(url, o["title"], _social_text(o, yt_url, 5000))
            else:  # instagram
                vert = vertical_version(job, lang, config.REEL_MAX_SECONDS)
                res = social.post_instagram_reel(public_urls.file_url(job.id, vert.name),
                                                 _social_text(o, yt_url, 2200),
                                                 cover_url=public_urls.file_url(job.id, o["thumbnail"]))
            _set(job, lang, t, status="done", **res)
        except Exception as exc:  # noqa: BLE001 - shown next to that platform's button
            _set(job, lang, t, status="error", error=str(exc)[:600], trace=traceback.format_exc()[-800:])


def start(job, lang: str, targets: list[str], privacy: str | None = None, publish_at: str | None = None) -> None:
    threading.Thread(target=run, args=(job, lang, targets, privacy, publish_at), daemon=True,
                     name=f"publish-{job.id}-{lang}").start()
