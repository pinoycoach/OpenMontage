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
from fastapi.responses import FileResponse
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
    """Content for an animated card scene (stat / list / quote), drawn by the composer."""
    kind: Literal["stat", "list", "quote"]
    title: Optional[str] = Field(None, max_length=300)
    label: Optional[str] = Field(None, max_length=300)
    items: list[str] = Field(default_factory=list, max_length=5)
    source: Optional[str] = Field(None, max_length=200)


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
                     if 0 <= i < len(cuts) and not req.scenes[i].card and i not in talking})
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
    wanted = sorted({i for i in req.talkingScenes if 0 <= i < len(cuts)})
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
                authorization: Optional[str] = Header(None)) -> FileResponse:
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
    return FileResponse(out, media_type="video/mp4", filename="reel-hd.mp4", headers=headers or None)


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


def transcribe_segments(path: Path) -> list[dict]:
    """Sentence-ish segments with start/end times for a long recording."""
    from faster_whisper import WhisperModel
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _ = _whisper_model.transcribe(str(path), beam_size=1, vad_filter=True)
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
                 authorization: Optional[str] = Header(None)) -> FileResponse:
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
    return FileResponse(out, media_type="video/mp4", filename="clip.mp4")
