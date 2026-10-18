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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
MAX_SCENES = 30
MAX_ASSET_BYTES = 25 * 1024 * 1024
FPS = 30

CameraMotion = Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "subtle-pulse"]
CaptionStyle = Literal["yellow-pop", "neon-cyan", "minimal-white"]
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
    motionScenes: list[int] = Field(default_factory=list, max_length=MAX_MOTION_SCENES)
    # Floating particles over the whole video (part of a visual style).
    particles: Optional[Particles] = None
    # Closing card with the creator's handle, shown after the last scene.
    endCard: Optional[EndCard] = None
    # Whoosh on scene changes and a soft pop when a card appears, mixed under the voice.
    sfx: bool = False


app = FastAPI(title="OpenMontage reel render")
# One render per instance: Remotion already uses every core it is given.
_render_lock = threading.Lock()


def _check_token(authorization: Optional[str]) -> None:
    if not RENDER_TOKEN:
        raise HTTPException(503, "RENDER_TOKEN is not configured on the render service.")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), RENDER_TOKEN.encode()):
        raise HTTPException(401, "Bad render token.")


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


def word_captions(text: str, start: float, end: float) -> list[dict]:
    """Spread a line's words across its scene, weighted by word length."""
    words = text.split()
    if not words or end <= start:
        return []
    inset = min(0.15, (end - start) * 0.05)
    t0, t1 = start + inset, end - inset
    weights = [len(w) + 2 for w in words]
    total = sum(weights)
    out, t = [], t0
    for i, (w, wt) in enumerate(zip(words, weights)):
        dt = (t1 - t0) * wt / total
        out.append({
            "word": w,
            "startMs": round(t * 1000),
            "endMs": round((t + dt) * 1000),
            "pageBreakAfter": i == len(words) - 1,
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


def caption_line(scene: Scene, has_voice: bool) -> str:
    # With narration, captions follow what is spoken; otherwise show the on-screen text.
    return (scene.voiceoverLine if has_voice else None) or scene.captionOverlay or ""


def build_props(req: RenderRequest, image_paths: list[Path], voice_path: Optional[Path],
                voice_seconds: Optional[float],
                heard: Optional[list[tuple[str, float, float]]] = None) -> dict:
    spans = scene_timeline([s.durationSeconds for s in req.scenes], voice_seconds)
    vertical = req.aspectRatio == "9:16"

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
        }
        if scene.card:
            cut["type"] = "reel_card"
            cut["card"] = scene.card.model_dump(exclude_none=True)
        cuts.append(cut)
        words = lines[i]
        if req.captions and exact:
            for n, word in enumerate(words):
                t0, t1 = exact[word_index + n]
                captions.append({"word": word, "startMs": round(t0 * 1000), "endMs": round(t1 * 1000),
                                 "pageBreakAfter": n == len(words) - 1})
        elif req.captions:
            captions.extend(word_captions(" ".join(words), start, end))
        word_index += len(words)

    props: dict = {
        "cuts": cuts,
        "overlays": [],
        "captions": captions,
        "audio": {},
        # Every scene is a full-frame image, so skip the animated background (4x faster renders).
        "plainBackground": True,
        # Big, short caption pages that sit above the Reels/TikTok UI on vertical video.
        "captionFontSize": 76 if vertical else 56,
        "captionWordsPerPage": 4 if vertical else 6,
        "captionBottomOffset": 460 if vertical else 90,
        "themeConfig": {
            "backgroundColor": "#000000",
            "surfaceColor": "#000000",
            "textColor": "#FFFFFF",
            "captionHighlightColor": CAPTION_COLORS[req.captionStyle],
            "captionBackgroundColor": "rgba(0, 0, 0, 0.7)",
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


def mix_music(voice_path: Optional[Path], music_path: Path, total: float, job_dir: Path,
              volume: float) -> Path:
    """One soundtrack: the music looped or trimmed to the video, faded in and out, and ducked
    under the voice (a sidechain compressor lowers it whenever someone speaks)."""
    out = job_dir / "soundtrack.wav"
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fade_out = max(total - 2.0, 0.0)
    music_chain = (f"[1:a]{fmt},atrim=0:{total:.2f},volume={volume:.3f},"
                   f"afade=t=in:d=1,afade=t=out:st={fade_out:.2f}:d=2")
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


def _download(url: str, out: Path) -> Path:
    with urllib.request.urlopen(url, timeout=120) as resp:
        out.write_bytes(resp.read())
    return out


def talking_clip(face: Path, voice: Path, start: float, end: float, out: Path, deadline: float) -> Path:
    """Lip-sync the persona photo to voice[start:end] with fal and save the clip as `out`.
    The audio is cut from the narration itself, so the lips match what the viewer hears."""
    segment = out.with_suffix(".mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                    "-i", str(voice), "-ac", "1", "-b:a", "64k", str(segment)],
                   check=True, capture_output=True, timeout=60)
    result = _fal_run(FAL_TALKING_MODEL, {
        "image_url": _fal_file_url(face, "image/jpeg"),
        "audio_url": _fal_file_url(segment, "audio/mpeg"),
    }, deadline, "talking")
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
    result = _fal_run(FAL_MOTION_MODEL, {
        "start_image_url": _fal_file_url(still, "image/jpeg"),
        "prompt": (prompt or "").strip() or MOTION_DEFAULT_PROMPT,
        "duration": str(duration),
        "generate_audio": False,  # the reel has its own narration and music
        "negative_prompt": "blur, distort, low quality, text, captions, watermark, morphing faces",
    }, deadline, "motion")
    raw = _download(result["video"]["url"], out.with_name(out.stem + "-raw.mp4"))
    # Stretch or squeeze to the scene's length so the clip neither freezes nor gets cut short.
    factor = seconds / (probe_duration(raw) or duration)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-an",
                    "-vf", f"setpts={factor:.4f}*PTS", "-t", f"{seconds:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)],
                   check=True, capture_output=True, timeout=180)
    return out


def add_motion_scenes(props: dict, req: RenderRequest, job_dir: Path, image_paths: list[Path]) -> int:
    """Swap the requested scenes for animated clips; a scene whose clip fails keeps its image.
    Card scenes and talking scenes are skipped. Returns how many scenes now move."""
    cuts = props["cuts"]
    talking = set(req.talkingScenes) if req.talkingImage else set()
    wanted = sorted({i for i in req.motionScenes
                     if 0 <= i < len(req.scenes) and not req.scenes[i].card and i not in talking})
    if not (wanted and FAL_KEY):
        return 0
    deadline = time.monotonic() + MOTION_TIMEOUT_SECONDS
    moved = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        futures = {
            pool.submit(motion_clip, image_paths[i], req.scenes[i].motionPrompt,
                        cuts[i]["out_seconds"] - cuts[i]["in_seconds"], job_dir / f"motion-{i + 1}.mp4",
                        deadline): i
            for i in wanted
        }
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            try:
                clip = future.result()
            except Exception as exc:  # fal down, slow or out of credit: keep the image
                print(f"[motion] scene {i + 1} kept its image: {exc}", flush=True)
                continue
            cuts[i]["source"] = clip.name
            moved += 1
    return moved


def add_talking_scenes(props: dict, req: RenderRequest, job_dir: Path, voice_path: Optional[Path]) -> int:
    """Swap the requested scenes for talking clips; a scene whose clip fails keeps its image.
    Returns how many scenes now talk."""
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
                continue
            cuts[i]["source"] = clip.name
            talked += 1
    return talked


def video_response(path: Path, filename: str, headers: Optional[dict] = None) -> StreamingResponse:
    """Send the MP4 in chunks, without Content-Length. Cloud Run refuses a response sent in one
    piece if it's over 32 MB, and reels with motion or talking clips can be larger than that."""
    def chunks():
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', **(headers or {})})


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


@app.get("/health")  # Cloud Run reserves paths ending in "z", so /healthz never reaches the app there
@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "composer": (COMPOSER_DIR / "node_modules").exists(), "talking": bool(FAL_KEY),
            "motion": bool(FAL_KEY)}


@app.post("/render")
def render_reel(req: RenderRequest, background: BackgroundTasks,
                authorization: Optional[str] = Header(None)) -> StreamingResponse:
    _check_token(authorization)
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
        # Talking and motion clips come from fal at the same time.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            talking_job = pool.submit(add_talking_scenes, props, req, job_dir, voice_path)
            motion_job = pool.submit(add_motion_scenes, props, req, job_dir, image_paths)
            talked, moved = talking_job.result(), motion_job.result()
        if music_path:
            # Explainer adds a one-second fade after the last scene.
            total = max(c["out_seconds"] for c in props["cuts"]) + 1.0
            soundtrack = mix_music(voice_path, music_path, total, job_dir, req.musicVolume)
            props["audio"]["narration"] = {"src": soundtrack.name, "volume": 1}
        if req.sfx:
            narration = props["audio"].get("narration")
            base = job_dir / narration["src"] if narration else None
            total = max(c["out_seconds"] for c in props["cuts"]) + 1.0
            try:
                mixed = mix_sfx(base, sfx_events(props), total, job_dir)
                props["audio"]["narration"] = {"src": mixed.name, "volume": 1}
            except Exception as exc:  # effects are a nice-to-have; render without them
                print(f"[sfx] skipped: {exc}", flush=True)
        if not _render_lock.acquire(timeout=5):
            raise HTTPException(429, "Another reel is rendering. Try again in a minute.")
        try:
            out = render(props, job_dir, req.aspectRatio)
        finally:
            _render_lock.release()
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    headers = {}
    if req.talkingScenes:
        headers["X-Talking-Scenes"] = f"{talked}/{len(set(req.talkingScenes))}"
    if req.motionScenes:
        headers["X-Motion-Scenes"] = f"{moved}/{len(set(req.motionScenes))}"
    return video_response(out, "reel-hd.mp4", headers)


# ---------------------------------------------------------------------------
# Clips: long video (Google Drive link) -> transcript -> vertical, captioned short clips.
# The app picks the moments from the transcript; this service downloads, transcribes and cuts.
# ---------------------------------------------------------------------------

CLIP_CACHE = Path(os.environ.get("CLIP_CACHE", "/tmp/clip-sources"))
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_MB", "3000")) * 1024 * 1024
MAX_SOURCE_MINUTES = float(os.environ.get("MAX_SOURCE_MINUTES", "90"))
MAX_CLIP_SECONDS = 90
DRIVE_ID_RE = re.compile(r"(?:/d/|[?&]id=)([A-Za-z0-9_-]{20,})")
_source_locks: dict[str, threading.Lock] = {}
_source_locks_guard = threading.Lock()


def drive_file_id(url: str) -> str:
    """The file id in a Google Drive share link (…/file/d/<id>/view or …?id=<id>)."""
    match = DRIVE_ID_RE.search(url or "")
    if not match:
        raise HTTPException(400, "That doesn't look like a Google Drive file link.")
    return match.group(1)


def _download_drive(file_id: str, dest: Path) -> None:
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            if "text/html" in (resp.headers.get("Content-Type") or ""):
                raise HTTPException(400, "Google Drive didn't send the video. Share it as "
                                         "\"Anyone with the link\" and try again.")
            size = 0
            with tmp.open("wb") as out:
                while chunk := resp.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise HTTPException(400, f"That video is over {MAX_SOURCE_BYTES // (1024 * 1024)} MB.")
                    out.write(chunk)
        tmp.rename(dest)
    except urllib.error.HTTPError as exc:
        raise HTTPException(400, f"Google Drive refused the download ({exc.code}). "
                                 "Check the link is shared as \"Anyone with the link\".") from exc
    finally:
        tmp.unlink(missing_ok=True)


def source_video(drive_url: str) -> Path:
    """The Drive video on local disk, downloaded once per instance and reused for every clip."""
    file_id = drive_file_id(drive_url)
    with _source_locks_guard:
        lock = _source_locks.setdefault(file_id, threading.Lock())
    with lock:
        CLIP_CACHE.mkdir(parents=True, exist_ok=True)
        path = CLIP_CACHE / f"{file_id}.video"
        if not path.exists():
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
def clips_transcribe(req: ClipSourceRequest, authorization: Optional[str] = Header(None)) -> dict:
    _check_token(authorization)
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
def clips_render(req: ClipRenderRequest, background: BackgroundTasks,
                 authorization: Optional[str] = Header(None)) -> StreamingResponse:
    _check_token(authorization)
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
        if not _render_lock.acquire(timeout=5):
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


@app.post("/dub/transcribe")
def dub_transcribe(req: DubTranscribeRequest, authorization: Optional[str] = Header(None)) -> dict:
    _check_token(authorization)
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
    return {"duration": round(duration, 2), "width": width, "height": height, "segments": segments,
            "frames": segment_frames(src, segments), "subtitleBand": band}


def background_track(audio: Path, job_dir: Path) -> Path:
    """Music and effects without the voices: Demucs on fal splits the soundtrack into stems, and
    everything except the vocals is mixed back together."""
    small = job_dir / "for-stems.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio), "-b:a", "192k", str(small)],
                   check=True, capture_output=True, timeout=180)
    result = _fal_run(FAL_STEMS_MODEL, {
        "audio_url": _fal_file_url(small, "audio/mpeg"),
        "model": "htdemucs",
        "stems": ["drums", "bass", "other"],
        "output_format": "mp3",
    }, time.monotonic() + STEMS_TIMEOUT_SECONDS, "stems")
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
def dub_render(req: DubRenderRequest, background: BackgroundTasks,
               authorization: Optional[str] = Header(None)) -> StreamingResponse:
    _check_token(authorization)
    src = source_video(req.driveUrl)
    total = probe_duration(src)
    if not total:
        raise HTTPException(400, "That file isn't a video we can read.")
    if req.coverBottom <= req.coverTop:
        raise HTTPException(400, "The subtitle cover's bottom edge must be below its top edge.")
    job_dir = Path(tempfile.mkdtemp(prefix=f"dub-{uuid.uuid4().hex[:8]}-"))
    background.add_task(shutil.rmtree, job_dir, ignore_errors=True)
    if not _render_lock.acquire(timeout=5):
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
            steps.append(f"[0:v]split[base][cut];[cut]{band},boxblur=24:3[blur];"
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
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, f"Dubbing failed: {(exc.stderr or b'')[-400:]!r}") from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        _render_lock.release()
    return video_response(out, "dubbed.mp4", {"X-Dub-Background": bed_note})
