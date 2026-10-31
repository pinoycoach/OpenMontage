"""Reel render service: turns a ViralReel Studio reel into an HD MP4.

POST /render takes a reel's scenes (images as data URLs), the voiceover and the
caption style, builds props for the Remotion ``Explainer`` composition and
returns the rendered H.264 MP4. Captions are word-by-word, timed across each
scene's window. Only data URLs are accepted, so the service never fetches
arbitrary URLs.

Captions are timed to the actual speech when faster-whisper is installed (it
transcribes the voiceover and lines its word timings up with the script), and an
optional music track is mixed under the voice with automatic ducking.

Run locally from the repo root:
    RENDER_TOKEN=dev uvicorn services.reel_render.app:app --port 8090
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:  # imported as the package services.reel_render.app (tests, uvicorn from the repo root)
    from . import finishing
except ImportError:  # imported as a plain module beside this file (reels_from_scripts.py)
    import finishing

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_DIR = REPO_ROOT / "remotion-composer"
RENDER_TOKEN = os.environ.get("RENDER_TOKEN", "")
RENDER_TIMEOUT_SECONDS = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "840"))
# "whisper" times captions to the real speech; anything else spreads words evenly per scene.
CAPTION_SYNC = os.environ.get("CAPTION_SYNC", "whisper")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
# Talking scenes: the persona photo lip-synced to that scene's stretch of narration (fal).
FAL_KEY = os.environ.get("FAL_KEY", "")
FAL_TALKING_MODEL = os.environ.get("FAL_TALKING_MODEL", "fal-ai/flashtalk")
TALKING_TIMEOUT_SECONDS = int(os.environ.get("TALKING_TIMEOUT_SECONDS", "360"))
MAX_TALKING_SCENES = 3
# Motion scenes: a still scene image animated into a short video clip (fal, Kling by default).
FAL_MOTION_MODEL = os.environ.get("FAL_MOTION_MODEL", "fal-ai/kling-video/v3/standard/image-to-video")
MOTION_TIMEOUT_SECONDS = int(os.environ.get("MOTION_TIMEOUT_SECONDS", "420"))
MAX_MOTION_SCENES = 2
# H3 Max motion ("cinematic"): each moving scene starts on its own image and ends on the next scene's
# image (keyframes), so the shot flows into the next one. About a third of Kling's price per second,
# so every image scene can move.
FAL_H3_MOTION_MODEL = os.environ.get("FAL_H3_MOTION_MODEL", "minimax/h3-max/reference-to-video")
MAX_H3_MOTION_SCENES = 12
MOTION_WORKERS = 4
# Rough fal prices in USD per second of clip, for the log line and the daily cap only (estimate;
# the fal dashboard has the real charge). A model not listed counts at its kind's fallback rate.
FAL_USD_PER_SECOND = {
    "fal-ai/flashtalk": 0.02,
    "fal-ai/kling-video/v3/standard/image-to-video": 0.14,
    "fal-ai/kling-video/v2.5-turbo/standard/image-to-video": 0.042,
    # 768P, the fal model page on 23 Oct 2026 (480P is 0.05, 1080P 0.16). One reference image stays
    # inside the free 4,096 reference tokens.
    "minimax/h3-max/reference-to-video": 0.08,
}
FAL_FALLBACK_USD_PER_SECOND = {"talking": 0.02, "motion": 0.14}
# Estimate: one Demucs job, flat (a full 15-minute episode at about $0.0007 per second).
FAL_STEMS_USD = 0.65
# Optional spend brake: past this many estimated USD per day (Manila time) fal is not called and
# the scene or background falls back as if fal had failed. Unset means no cap.
FAL_DAILY_CAP_USD = float(os.environ["FAL_DAILY_CAP_USD"]) if os.environ.get("FAL_DAILY_CAP_USD") else None
MANILA = timezone(timedelta(hours=8))  # Asia/Manila is UTC+8 all year (no daylight saving)
MAX_FAL_RESULT_BYTES = 500 * 1024 * 1024
MAX_SCENES = 30
MAX_ASSET_BYTES = 25 * 1024 * 1024
FPS = 30

CameraMotion = Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "subtle-pulse", "breathe"]
CaptionStyle = Literal["yellow-pop", "neon-cyan", "minimal-white"]
# Where a scene's captions sit: "low" above the Reels/TikTok buttons (the default), or "high", just
# under the "AI-generated" label, for a shot whose subject fills the lower half (Character Studio's
# ghost-dog punchline on 30 Oct 2026 was hidden behind its own caption).
CaptionPosition = Literal["low", "high"]
Particles = Literal["fireflies", "petals", "sparkles", "mist", "light-rays"]

# Explainer understands zoom/pan directly; the app's "subtle-pulse" is closest to ken-burns.
MOTION_MAP = {"subtle-pulse": "ken-burns"}
CAPTION_COLORS = {"yellow-pop": "#FDE047", "neon-cyan": "#38BDF8", "minimal-white": "#FFFFFF"}
MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}
DATA_URL_RE = re.compile(r"^data:([\w/+.-]+)(?:;[\w=.-]+)*;base64,(.*)$", re.DOTALL)


class Card(BaseModel):
    """Content for an animated card scene (stat / list / quote / compare), drawn by the composer.
    Compare items are the two sides as "Label: text", e.g. "Myth: …" and "Fact: …"."""
    kind: Literal["stat", "list", "quote", "compare"]
    title: Optional[str] = Field(None, max_length=300)
    label: Optional[str] = Field(None, max_length=300)
    items: list[str] = Field(default_factory=list, max_length=5)
    source: Optional[str] = Field(None, max_length=200)


class EndCard(BaseModel):
    """A closing card after the last scene: the creator's handle and a call to action."""
    handle: str = Field(min_length=1, max_length=40)
    text: str = Field("Follow for more", max_length=40)


END_CARD_SECONDS = 2.5
# Reels end on their last scene: no empty tail after it (the composer's default is 1 s of black).
TAIL_SECONDS = 0.0
# Without word timings, the last scene runs this long past its last caption word, so the line can be read.
LAST_LINE_HOLD = 0.5
# Straight cuts everywhere, the first scene included: a fade-in on frame 1 is a blank opening frame,
# and short-form viewers decide in the first second (skills/creative/short-form.md: movement and text
# in frame 1, no fade-in). The composer's default is a spring fade-in, so "cut" has to be explicit.


class CardClip(BaseModel):
    """A clip placed into a still card (Character Studio's paper quote card): `image` is the card with
    its paper, quote and torn frame; the clip's crop (sx, sy, sw, sh, in clip pixels) is scaled into the
    box (x, y, w, h, on the 1080x1920 card) and cut to the torn edge by `mask` (a PNG the box's size,
    white where the clip shows). Done here, frame by frame, instead of recording the card in the browser,
    which stuttered and froze on real laptops (31 Oct 2026)."""
    video: str
    mask: str
    x: int = Field(ge=0, le=4000)
    y: int = Field(ge=0, le=4000)
    w: int = Field(gt=0, le=4000)
    h: int = Field(gt=0, le=4000)
    sx: float = Field(0, ge=0)
    sy: float = Field(0, ge=0)
    sw: float = Field(gt=0)
    sh: float = Field(gt=0)


class Scene(BaseModel):
    image: str
    durationSeconds: float = Field(4, gt=0, le=60)
    captionOverlay: Optional[str] = None
    voiceoverLine: Optional[str] = None
    cameraMotion: Optional[CameraMotion] = None
    # Set for card scenes: rendered as an animated card; `image` (the still card) is the fallback.
    card: Optional[Card] = None
    # What should move in a motion scene, e.g. "steam rises from the cup".
    motionPrompt: Optional[str] = Field(None, max_length=500)
    # A ready-made clip for this scene (video data URL, e.g. an approved AI shot). It plays instead of
    # the image; when it's shorter than the scene, its last frame is held so the frame never goes empty.
    video: Optional[str] = None
    # Or a clip that plays inside the still card `image` (see CardClip). It moves with the card's camera
    # move, and a clip shorter than the scene goes there and back instead of freezing on its last frame.
    cardClip: Optional[CardClip] = None
    captionPosition: CaptionPosition = "low"


class RenderRequest(BaseModel):
    scenes: list[Scene] = Field(min_length=1, max_length=MAX_SCENES)
    voiceover: Optional[str] = None
    aspectRatio: Literal["9:16", "16:9"] = "9:16"
    captionStyle: CaptionStyle = "yellow-pop"
    captions: bool = True
    music: Optional[str] = None
    musicVolume: float = Field(0.2, ge=0, le=1)
    # Persona photo and the scenes (0-based) where it talks to camera instead of showing the image.
    talkingImage: Optional[str] = None
    talkingScenes: list[int] = Field(default_factory=list, max_length=MAX_TALKING_SCENES)
    # Scenes (0-based) whose image is animated into a short video clip.
    motionScenes: list[int] = Field(default_factory=list, max_length=MAX_H3_MOTION_SCENES)
    # "kling": up to MAX_MOTION_SCENES scenes. "h3": H3 Max, up to MAX_H3_MOTION_SCENES scenes.
    # Both animate each scene from its own image alone.
    motionEngine: Literal["kling", "h3"] = "kling"
    # Floating particles over the whole video (part of a visual style).
    particles: Optional[Particles] = None
    # Closing card with the creator's handle, shown after the last scene.
    endCard: Optional[EndCard] = None
    # Whoosh on scene changes and a soft pop when a card appears, mixed under the voice.
    sfx: bool = False
    # The finishing pass (finishing.py), on by default: the "AI-generated" tag on every frame, and the
    # audio at -14 LUFS with the true peak at -1 dBTP or below. Off only for a reel with nothing AI in it.
    aiLabel: bool = True
    normalizeAudio: bool = True
    # The dark edges the composer draws over image and video scenes. Off for a light, paper-style card
    # (Character Studio's quote cards), where they read as grey dirt in the corners.
    vignette: bool = True
    # A reel made to replay seamlessly on the platform (Character Studio's loop cards, 30 Oct 2026): no
    # hold at the end, clips there and back, and music cut to the exact length with its end blended into
    # its start (no fades), so the replay shows no restart. (No reel fades in from black any more.)
    loop: bool = False


OPEN_PATHS = {"/health", "/healthz"}


def _check_token(request: Request, authorization: Optional[str] = Header(None)) -> None:
    """Every route needs the token except the health check. It runs as an app-wide dependency, so a
    new route is covered without remembering to call it."""
    if request.url.path in OPEN_PATHS:
        return
    if not RENDER_TOKEN:
        raise HTTPException(503, "RENDER_TOKEN is not configured on the render service.")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), RENDER_TOKEN.encode()):
        raise HTTPException(401, "Bad render token.")


app = FastAPI(title="OpenMontage reel render", dependencies=[Depends(_check_token)])
# One render per instance: Remotion already uses every core it is given.
_render_lock = threading.Lock()
LOCK_WAIT_SECONDS = 5


def decode_data_url(data_url: str, allowed_prefix: str) -> tuple[bytes, str]:
    """Return (bytes, file extension) for a base64 data URL of the allowed kind."""
    match = DATA_URL_RE.match(data_url.strip())
    if not match:
        raise HTTPException(400, "Assets must be base64 data URLs.")
    mime = match.group(1).lower()
    if not mime.startswith(allowed_prefix) or mime not in MIME_EXT:
        raise HTTPException(400, f"Unsupported asset type: {mime}")
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "Asset is not valid base64.") from exc
    if not raw or len(raw) > MAX_ASSET_BYTES:
        raise HTTPException(400, "Asset is empty or too large.")
    return raw, MIME_EXT[mime]


def probe_duration(path: Path) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return float(out) if out else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def scene_timeline(durations: list[float], voice_seconds: Optional[float]) -> list[tuple[float, float]]:
    """Scene (start, end) times. Like the app's own exporter, stretch every scene
    when the voiceover runs longer than the plan so no narration is cut."""
    planned = sum(durations)
    stretch = 1.0
    if voice_seconds and voice_seconds + 0.5 > planned:
        stretch = (voice_seconds + 0.5) / planned
    spans, t = [], 0.0
    for d in durations:
        spans.append((round(t, 3), round(t + d * stretch, 3)))
        t += d * stretch
    return spans


def page_breaks(count: int, per_page: int) -> set[int]:
    """Word positions (0-based) to end a caption page after, so a line's pages are as even as possible
    and never leave one word alone. A line of up to per_page + 2 words is one page (so "Nilagyan ng mga Pinoy
    ng bubong," reads as one caption); longer lines split evenly: 7 → 4 + 3, 9 → 3 + 3 + 3."""
    if count <= 0:
        return set()
    if count <= per_page + 2:  # a short line (up to 6 words on vertical) stays whole: one page, two rows
        return {count - 1}
    pages = -(-count // per_page)
    base, extra = divmod(count, pages)
    out, k = set(), 0
    for p in range(pages):
        k += base + (1 if p < extra else 0)
        out.add(k - 1)
    return out


def word_captions(text: str, start: float, end: float, per_page: int = 4) -> list[dict]:
    """Spread a line's words across its whole scene, weighted by word length. No inset at the edges:
    an inset left a caption gap of up to 0.3 s at every cut in reels without a voice."""
    words = text.split()
    if not words or end <= start:
        return []
    t0, t1 = start, end
    weights = [len(w) + 2 for w in words]
    total = sum(weights)
    breaks = page_breaks(len(words), per_page)
    out, t = [], t0
    for i, (w, wt) in enumerate(zip(words, weights)):
        dt = (t1 - t0) * wt / total
        out.append({
            "word": w,
            "startMs": round(t * 1000),
            "endMs": round((t + dt) * 1000),
            "pageBreakAfter": i in breaks,
        })
        t += dt
    return out


_whisper_model = None
_whisper_lock = threading.Lock()


def transcribe(path: Path) -> Optional[list[tuple[str, float, float]]]:
    """Word timings heard in the audio, or None when faster-whisper isn't available or fails."""
    global _whisper_model
    if CAPTION_SYNC != "whisper":
        return None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    try:
        with _whisper_lock:
            if _whisper_model is None:
                _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            segments, _ = _whisper_model.transcribe(str(path), word_timestamps=True, beam_size=1)
            return [(w.word.strip(), float(w.start), float(w.end))
                    for seg in segments for w in (seg.words or []) if w.word.strip()]
    except Exception as exc:  # noqa: BLE001 - captions fall back to estimated timing
        print(f"[reel_render] transcription failed, using estimated caption timing: {exc}")
        return None


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def align_words(script: list[str], heard: list[tuple[str, float, float]],
                min_match: float = 0.6) -> Optional[list[tuple[float, float]]]:
    """(start, end) for every script word, taken from the matching heard word.
    Words Whisper missed or misheard are fitted into the gaps between matched ones.
    Returns None when too little of the script was recognised to trust."""
    if not script or not heard:
        return None
    times: list[Optional[tuple[float, float]]] = [None] * len(script)
    matcher = SequenceMatcher(a=[_norm(w) for w in script], b=[_norm(h[0]) for h in heard], autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            h = heard[block.b + k]
            times[block.a + k] = (h[1], h[2])
    if sum(t is not None for t in times) < min_match * len(script):
        return None
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        gap_start = times[i - 1][1] if i > 0 else max(0.0, times[j][0] - 0.3 * (j - i))
        gap_end = times[j][0] if j < len(times) else gap_start + 0.3 * (j - i)
        step = max(gap_end - gap_start, 0.0) / (j - i)
        for k in range(i, j):
            times[k] = (gap_start + step * (k - i), gap_start + step * (k - i + 1))
        i = j
    return [t for t in times if t is not None]


def timed_spans(word_ranges: list[tuple[int, int]], times: list[tuple[float, float]],
                min_scene: float = 0.8) -> Optional[list[tuple[float, float]]]:
    """Scene cuts that land between the last word of one line and the first word of the next,
    so each picture changes exactly when the narration moves on."""
    if any(a >= b for a, b in word_ranges):
        return None
    bounds = [0.0]
    for (_, prev_end), (next_start, _) in zip(word_ranges, word_ranges[1:]):
        bounds.append((times[prev_end - 1][1] + times[next_start][0]) / 2)
    bounds.append(times[word_ranges[-1][1] - 1][1] + 0.8)
    spans = [(round(a, 3), round(b, 3)) for a, b in zip(bounds, bounds[1:])]
    if any(b - a < min_scene for a, b in spans):
        return None
    return spans


CAPTION_TOP_OFFSET = 400


def caption_line(scene: Scene, has_voice: bool) -> str:
    # With narration, captions follow what is spoken; otherwise show the on-screen text.
    return (scene.voiceoverLine if has_voice else None) or scene.captionOverlay or ""


def build_props(req: RenderRequest, image_paths: list[Path], voice_path: Optional[Path],
                voice_seconds: Optional[float],
                heard: Optional[list[tuple[str, float, float]]] = None) -> dict:
    spans = scene_timeline([s.durationSeconds for s in req.scenes], voice_seconds)
    vertical = req.aspectRatio == "9:16"
    per_page = 4 if vertical else 6  # caption words per page

    # Exact timing: line the heard words up with the script, then cut scenes between lines.
    exact: Optional[list[tuple[float, float]]] = None
    lines = [caption_line(s, bool(voice_path)).split() for s in req.scenes]
    if voice_path and heard:
        script = [w for line in lines for w in line]
        times = align_words(script, heard)
        if times:
            ranges, k = [], 0
            for line in lines:
                ranges.append((k, k + len(line)))
                k += len(line)
            exact_spans = timed_spans(ranges, times)
            if exact_spans:
                spans, exact = exact_spans, times

    if not exact and spans and not req.endCard and not req.loop:  # an end card follows instead
        start, end = spans[-1]
        spans[-1] = (start, round(end + LAST_LINE_HOLD, 3))

    cuts, captions = [], []
    word_index = 0
    for i, (scene, path, (start, end)) in enumerate(zip(req.scenes, image_paths, spans)):
        motion = scene.cameraMotion or "subtle-pulse"
        cut = {
            "id": f"scene-{i + 1}",
            "source": path.name,  # resolved against --public-dir (the job folder)
            "in_seconds": start,
            "out_seconds": end,
            "animation": MOTION_MAP.get(motion, motion),
            "transition_in": "cut",
            "transition_out": "cut",
        }
        if not req.vignette:
            cut["vignette"] = False
        if scene.card:
            cut["type"] = "reel_card"
            cut["card"] = scene.card.model_dump(exclude_none=True)
        cuts.append(cut)
        words = lines[i]
        breaks = page_breaks(len(words), per_page)
        first = len(captions)
        if req.captions and exact:
            for n, word in enumerate(words):
                t0, t1 = exact[word_index + n]
                captions.append({"word": word, "startMs": round(t0 * 1000), "endMs": round(t1 * 1000),
                                 "pageBreakAfter": n in breaks})
        elif req.captions:
            captions.extend(word_captions(" ".join(words), start, end, per_page))
        # A scene's last word always ends a page, so every page belongs to one scene and takes its place.
        if scene.captionPosition == "high":
            for word in captions[first:]:
                word["high"] = True
        word_index += len(words)

    props: dict = {
        "cuts": cuts,
        "overlays": [],
        "captions": captions,
        "audio": {},
        # Every scene is a full-frame image, so skip the animated background (4x faster renders).
        "plainBackground": True,
        # End on the last scene, with its caption still on screen.
        "tailSeconds": TAIL_SECONDS,
        "holdLastCaption": not req.endCard,
        # Caption pages appear at full opacity at once: a fading page is unreadable on bright frames.
        "captionEntrance": "none",
        # Big, short caption pages that sit above the Reels/TikTok UI on vertical video.
        "captionFontSize": 76 if vertical else 56,
        # The composer's hard limit; page_breaks() already splits lines (short lines stay whole up to +2).
        "captionWordsPerPage": per_page + 2,
        "captionBottomOffset": 460 if vertical else 90,
        # A "high" page's top edge: under the "AI-generated" label, which ends near y 345 on vertical.
        "captionTopOffset": CAPTION_TOP_OFFSET if vertical else 90,
        "themeConfig": {
            "backgroundColor": "#000000",
            "surfaceColor": "#000000",
            "textColor": "#FFFFFF",
            "captionHighlightColor": CAPTION_COLORS[req.captionStyle],
            # A near-solid dark box: white and yellow words stay readable on any photo.
            "captionBackgroundColor": "rgba(0, 0, 0, 0.85)",
        },
    }
    if req.endCard and cuts:
        start = cuts[-1]["out_seconds"]
        cuts.append({
            "id": "end-card",
            "source": cuts[-1]["source"],
            "in_seconds": start,
            "out_seconds": round(start + END_CARD_SECONDS, 3),
            "type": "reel_card",
            "card": {"kind": "end", "title": req.endCard.handle, "label": req.endCard.text},
        })
    if req.particles:
        props["particles"] = req.particles
    if voice_path:
        props["audio"]["narration"] = {"src": voice_path.name, "volume": 1}
    props["captionTiming"] = "speech" if exact else "estimated"
    return props


# The music bed's loudness at musicVolume 1.0, before ducking. AI tracks (Lyria) arrive mastered loud,
# so a plain volume cut left the bed louder than the voice in the pauses; levelling it first makes
# musicVolume mean the same thing for every track (0.5 = -32 LUFS, about 18 dB under a -14 voice).
MUSIC_BED_LUFS = -26.0


# A loop reel's music: the half second after the cut is blended (equal power) into the first half
# second, so the end of the track runs straight on into its start when the platform replays it.
LOOP_BLEND_SECONDS = 0.5


def loop_music(music_path: Path, total: float, job_dir: Path, volume: float) -> Path:
    """The music for a loop reel, exactly `total` long: levelled like any bed, then the blend after the
    cut mixed into the start (equal power), so its end runs straight on into its start. Two ffmpeg
    steps: one graph that splits a stream and waits on its own tail stalled and went silent."""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    blend = min(LOOP_BLEND_SECONDS, total / 4)
    levelled = job_dir / "music-levelled.wav"
    out = job_dir / "music-loop.wav"
    steps = [
        ["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(music_path), "-af",
         f"{fmt},atrim=0:{total + blend:.3f},loudnorm=I={MUSIC_BED_LUFS:.1f}:TP=-2:LRA=11,{fmt},volume={volume:.3f}",
         "-t", f"{total + blend:.3f}", str(levelled)],
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(levelled), "-i", str(levelled), "-filter_complex",
         f"[0:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={blend:.3f}:curve=qsin[start];"
         f"[1:a]atrim={total:.3f}:{total + blend:.3f},asetpts=PTS-STARTPTS,afade=t=out:d={blend:.3f}:curve=qsin[end];"
         "[start][end]amix=inputs=2:duration=first:normalize=0[out]",
         "-map", "[out]", str(out)],
    ]
    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise HTTPException(500, f"Loop music failed: {proc.stderr[-500:]}")
    return out


def mix_music(voice_path: Optional[Path], music_path: Path, total: float, job_dir: Path,
              volume: float, loop: bool = False) -> Path:
    """One soundtrack: the music looped or trimmed to the video, levelled to MUSIC_BED_LUFS, faded in
    and out (or, for a loop reel, blended end into start), and ducked under the voice (a sidechain
    compressor lowers it whenever someone speaks)."""
    out = job_dir / "soundtrack.wav"
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fade_out = max(total - 1.0, 0.0)  # a 1 s fade that ends exactly with the video
    if loop:
        music_path = loop_music(music_path, total, job_dir, volume)
        music_chain = f"[1:a]{fmt},atrim=0:{total:.3f}"
    else:
        music_chain = (f"[1:a]{fmt},atrim=0:{total:.2f},loudnorm=I={MUSIC_BED_LUFS:.1f}:TP=-2:LRA=11,{fmt},"
                       f"volume={volume:.3f},"
                       f"afade=t=in:d=1,afade=t=out:st={fade_out:.2f}:d=1")
    if voice_path:
        graph = (f"[0:a]{fmt},apad=whole_dur={total:.2f},asplit=2[voice][key];"
                 f"{music_chain}[music];"
                 "[music][key]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[ducked];"
                 "[voice][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]")
        inputs = ["-i", str(voice_path)]
    else:
        graph = f"{music_chain},apad=whole_dur={total:.2f}[out]"
        inputs = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-stream_loop", "-1", "-i", str(music_path),
           "-filter_complex", graph, "-map", "[out]", "-t", f"{total:.2f}", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(500, f"Music mix failed: {proc.stderr[-500:]}")
    return out


SFX_SOURCES = {
    # Short, quiet effects made with ffmpeg, so there are no sound files to license or ship.
    "whoosh": ("anoisesrc=d=0.5:c=pink:a=0.6,highpass=f=300,lowpass=f=3500,"
               "afade=t=in:d=0.3:curve=exp,afade=t=out:st=0.3:d=0.2,volume=1.4"),
    "pop": ("sine=f=660:d=0.12,afade=t=out:st=0.01:d=0.11:curve=exp,volume=2.7"),
}


def sfx_events(props: dict) -> list[tuple[str, float]]:
    """A whoosh just before each scene change and a pop as each card lands."""
    events: list[tuple[str, float]] = []
    for i, cut in enumerate(props["cuts"]):
        if i > 0:
            events.append(("whoosh", max(0.0, cut["in_seconds"] - 0.25)))
        if cut.get("type") == "reel_card":
            events.append(("pop", cut["in_seconds"] + 0.35))
    return events


def mix_sfx(base: Optional[Path], events: list[tuple[str, float]], total: float, job_dir: Path) -> Path:
    """Lay the effects over the narration (or soundtrack), or over silence when there's no audio."""
    out = job_dir / "with-sfx.wav"
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    inputs = ["-i", str(base)] if base else ["-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    graph = [f"[0:a]{fmt},apad=whole_dur={total:.2f}[base]"]
    labels = ["[base]"]
    for n, (kind, at) in enumerate(events):
        ms = int(at * 1000)
        graph.append(f"{SFX_SOURCES[kind]},{fmt},adelay={ms}|{ms}[e{n}]")
        labels.append(f"[e{n}]")
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0,alimiter=limit=0.95[out]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(graph),
           "-map", "[out]", "-t", f"{total:.2f}", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"Sound effects mix failed: {proc.stderr[-500:]}")
    return out


def _fal_json(url: str, body: Optional[dict] = None, timeout: int = 60, method: Optional[str] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    method = method or ("POST" if data else "GET")
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Keep fal's explanation (e.g. which input a 422 rejected) for the logs.
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"fal HTTP {exc.code}: {detail}") from exc


def _as_data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _fal_file_url(path: Path, mime: str) -> str:
    """Upload a file to fal's CDN and return its URL. Some models (sync-3) reject inline data
    URLs; if the upload itself fails, fall back to the data URL, which others accept."""
    try:
        upload = _fal_json("https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3",
                           {"file_name": path.name, "content_type": mime}, timeout=30)
        request = urllib.request.Request(upload["upload_url"], data=path.read_bytes(), method="PUT",
                                         headers={"Content-Type": mime})
        with urllib.request.urlopen(request, timeout=60):
            pass
        return upload["file_url"]
    except Exception as exc:
        print(f"[talking] upload to fal failed, sending {path.name} inline: {exc}", flush=True)
        return _as_data_url(path, mime)


def _fal_run(model: str, body: dict, deadline: float, tag: str) -> dict:
    """Submit a job to fal's queue, wait for it and return its result. Past the deadline the
    job is cancelled, so a clip nobody will use isn't made (and billed) later."""
    job = _fal_json(f"https://queue.fal.run/{model}", body)
    status_url, response_url = job["status_url"], job["response_url"]
    while True:
        status = _fal_json(status_url).get("status")
        if status == "COMPLETED":
            return _fal_json(response_url)
        if status not in ("IN_QUEUE", "IN_PROGRESS"):
            raise RuntimeError(f"fal job ended as {status}")
        if time.monotonic() > deadline:
            try:
                _fal_json(job.get("cancel_url") or f"{response_url}/cancel", method="PUT", timeout=15)
            except Exception as exc:
                print(f"[{tag}] could not cancel the fal job: {exc}", flush=True)
            raise TimeoutError(f"fal took too long (still {status})")
        time.sleep(3)


_fal_spend = {"day": "", "usd": 0.0}
_fal_spend_lock = threading.Lock()


def fal_estimate(tag: str, model: str, seconds: float) -> float:
    if tag == "stems":
        return FAL_STEMS_USD
    rate = FAL_USD_PER_SECOND.get(model, FAL_FALLBACK_USD_PER_SECOND[tag])
    return round(rate * seconds, 4)


def _fal_log(severity: str, **fields) -> None:
    # One JSON line per fal job; Cloud Run's logging reads it as a structured entry.
    print(json.dumps({"severity": severity, "event": "fal_job", **fields}), flush=True)


def _fal_paid(tag: str, model: str, body: dict, deadline: float, seconds: float) -> dict:
    """_fal_run with the daily cap and a log line. A refused job raises like a failed one, so the
    caller falls back the same way."""
    estimate = fal_estimate(tag, model, seconds)
    line = {"tag": tag, "model": model, "seconds": round(seconds, 2), "estimateUsd": estimate}
    with _fal_spend_lock:
        today = datetime.now(MANILA).date().isoformat()
        if _fal_spend["day"] != today:
            _fal_spend.update(day=today, usd=0.0)
        if FAL_DAILY_CAP_USD is not None and _fal_spend["usd"] + estimate > FAL_DAILY_CAP_USD:
            _fal_log("WARNING", **line, status="cap", dayTotalUsd=round(_fal_spend["usd"], 4),
                     capUsd=FAL_DAILY_CAP_USD)
            raise RuntimeError(f"daily fal cap of ${FAL_DAILY_CAP_USD:g} reached")
        # Counted when sent: a job that fails or is cancelled may still be billed.
        _fal_spend["usd"] += estimate
        day_total = round(_fal_spend["usd"], 4)
    try:
        result = _fal_run(model, body, deadline, tag)
    except Exception as exc:
        _fal_log("WARNING", **line, status="failed", dayTotalUsd=day_total, error=str(exc)[:300])
        raise
    _fal_log("INFO", **line, status="ok", dayTotalUsd=day_total)
    return result


def _download(url: str, out: Path) -> Path:
    """Save a fal result, refusing anything over MAX_FAL_RESULT_BYTES (it lands in memory on Cloud Run)."""
    size = 0
    with urllib.request.urlopen(url, timeout=120) as resp, out.open("wb") as f:
        while chunk := resp.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FAL_RESULT_BYTES:
                raise RuntimeError(f"fal result is over {MAX_FAL_RESULT_BYTES // (1024 * 1024)} MB")
            f.write(chunk)
    return out


def talking_clip(face: Path, voice: Path, start: float, end: float, out: Path, deadline: float) -> Path:
    """Lip-sync the persona photo to voice[start:end] with fal and save the clip as `out`.
    The audio is cut from the narration itself, so the lips match what the viewer hears."""
    segment = out.with_suffix(".mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                    "-i", str(voice), "-ac", "1", "-b:a", "64k", str(segment)],
                   check=True, capture_output=True, timeout=60)
    result = _fal_paid("talking", FAL_TALKING_MODEL, {
        "image_url": _fal_file_url(face, "image/jpeg"),
        "audio_url": _fal_file_url(segment, "audio/mpeg"),
    }, deadline, end - start)
    return _download(result["video"]["url"], out)


MOTION_DEFAULT_PROMPT = ("Subtle, natural motion in the scene; gentle slow camera push-in. "
                         "Keep the people, objects and composition the same.")


def motion_clip(image: Path, prompt: Optional[str], seconds: float, out: Path, deadline: float) -> Path:
    """Animate a scene image with fal (Kling) and retime the clip to exactly `seconds`."""
    still = out.with_name(out.stem + "-still.jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(image),
                    "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(still)],
                   check=True, capture_output=True, timeout=60)
    duration = min(10, max(3, math.ceil(seconds)))
    result = _fal_paid("motion", FAL_MOTION_MODEL, {
        "start_image_url": _fal_file_url(still, "image/jpeg"),
        "prompt": (prompt or "").strip() or MOTION_DEFAULT_PROMPT,
        "duration": str(duration),
        "generate_audio": False,  # the reel has its own narration and music
        "negative_prompt": "blur, distort, low quality, text, captions, watermark, morphing faces",
    }, deadline, duration)
    raw = _download(result["video"]["url"], out.with_name(out.stem + "-raw.mp4"))
    # Stretch or squeeze to the scene's length so the clip neither freezes nor gets cut short.
    factor = seconds / (probe_duration(raw) or duration)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-an",
                    "-vf", f"setpts={factor:.4f}*PTS", "-t", f"{seconds:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)],
                   check=True, capture_output=True, timeout=180)
    return out


H3_DEFAULT_PROMPT = ("One continuous cinematic shot with a slow, smooth camera move. Keep the place, objects, "
                     "people and colours the same. No text.")


def h3_motion_clip(image: Path, prompt: Optional[str], seconds: float, out: Path,
                   deadline: float, aspect_ratio: str = "9:16") -> Path:
    """Animate a scene with fal H3 Max: it opens on `image` and moves for the whole clip. Retimed to
    exactly `seconds`. No end keyframe: given one, H3 doesn't move into it, it hard-cuts to it partway
    through (1.1-2.3 s into a 2-3 s clip, measured 23 Oct 2026), which put the next scene's picture
    under the current caption. The next scene's own clip opens on that picture anyway."""
    still = out.with_name(f"{out.stem}-start.jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(image),
                    "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(still)],
                   check=True, capture_output=True, timeout=60)
    start_url = _fal_file_url(still, "image/jpeg")
    duration = min(15, max(2, math.ceil(seconds)))
    body = {
        "prompt": (prompt or "").strip() or H3_DEFAULT_PROMPT,
        "image_url": start_url,
        "reference_image_urls": [start_url],  # H3 needs at least one
        "duration": duration,
        "resolution": "768P",
        "aspect_ratio": aspect_ratio,
        "prompt_expansion_mode": "balanced",
    }
    result = _fal_paid("motion", FAL_H3_MOTION_MODEL, body, deadline, duration)
    raw = _download(result["video"]["url"], out.with_name(out.stem + "-raw.mp4"))
    factor = seconds / (probe_duration(raw) or duration)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-an",
                    "-vf", f"setpts={factor:.4f}*PTS", "-t", f"{seconds:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)],
                   check=True, capture_output=True, timeout=180)
    return out


def add_motion_scenes(props: dict, req: RenderRequest, job_dir: Path, image_paths: list[Path],
                      failures: Optional[list] = None) -> int:
    """Swap the requested scenes for animated clips; a scene whose clip fails keeps its image (and is
    added to `failures`). Card scenes and talking scenes are skipped. Returns how many scenes now move."""
    cuts = props["cuts"]
    talking = set(req.talkingScenes) if req.talkingImage else set()
    wanted = sorted({i for i in req.motionScenes
                     if 0 <= i < len(req.scenes) and not req.scenes[i].card and i not in talking})
    h3 = req.motionEngine == "h3"
    wanted = wanted[:MAX_H3_MOTION_SCENES if h3 else MAX_MOTION_SCENES]
    if not (wanted and FAL_KEY):
        return 0
    deadline = time.monotonic() + MOTION_TIMEOUT_SECONDS
    moved = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(wanted), MOTION_WORKERS)) as pool:
        futures = {
            (pool.submit(h3_motion_clip, image_paths[i], req.scenes[i].motionPrompt,
                         cuts[i]["out_seconds"] - cuts[i]["in_seconds"], job_dir / f"motion-{i + 1}.mp4",
                         deadline, req.aspectRatio)
             if h3 else
             pool.submit(motion_clip, image_paths[i], req.scenes[i].motionPrompt,
                         cuts[i]["out_seconds"] - cuts[i]["in_seconds"], job_dir / f"motion-{i + 1}.mp4",
                         deadline)): i
            for i in wanted
        }
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            try:
                clip = future.result()
            except Exception as exc:  # fal down, slow or out of credit: keep the image
                print(f"[motion] scene {i + 1} kept its image: {exc}", flush=True)
                if failures is not None:
                    failures.append(f"motion scene {i + 1}")
                continue
            cuts[i]["source"] = clip.name
            moved += 1
    return moved


def add_talking_scenes(props: dict, req: RenderRequest, job_dir: Path, voice_path: Optional[Path],
                       failures: Optional[list] = None) -> int:
    """Swap the requested scenes for talking clips; a scene whose clip fails keeps its image (and is
    added to `failures`). Returns how many scenes now talk."""
    cuts = props["cuts"]
    wanted = sorted({i for i in req.talkingScenes if 0 <= i < len(req.scenes)})
    if not (wanted and req.talkingImage and voice_path and FAL_KEY):
        return 0
    raw, ext = decode_data_url(req.talkingImage, "image/")
    original = job_dir / f"face-original{ext}"
    original.write_bytes(raw)
    face = job_dir / "face.jpg"  # small JPEG keeps the inline upload light
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(original),
                    "-vf", "scale='min(768,iw)':-2", "-q:v", "3", str(face)],
                   check=True, capture_output=True, timeout=60)
    deadline = time.monotonic() + TALKING_TIMEOUT_SECONDS
    talked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        futures = {
            pool.submit(talking_clip, face, voice_path, cuts[i]["in_seconds"], cuts[i]["out_seconds"],
                        job_dir / f"talk-{i + 1}.mp4", deadline): i
            for i in wanted
        }
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            try:
                clip = future.result()
            except Exception as exc:  # fal down, slow or out of credit: keep the image
                print(f"[talking] scene {i + 1} kept its image: {exc}", flush=True)
                if failures is not None:
                    failures.append(f"talking scene {i + 1}")
                continue
            cuts[i]["source"] = clip.name
            talked += 1
    return talked


def video_response(path: Path, filename: str, headers: Optional[dict] = None) -> StreamingResponse:
    """Send the MP4 in chunks, without Content-Length. Cloud Run refuses a response sent in one
    piece if it's over 32 MB, and reels with motion or talking clips can be larger than that.
    The file is finished before this is called, so X-Expected-Bytes lets the client spot a cut-off stream."""
    size = path.stat().st_size

    def chunks():
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "X-Expected-Bytes": str(size),
        **(headers or {})})


def card_clip_video(card_image: Path, clip: CardClip, stem: Path) -> Path:
    """The card with its clip playing in the torn frame, as a video the clip's length (CardClip)."""
    raw, ext = decode_data_url(clip.video, "video/")
    src = stem.with_name(f"{stem.name}-src{ext or '.mp4'}")
    src.write_bytes(raw)
    mraw, _ = decode_data_url(clip.mask, "image/")
    mask = stem.with_name(f"{stem.name}-mask.png")
    mask.write_bytes(mraw)
    out = stem.with_name(f"{stem.name}-card.mp4")
    graph = (f"[1:v]fps=30,crop={clip.sw:.2f}:{clip.sh:.2f}:{clip.sx:.2f}:{clip.sy:.2f},scale={clip.w}:{clip.h},"
             "setsar=1,format=yuva420p[c];"
             f"[2:v]scale={clip.w}:{clip.h},format=gray[m];"
             "[c][m]alphamerge[cm];"
             "[0:v]scale=1080:1920,setsar=1,format=yuv420p[bg];"
             f"[bg][cm]overlay={clip.x}:{clip.y}:shortest=1:format=auto,format=yuv420p[v]")
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-i", str(card_image),
                           "-i", str(src), "-i", str(mask), "-filter_complex", graph, "-map", "[v]", "-an",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(out)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(400, f"That card clip can't be placed in the card: {proc.stderr[-300:]}")
    return out


def ping_pong_clip(raw: bytes, ext: str, seconds: float, stem: Path) -> Path:
    """A loop reel's clip: played forward, then backward, repeated to fill the scene, so its end runs on
    into its start and the platform's replay shows no jump (the Hermit card's Omni push-in snapped back
    from close-up to wide on 30 Oct). The sender records about seconds / 2n of the clip; a recording runs
    a few frames long or short, so the clip is retimed to exactly half a there-and-back first (a card
    recorded 0.2 s long ended a loop on the wrong frame, 31 Oct). One there-and-back is frames 0..m-1
    then m-2..1, so the next one starts on frame 0 with no repeated frame."""
    src = stem.with_suffix(ext or ".mp4")
    src.write_bytes(raw)
    have = probe_duration(src) or 0.0
    if have <= 0.1:
        raise HTTPException(400, "That scene clip can't be read.")
    out = stem.parent / f"{stem.name}-pingpong.mp4"
    frames = max(4, round(seconds * 30))
    # The fewest there-and-backs the clip is long enough for; a longer clip plays its first part at its
    # own speed (a whole 9 s Omni clip squeezed into 4 s would rush).
    trips = max(1, math.ceil(seconds / (2 * have) - 0.25))  # up to about a fifth slower, rather than two trips
    m = max(2, round(frames / (2 * trips)) + 1)  # frames in the forward half, both ends included
    used = min(have, seconds / (2 * trips))
    stretch = m / 30 / used
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-an", "-filter_complex",
                           f"[0:v]trim=duration={used:.3f},setpts=PTS-STARTPTS,"
                           f"setpts={stretch:.6f}*PTS,fps=30,trim=end_frame={m},setpts=PTS-STARTPTS,split[f][b];"
                           f"[b]reverse,trim=start_frame=1:end_frame={m - 1},setpts=PTS-STARTPTS[r];"
                           "[f][r]concat=n=2:v=1,loop=loop=-1:size=32767,"
                           f"trim=end_frame={frames},setpts=PTS-STARTPTS[v]",
                           "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(400, f"That scene clip can't be looped: {proc.stderr[-300:]}")
    return out


def fit_clip(raw: bytes, ext: str, seconds: float, stem: Path) -> Path:
    """Save a supplied scene clip; when it's shorter than the scene, hold its last frame to fill it."""
    src = stem.with_suffix(ext or ".mp4")
    src.write_bytes(raw)
    have = probe_duration(src) or 0.0
    if have >= seconds + 0.05:
        return src
    out = stem.parent / f"{stem.name}-fit.mp4"
    pad = seconds - have + 0.1
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-an", "-vf",
                           f"tpad=stop_mode=clone:stop_duration={pad:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                           str(out)], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(400, f"That scene clip can't be read: {proc.stderr[-300:]}")
    return out


def render(props: dict, job_dir: Path, aspect: str) -> Path:
    width, height = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    props_path = job_dir / "props.json"
    props_path.write_text(json.dumps(props), encoding="utf-8")
    out = job_dir / "reel.mp4"
    cmd = [
        "npx", "remotion", "render", "src/index.tsx", "Explainer", str(out),
        "--props", str(props_path), "--codec", "h264", "--public-dir", str(job_dir),
        "--width", str(width), "--height", str(height),
    ]
    try:
        proc = subprocess.run(cmd, cwd=COMPOSER_DIR, capture_output=True, text=True,
                              timeout=RENDER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Render took too long.") from exc
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise HTTPException(500, f"Render failed: {tail}")
    return out


def finish_reel(raw: Path, req: RenderRequest, job_dir: Path) -> tuple[Path, dict]:
    """The label and loudness pass on a rendered reel (finishing.finish). With both switched off the raw
    render is returned as it is. Returns (final file, what was done) for the response headers."""
    if not (req.aiLabel or req.normalizeAudio):
        return raw, {}
    out = job_dir / "final.mp4"
    return out, finishing.finish(raw, out, ai_label=req.aiLabel, normalize=req.normalizeAudio, trim_lead=req.loop)


def finish_headers(info: dict) -> dict:
    """Response headers saying what the finishing pass did: the tag, and the measured loudness and peak."""
    headers = {}
    if "aiLabel" in info:
        headers["X-Ai-Label"] = "1" if info["aiLabel"] else "0"
    if info.get("lufs") is not None:
        headers["X-Loudness-Lufs"] = f"{info['lufs']:.1f}"
    if info.get("truePeak") is not None:
        headers["X-True-Peak-Dbtp"] = f"{info['truePeak']:.1f}"
    return headers


@app.get("/health")  # Cloud Run reserves paths ending in "z", so /healthz never reaches the app there
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "composer": (COMPOSER_DIR / "node_modules").exists(), "talking": bool(FAL_KEY),
            "motion": bool(FAL_KEY), "motionEngines": ["kling", "h3"] if FAL_KEY else []}


@app.post("/render")
def render_reel(req: RenderRequest, background: BackgroundTasks) -> StreamingResponse:
    job_dir = Path(tempfile.mkdtemp(prefix=f"reel-{uuid.uuid4().hex[:8]}-"))
    background.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    try:
        image_paths = []
        for i, scene in enumerate(req.scenes):
            raw, ext = decode_data_url(scene.image, "image/")
            path = job_dir / f"scene-{i + 1}{ext}"
            path.write_bytes(raw)
            image_paths.append(path)

        voice_path, voice_seconds = None, None
        if req.voiceover:
            raw, ext = decode_data_url(req.voiceover, "audio/")
            voice_path = job_dir / f"voiceover{ext}"
            voice_path.write_bytes(raw)
            voice_seconds = probe_duration(voice_path)

        music_path = None
        if req.music:
            raw, ext = decode_data_url(req.music, "audio/")
            music_path = job_dir / f"music{ext}"
            music_path.write_bytes(raw)

        heard = transcribe(voice_path) if voice_path else None
        props = build_props(req, image_paths, voice_path, voice_seconds, heard)
        for i, scene in enumerate(req.scenes):
            cut = props["cuts"][i]
            seconds = cut["out_seconds"] - cut["in_seconds"]
            stem = job_dir / f"clip-{i + 1}"
            if scene.cardClip:
                card = card_clip_video(image_paths[i], scene.cardClip, stem)
                raw, have = card.read_bytes(), probe_duration(card) or 0.0
                # Shorter than the scene (or a loop): there and back, never frozen on its last frame.
                clip = ping_pong_clip(raw, ".mp4", seconds, stem) if req.loop or have < seconds else fit_clip(raw, ".mp4", seconds, stem)
                cut["source"] = clip.name
                cut["animateVideo"] = True  # the card's camera move applies to the clip too
            elif scene.video:
                raw, ext = decode_data_url(scene.video, "video/")
                clip = ping_pong_clip(raw, ext, seconds, stem) if req.loop else fit_clip(raw, ext, seconds, stem)
                cut["source"] = clip.name
        # Taken before fal is called, so a request that is turned away has paid for nothing.
        if not _render_lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise HTTPException(429, "Another reel is rendering. Try again in a minute.")
        try:
            # Talking and motion clips come from fal at the same time.
            fal_failures: list[str] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                talking_job = pool.submit(add_talking_scenes, props, req, job_dir, voice_path, failures=fal_failures)
                motion_job = pool.submit(add_motion_scenes, props, req, job_dir, image_paths, failures=fal_failures)
                talked, moved = talking_job.result(), motion_job.result()
            if music_path:
                total = max(c["out_seconds"] for c in props["cuts"]) + TAIL_SECONDS
                soundtrack = mix_music(voice_path, music_path, total, job_dir, req.musicVolume, req.loop)
                props["audio"]["narration"] = {"src": soundtrack.name, "volume": 1}
            if req.sfx:
                narration = props["audio"].get("narration")
                base = job_dir / narration["src"] if narration else None
                total = max(c["out_seconds"] for c in props["cuts"]) + TAIL_SECONDS
                try:
                    mixed = mix_sfx(base, sfx_events(props), total, job_dir)
                    props["audio"]["narration"] = {"src": mixed.name, "volume": 1}
                except Exception as exc:  # effects are a nice-to-have; render without them
                    print(f"[sfx] skipped: {exc}", flush=True)
            out = render(props, job_dir, req.aspectRatio)
            # The tag and the loudness pass, inside the lock: it re-encodes the video on the same CPUs.
            out, finished = finish_reel(out, req, job_dir)
        finally:
            _render_lock.release()
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    headers = {"X-Fal-Failures": str(len(fal_failures)), **finish_headers(finished)}
    if req.talkingScenes:
        headers["X-Talking-Scenes"] = f"{talked}/{len(set(req.talkingScenes))}"
    if req.motionScenes:
        headers["X-Motion-Scenes"] = f"{moved}/{len(set(req.motionScenes))}"
    return video_response(out, "reel-hd.mp4", headers)


# ---------------------------------------------------------------------------
# Clips: long video (Google Drive link) -> transcript -> vertical, captioned short clips.
# The app picks the moments from the transcript; this service downloads, transcribes and cuts.
# ---------------------------------------------------------------------------

# /tmp is memory on Cloud Run, so the cache is kept under CLIP_CACHE_MAX_MB by dropping the oldest
# sources before a new download.
CLIP_CACHE = Path(os.environ.get("CLIP_CACHE", "/tmp/clip-sources"))
CLIP_CACHE_MAX_BYTES = int(os.environ.get("CLIP_CACHE_MAX_MB", "2500")) * 1024 * 1024
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_MB", "1500")) * 1024 * 1024
MAX_SOURCE_MINUTES = float(os.environ.get("MAX_SOURCE_MINUTES", "90"))
DRIVE_DOWNLOAD_DEADLINE_S = float(os.environ.get("DRIVE_DOWNLOAD_DEADLINE_S", "600"))
# A source handed out this recently may still be read by its request (Cloud Run ends requests at 900 s).
SOURCE_IN_USE_SECONDS = 900
MAX_CLIP_SECONDS = 90
DRIVE_ID_RE = re.compile(r"(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})")
_source_locks: dict[str, threading.Lock] = {}
_source_locks_guard = threading.Lock()
_cache_guard = threading.Lock()
_cache_reserved: dict[str, int] = {}  # downloads in progress: file id -> bytes expected


def drive_file_id(url: str) -> str:
    """The file id in a Google Drive share link (…/file/d/<id>/view or …?id=<id>)."""
    match = DRIVE_ID_RE.search(url or "")
    if not match:
        raise HTTPException(400, "That doesn't look like a Google Drive file link.")
    return match.group(1)


def make_room(cache: Path, needed: int, file_id: str) -> None:
    """Delete the oldest cached sources until the cache plus `needed` bytes fits CLIP_CACHE_MAX_BYTES.
    A source whose lock is held, or that was handed out within SOURCE_IN_USE_SECONDS, is kept (other
    downloads count at their expected size); when the rest can't make room the download is refused
    instead of filling memory."""
    with _cache_guard:
        files = [(f.stat().st_mtime, f.stat().st_size, f) for f in cache.glob("*.video")]
        total = sum(size for _, size, _ in files) + sum(_cache_reserved.values())
        now = time.time()
        for mtime, size, f in sorted(files, key=lambda x: x[0]):
            if total + needed <= CLIP_CACHE_MAX_BYTES:
                break
            lock = _source_locks.get(f.stem)
            if (lock and lock.locked()) or now - mtime < SOURCE_IN_USE_SECONDS:
                continue
            f.unlink(missing_ok=True)
            total -= size
            print(f"[clips] dropped cached source {f.name} ({size // (1024 * 1024)} MB) to make room", flush=True)
        if total + needed > CLIP_CACHE_MAX_BYTES:
            raise HTTPException(429, "The render service is busy with other videos. Try again in a few minutes.")
        _cache_reserved[file_id] = needed


def _download_drive(file_id: str, dest: Path) -> None:
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    tmp = dest.with_suffix(".part")
    deadline = time.monotonic() + DRIVE_DOWNLOAD_DEADLINE_S
    too_big = f"That video is over {MAX_SOURCE_BYTES // (1024 * 1024)} MB."
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            if "text/html" in (resp.headers.get("Content-Type") or ""):
                raise HTTPException(400, "Google Drive didn't send the video. Share it as "
                                         "\"Anyone with the link\" and try again.")
            expected = int(resp.headers.get("Content-Length") or MAX_SOURCE_BYTES)
            if expected > MAX_SOURCE_BYTES:
                raise HTTPException(400, too_big)
            make_room(dest.parent, expected, file_id)
            size = 0
            with tmp.open("wb") as out:
                while chunk := resp.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise HTTPException(400, too_big)
                    if time.monotonic() > deadline:
                        raise HTTPException(504, "Google Drive took too long to send the video.")
                    out.write(chunk)
        # Checked before it joins the cache, so a video that will be refused doesn't take up memory.
        duration = probe_duration(tmp)
        if not duration:
            raise HTTPException(400, "That file isn't a video we can read.")
        if duration > MAX_SOURCE_MINUTES * 60:
            raise HTTPException(400, f"That video is over {int(MAX_SOURCE_MINUTES)} minutes.")
        tmp.rename(dest)
    except urllib.error.HTTPError as exc:
        raise HTTPException(400, f"Google Drive refused the download ({exc.code}). "
                                 "Check the link is shared as \"Anyone with the link\".") from exc
    finally:
        tmp.unlink(missing_ok=True)
        with _cache_guard:
            _cache_reserved.pop(file_id, None)


def source_video(drive_url: str) -> Path:
    """The Drive video on local disk, downloaded once per instance and reused for every clip."""
    file_id = drive_file_id(drive_url)
    with _source_locks_guard:
        lock = _source_locks.setdefault(file_id, threading.Lock())
    with lock:
        CLIP_CACHE.mkdir(parents=True, exist_ok=True)
        path = CLIP_CACHE / f"{file_id}.video"
        if path.exists():
            os.utime(path)  # marks it in use, and most recently used, for make_room
        else:
            _download_drive(file_id, path)
        return path


_extra_whisper_models: dict[str, object] = {}


def transcribe_segments(path: Path, language: Optional[str] = None, model_name: Optional[str] = None) -> list[dict]:
    """Sentence-ish segments with start/end times for a long recording."""
    from faster_whisper import WhisperModel
    global _whisper_model
    with _whisper_lock:
        if model_name and model_name != WHISPER_MODEL:
            if model_name not in _extra_whisper_models:
                _extra_whisper_models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
            model = _extra_whisper_models[model_name]
        else:
            if _whisper_model is None:
                _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            model = _whisper_model
        segments, _ = model.transcribe(str(path), beam_size=1, vad_filter=True, language=language)
        return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                for s in segments if s.text.strip()]


class ClipSourceRequest(BaseModel):
    driveUrl: str


class ClipRenderRequest(BaseModel):
    driveUrl: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    captionStyle: CaptionStyle = "yellow-pop"
    captions: bool = True


@app.post("/clips/transcribe")
def clips_transcribe(req: ClipSourceRequest) -> dict:
    src = source_video(req.driveUrl)
    duration = probe_duration(src)
    if not duration:
        raise HTTPException(400, "That file isn't a video we can read.")
    if duration > MAX_SOURCE_MINUTES * 60:
        raise HTTPException(400, f"That video is over {int(MAX_SOURCE_MINUTES)} minutes.")
    with tempfile.TemporaryDirectory(prefix="clipsrc-") as tmp:
        audio = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        str(audio)], check=True, capture_output=True, timeout=600)
        segments = transcribe_segments(audio)
    return {"duration": round(duration, 2), "segments": segments}


def probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    width, height = (int(v) for v in out.split(",")[:2])
    return width, height


def face_center_x(clip: Path, duration: float, samples: int = 8) -> Optional[float]:
    """Where the speaker's face sits across the clip, as a 0-1 fraction of the width (median of a
    few sampled frames). None when OpenCV isn't installed or no face is found."""
    try:
        import cv2
    except ImportError:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    centers = []
    with tempfile.TemporaryDirectory(prefix="faces-") as tmp:
        for i in range(samples):
            frame = Path(tmp) / f"f{i}.jpg"
            t = duration * (i + 0.5) / samples
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(clip),
                            "-frames:v", "1", "-vf", "scale=640:-2", str(frame)], capture_output=True, timeout=60)
            img = cv2.imread(str(frame))
            if img is None:
                continue
            faces = cascade.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 5, minSize=(40, 40))
            if len(faces):
                x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
                centers.append((x + w / 2) / img.shape[1])
    if not centers:
        return None
    centers.sort()
    return centers[len(centers) // 2]


def vertical_crop(src_w: int, src_h: int, center_x: Optional[float]) -> tuple[int, int, int, int]:
    """(w, h, x, y) of the largest 9:16 window, centred on the face when known."""
    w, h = src_w, src_h
    if src_w / src_h > 9 / 16:
        w = int(src_h * 9 / 16) // 2 * 2
    else:
        h = int(src_w * 16 / 9) // 2 * 2
    cx = (center_x if center_x is not None else 0.5) * src_w
    x = int(min(max(cx - w / 2, 0), src_w - w))
    y = (src_h - h) // 2
    return w, h, x, y


def clip_captions(heard: list[tuple[str, float, float]]) -> list[dict]:
    """Word captions straight from what was heard; a page ends after each sentence."""
    return [{"word": word, "startMs": round(start * 1000), "endMs": round(end * 1000),
             "pageBreakAfter": bool(re.search(r"[.!?]$", word))}
            for word, start, end in heard]


@app.post("/clips/render")
def clips_render(req: ClipRenderRequest, background: BackgroundTasks) -> StreamingResponse:
    if req.end - req.start > MAX_CLIP_SECONDS or req.end <= req.start:
        raise HTTPException(400, f"A clip must be between 1 and {MAX_CLIP_SECONDS} seconds.")
    src = source_video(req.driveUrl)
    job_dir = Path(tempfile.mkdtemp(prefix=f"clip-{uuid.uuid4().hex[:8]}-"))
    background.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    try:
        cut, audio = job_dir / "cut.mp4", job_dir / "audio.wav"
        span = ["-ss", f"{req.start:.2f}", "-to", f"{req.end:.2f}", "-i", str(src)]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *span, "-an", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "20", str(cut)], check=True, capture_output=True, timeout=600)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *span, "-vn", "-ac", "2", "-ar", "48000",
                        str(audio)], check=True, capture_output=True, timeout=300)
        duration = probe_duration(cut) or (req.end - req.start)
        src_w, src_h = probe_size(cut)
        w, h, x, y = vertical_crop(src_w, src_h, face_center_x(cut, duration))
        vertical = job_dir / "clip.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(cut), "-vf",
                        f"crop={w}:{h}:{x}:{y},scale=1080:1920", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", str(vertical)], check=True, capture_output=True, timeout=600)
        heard = transcribe(audio) if req.captions else None
        props = {
            "cuts": [{"id": "clip", "source": vertical.name, "in_seconds": 0, "out_seconds": duration}],
            "overlays": [],
            "captions": clip_captions(heard or []),
            "audio": {"narration": {"src": audio.name, "volume": 1}},
            "plainBackground": True,
            "captionFontSize": 76,
            "captionWordsPerPage": 4,
            "captionBottomOffset": 460,
            "themeConfig": {
                "backgroundColor": "#000000",
                "surfaceColor": "#000000",
                "textColor": "#FFFFFF",
                "captionHighlightColor": CAPTION_COLORS[req.captionStyle],
                "captionBackgroundColor": "rgba(0, 0, 0, 0.7)",
            },
        }
        if not _render_lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise HTTPException(429, "Another video is rendering. Try again in a minute.")
        try:
            out = render(props, job_dir, "9:16")
        finally:
            _render_lock.release()
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Cutting the clip failed: {(exc.stderr or b'')[-300:]!r}") from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return video_response(out, "clip.mp4")


# ---------------------------------------------------------------------------
# Dub: a video on Google Drive -> transcript in its own language (the app translates it and makes
# the new voices) -> the new voices laid over the original music and effects, with the old burned-in
# subtitles covered and new ones added.
# ---------------------------------------------------------------------------

# "small" hears Chinese far better than "base"; it is downloaded into the image at build time.
DUB_WHISPER_MODEL = os.environ.get("DUB_WHISPER_MODEL", "small")
FAL_STEMS_MODEL = os.environ.get("FAL_STEMS_MODEL", "fal-ai/demucs")
STEMS_TIMEOUT_SECONDS = int(os.environ.get("STEMS_TIMEOUT_SECONDS", "300"))
MAX_DUB_LINES = 400
MAX_DUB_SECONDS = 15 * 60
MAX_SPEEDUP = 1.35  # a line may be sped up this much to fit before the next one starts
VOICE_RATE = 48000


class DubTranscribeRequest(BaseModel):
    driveUrl: str
    language: str = Field("zh", max_length=8)


class DubLine(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    audio: str  # the new voice for this line, as an audio data URL
    text: Optional[str] = Field(None, max_length=500)  # subtitle text


class DubRenderRequest(BaseModel):
    driveUrl: str
    lines: list[DubLine] = Field(min_length=1, max_length=MAX_DUB_LINES)
    keepBackground: bool = True
    coverSubtitles: bool = True
    # Where the original burned-in subtitles sit, as fractions of the frame height.
    coverTop: float = Field(0.78, ge=0, le=1)
    coverBottom: float = Field(0.94, ge=0, le=1)
    subtitles: bool = True
    # Optional Drive link to a short clip of the creator (e.g. "Idub natin sa Filipino!") played first.
    introUrl: Optional[str] = Field(None, max_length=500)


MAX_INTRO_SECONDS = 15


def _has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30).stdout
    return bool(out.strip())


def prepend_intro(intro: Path, main: Path, width: int, height: int, out: Path) -> Path:
    """The intro clip, fitted to the episode's frame (letterboxed if its shape differs), then the episode."""
    duration = probe_duration(intro)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(intro), "-i", str(main)]
    intro_audio = "[0:a]"
    if not _has_audio(intro):
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"anullsrc=r={VOICE_RATE}:cl=stereo"]
        intro_audio = "[2:a]"
    norm_a = f"aresample={VOICE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
    graph = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[iv];"
             f"[1:v]setsar=1,fps=30,format=yuv420p[mv];"
             f"{intro_audio}{norm_a}[ia];[1:a]{norm_a}[ma];"
             f"[iv][ia][mv][ma]concat=n=2:v=1:a=1[v][a]")
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    return out


def segment_pitch(audio: Path, segments: list[dict]) -> None:
    """Add a rough voice pitch (Hz) to each segment, from the 16 kHz mono track: the median of the
    autocorrelation pitch over voiced 40 ms frames. Men speak around 85-160 Hz, women 165-260 Hz;
    with music underneath it's a hint, not a measurement."""
    import numpy as np
    import wave
    with wave.open(str(audio)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    win, hop = int(rate * 0.04), int(rate * 0.02)
    lo, hi = int(rate / 400), int(rate / 70)  # lags for 70-400 Hz
    for seg in segments:
        chunk = pcm[int(seg["start"] * rate): int(seg["end"] * rate)]
        starts = list(range(0, max(0, len(chunk) - win), hop))
        if len(starts) > 40:  # 40 evenly spaced frames per line is plenty for a median
            starts = [starts[int(i * (len(starts) - 1) / 39)] for i in range(40)]
        pitches = []
        for at in starts:
            frame = chunk[at: at + win]
            frame = frame - frame.mean()
            energy = float(np.dot(frame, frame))
            if energy < 1e-4:
                continue
            spectrum = np.fft.rfft(frame, 2 * win)
            ac = np.fft.irfft(spectrum * np.conj(spectrum))[:win]
            lag = lo + int(np.argmax(ac[lo:hi]))
            if ac[lag] / ac[0] > 0.45:  # clearly periodic: voiced speech
                pitches.append(rate / lag)
        seg["pitch"] = round(float(np.median(pitches))) if len(pitches) >= 3 else None


def segment_frames(src: Path, segments: list[dict], count: int = 3) -> list[str]:
    """A few small JPEG stills taken while people speak, so any burned-in subtitles are on screen."""
    if not segments:
        return []
    indexes = sorted({int(i * (len(segments) - 1) / max(1, count - 1)) for i in range(count)})
    frames = []
    for seg in (segments[i] for i in indexes):
        at = (seg["start"] + seg["end"]) / 2
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                               "-vf", "scale=-2:640", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
                              capture_output=True, timeout=60)
        if proc.returncode == 0 and proc.stdout:
            frames.append("data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode())
    return frames


def _gray_still(src: Path, at: float, height: int = 640):
    import numpy as np
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                           "-vf", f"scale=-2:{height}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, timeout=60)
    if proc.returncode or len(proc.stdout) < height:
        return None
    return np.frombuffer(proc.stdout, np.uint8).reshape(height, -1)


def subtitle_text_rows(gray) -> "np.ndarray":
    """Per image row, the share of pixels that look like burned-in subtitle text: bright pixels with a dark
    outline a few pixels to the left or right."""
    import numpy as np
    bright, dark = gray > 215, gray < 60
    near = np.zeros_like(dark)
    for d in (2, 3, 4):
        near |= np.roll(dark, d, 1) | np.roll(dark, -d, 1)
    return (bright & near).mean(1)


def find_subtitle_band(stills: list) -> Optional[dict]:
    """Where burned-in subtitles sit, as fractions of the height, from stills taken while people speak.
    Subtitle rows score high in most stills at the same height; the scene behind them changes."""
    import numpy as np
    stills = [s for s in stills if s is not None]
    if len(stills) < 2:
        return None
    rows = np.percentile([subtitle_text_rows(s) for s in stills], 40, axis=0)
    height = len(rows)
    lo = int(height * 0.3)  # dialogue subtitles sit in the lower part of the frame
    floor = float(np.median(rows[lo:]))
    hits = np.where(rows[lo:] > max(0.03, floor * 4))[0] + lo
    if not len(hits):
        return None
    # Group hit rows that are close together (two-line subtitles) and keep the strongest group.
    groups, start = [], hits[0]
    for a, b in zip(hits, hits[1:]):
        if b - a > height * 0.03:
            groups.append((start, a))
            start = b
    groups.append((start, hits[-1]))
    top, bottom = max(groups, key=lambda g: rows[g[0]: g[1] + 1].sum())
    if bottom - top < height * 0.012 or bottom - top > height * 0.2:
        return None
    pad = height * 0.02
    return {"top": round(float(max(0.0, (top - pad) / height)), 3),
            "bottom": round(float(min(1.0, (bottom + pad) / height)), 3)}


def segment_subtitle_band(src: Path, segments: list[dict], count: int = 5) -> Optional[dict]:
    if not segments:
        return None
    indexes = sorted({int(i * (len(segments) - 1) / max(1, count - 1)) for i in range(count)})
    return find_subtitle_band([_gray_still(src, (segments[i]["start"] + segments[i]["end"]) / 2) for i in indexes])


def segment_subtitle_crops(src: Path, segments: list[dict], band: dict) -> None:
    """Add to each segment a small JPEG of the burned-in subtitle band at its midpoint, so the translator
    can read what the subtitle says when the speech recognition mishears a word."""
    top, bottom = band["top"], band["bottom"]

    def crop(seg: dict) -> Optional[str]:
        at = (seg["start"] + seg["end"]) / 2
        vf = f"crop=iw:ih*{bottom - top:.4f}:0:ih*{top:.4f},scale=480:-2"
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                               "-vf", vf, "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "-"],
                              capture_output=True, timeout=60)
        if proc.returncode or not proc.stdout:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for seg, image in zip(segments, pool.map(crop, segments)):
            seg["subtitleImage"] = image


@app.post("/dub/transcribe")
def dub_transcribe(req: DubTranscribeRequest) -> dict:
    src = source_video(req.driveUrl)
    duration = probe_duration(src)
    if not duration:
        raise HTTPException(400, "That file isn't a video we can read.")
    if duration > MAX_DUB_SECONDS:
        raise HTTPException(400, f"Dub one episode at a time (up to {MAX_DUB_SECONDS // 60} minutes).")
    with tempfile.TemporaryDirectory(prefix="dubsrc-") as tmp:
        audio = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        str(audio)], check=True, capture_output=True, timeout=600)
        segments = transcribe_segments(audio, language=req.language or None, model_name=DUB_WHISPER_MODEL)
        try:
            segment_pitch(audio, segments)
        except Exception as exc:  # a hint only; never fail the transcript over it
            print(f"[dub] pitch estimate skipped: {exc}", flush=True)
    width, height = probe_size(src)
    try:
        band = segment_subtitle_band(src, segments)
    except Exception as exc:  # a hint only
        print(f"[dub] subtitle search skipped: {exc}", flush=True)
        band = None
    if band:
        try:
            segment_subtitle_crops(src, segments, band)
        except Exception as exc:  # a hint only
            print(f"[dub] subtitle crops skipped: {exc}", flush=True)
    return {"duration": round(duration, 2), "width": width, "height": height, "segments": segments,
            "frames": segment_frames(src, segments), "subtitleBand": band}


def background_track(audio: Path, job_dir: Path) -> Path:
    """Music and effects without the voices: Demucs on fal splits the soundtrack into stems, and
    everything except the vocals is mixed back together."""
    small = job_dir / "for-stems.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio), "-b:a", "192k", str(small)],
                   check=True, capture_output=True, timeout=180)
    result = _fal_paid("stems", FAL_STEMS_MODEL, {
        "audio_url": _fal_file_url(small, "audio/mpeg"),
        "model": "htdemucs",
        "stems": ["drums", "bass", "other"],
        "output_format": "mp3",
    }, time.monotonic() + STEMS_TIMEOUT_SECONDS, probe_duration(small) or 0.0)
    stems = []
    for name in ("drums", "bass", "other"):
        url = (result.get(name) or {}).get("url")
        if url:
            stems.append(_download(url, job_dir / f"stem-{name}.mp3"))
    if not stems:
        raise RuntimeError("the stem model returned no stems")
    out = job_dir / "background.wav"
    inputs = [arg for stem in stems for arg in ("-i", str(stem))]
    labels = "".join(f"[{i}:a]" for i in range(len(stems)))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
                    f"{labels}amix=inputs={len(stems)}:duration=longest:normalize=0[out]",
                    "-map", "[out]", "-ac", "2", "-ar", str(VOICE_RATE), str(out)],
                   check=True, capture_output=True, timeout=180)
    return out


def _decode_line(path: Path, tempo: float) -> "np.ndarray":
    import numpy as np
    filters = [f"atempo={tempo:.3f}"] if tempo > 1.001 else []
    cmd = ["ffmpeg", "-loglevel", "error", "-i", str(path), *(["-af", ",".join(filters)] if filters else []),
           "-f", "s16le", "-ac", "1", "-ar", str(VOICE_RATE), "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32)


def voice_track(lines: list[DubLine], paths: list[Path], total: float, out: Path) -> Path:
    """All the new lines on one track, each starting where the original line did. A line that runs
    into the next one is sped up a little (at most MAX_SPEEDUP)."""
    import numpy as np
    import wave
    buffer = np.zeros(int(total * VOICE_RATE) + VOICE_RATE, dtype=np.float32)
    order = sorted(range(len(lines)), key=lambda i: lines[i].start)
    for rank, i in enumerate(order):
        line = lines[i]
        next_start = lines[order[rank + 1]].start if rank + 1 < len(order) else total
        room = max(next_start - line.start, line.end - line.start, 0.3)
        length = probe_duration(paths[i]) or room
        tempo = min(MAX_SPEEDUP, length / room) if length > room else 1.0
        pcm = _decode_line(paths[i], tempo)
        at = int(line.start * VOICE_RATE)
        end = min(at + len(pcm), len(buffer))
        buffer[at:end] += pcm[: end - at]
    peak = float(np.max(np.abs(buffer))) or 1.0
    if peak > 32000:
        buffer *= 32000 / peak
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(VOICE_RATE)
        w.writeframes(buffer.astype(np.int16).tobytes())
    return out


def _ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def subtitle_file(lines: list[DubLine], width: int, height: int, bottom_margin: int, out: Path) -> Path:
    """An ASS subtitle file: bold white text with a dark outline, centred in the covered band."""
    size = max(28, round(height * 0.042))
    rows = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 0", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, "
            "MarginL, MarginR, MarginV, BorderStyle, Outline, Shadow",
            f"Style: Default,Liberation Sans,{size},&H00FFFFFF,&H00000000,&H64000000,-1,2,"
            f"{round(width * 0.06)},{round(width * 0.06)},{bottom_margin},1,{max(2, size // 14)},0",
            "", "[Events]", "Format: Layer, Start, End, Style, Text"]
    for line in sorted(lines, key=lambda l: l.start):
        text = (line.text or "").replace("\n", " ").replace("{", "(").replace("}", ")").strip()
        if text:
            rows.append(f"Dialogue: 0,{_ass_time(line.start)},{_ass_time(line.end)},Default,{text}")
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return out


@app.post("/dub/render")
def dub_render(req: DubRenderRequest, background: BackgroundTasks) -> StreamingResponse:
    src = source_video(req.driveUrl)
    total = probe_duration(src)
    if not total:
        raise HTTPException(400, "That file isn't a video we can read.")
    if req.coverBottom <= req.coverTop:
        raise HTTPException(400, "The subtitle cover's bottom edge must be below its top edge.")
    intro: Optional[Path] = None
    if req.introUrl and req.introUrl.strip():
        intro = source_video(req.introUrl.strip())
        intro_seconds = probe_duration(intro)
        if not intro_seconds:
            raise HTTPException(400, "The intro clip isn't a video we can read.")
        if intro_seconds > MAX_INTRO_SECONDS:
            raise HTTPException(400, f"Keep the intro clip under {MAX_INTRO_SECONDS} seconds.")
    job_dir = Path(tempfile.mkdtemp(prefix=f"dub-{uuid.uuid4().hex[:8]}-"))
    background.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    if not _render_lock.acquire(timeout=LOCK_WAIT_SECONDS):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(429, "Another video is rendering. Try again in a minute.")
    try:
        paths = []
        for n, line in enumerate(req.lines):
            raw, ext = decode_data_url(line.audio, "audio/")
            path = job_dir / f"line-{n:03d}{ext}"
            path.write_bytes(raw)
            paths.append(path)
        voices = voice_track(req.lines, paths, total, job_dir / "voices.wav")

        bed_note = "off"
        bed: Optional[Path] = None
        if req.keepBackground:
            if FAL_KEY:
                original = job_dir / "original.wav"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "2",
                                "-ar", str(VOICE_RATE), str(original)], check=True, capture_output=True, timeout=300)
                try:
                    bed, bed_note = background_track(original, job_dir), "kept"
                except Exception as exc:  # fal down or slow: deliver the voices alone rather than fail
                    print(f"[dub] background not separated: {exc}", flush=True)
                    bed_note = "failed"
            else:
                bed_note = "unavailable"

        soundtrack = job_dir / "soundtrack.wav"
        if bed:
            graph = ("[0:a]aformat=channel_layouts=stereo,volume=0.9[bed];[1:a]aformat=channel_layouts=stereo[v];"
                     "[bed][v]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[out]")
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(bed), "-i", str(voices),
                   "-filter_complex", graph, "-map", "[out]", "-t", f"{total:.3f}", str(soundtrack)]
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(voices), "-ac", "2", "-t", f"{total:.3f}",
                   str(soundtrack)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)

        width, height = probe_size(src)
        top, bottom = req.coverTop, req.coverBottom
        steps = []
        if req.coverSubtitles:
            band = f"crop=iw:trunc(ih*{bottom - top:.4f}/2)*2:0:trunc(ih*{top:.4f}/2)*2"
            # The blur radius may not exceed half the strip's height (chroma planes are half as tall), so a
            # tight band around one subtitle line gets a smaller radius.
            blur = "boxblur=luma_radius='min(24,h/2-1)':luma_power=3:chroma_radius='min(24,ch/2-1)':chroma_power=3"
            steps.append(f"[0:v]split[base][cut];[cut]{band},{blur}[blur];"
                         f"[base][blur]overlay=0:trunc(H*{top:.4f}/2)*2,"
                         f"drawbox=x=0:y=ih*{top:.4f}:w=iw:h=ih*{bottom - top:.4f}:color=black@0.35:t=fill[v1]")
        else:
            steps.append("[0:v]null[v1]")
        if req.subtitles and any((l.text or "").strip() for l in req.lines):
            # Centre the new subtitles in the covered band (or near the bottom when nothing is covered).
            margin = round(height * (1 - bottom) + height * (bottom - top) * 0.25) if req.coverSubtitles \
                else round(height * 0.08)
            subs = subtitle_file(req.lines, width, height, margin, job_dir / "subs.ass")
            steps.append(f"[v1]ass={subs.name}[vout]")
        else:
            steps.append("[v1]null[vout]")
        out = job_dir / "dubbed.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-i", str(soundtrack),
                        "-filter_complex", ";".join(steps), "-map", "[vout]", "-map", "1:a",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
                       check=True, capture_output=True, timeout=900, cwd=job_dir)
        if intro:
            out = prepend_intro(intro, out, width, height, job_dir / "with-intro.mp4")
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Dubbing failed: {(exc.stderr or b'')[-400:]!r}") from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        _render_lock.release()
    return video_response(out, "dubbed.mp4", {"X-Dub-Background": bed_note,
                                              "X-Fal-Failures": "1" if bed_note == "failed" else "0"})
