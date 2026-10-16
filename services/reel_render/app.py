"""Reel render service: turns a ViralReel Studio reel into an HD MP4.

POST /render takes a reel's scenes (images as data URLs), the voiceover and the
caption style, builds props for the Remotion ``Explainer`` composition and
returns the rendered H.264 MP4. Captions are word-by-word, timed across each
scene's window. Only data URLs are accepted, so the service never fetches
arbitrary URLs.

Run locally from the repo root:
    RENDER_TOKEN=dev uvicorn services.reel_render.app:app --port 8090
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_DIR = REPO_ROOT / "remotion-composer"
RENDER_TOKEN = os.environ.get("RENDER_TOKEN", "")
RENDER_TIMEOUT_SECONDS = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "840"))
MAX_SCENES = 30
MAX_ASSET_BYTES = 25 * 1024 * 1024
FPS = 30

CameraMotion = Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "subtle-pulse"]
CaptionStyle = Literal["yellow-pop", "neon-cyan", "minimal-white"]

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


class Scene(BaseModel):
    image: str
    durationSeconds: float = Field(4, gt=0, le=60)
    captionOverlay: Optional[str] = None
    voiceoverLine: Optional[str] = None
    cameraMotion: Optional[CameraMotion] = None


class RenderRequest(BaseModel):
    scenes: list[Scene] = Field(min_length=1, max_length=MAX_SCENES)
    voiceover: Optional[str] = None
    aspectRatio: Literal["9:16", "16:9"] = "9:16"
    captionStyle: CaptionStyle = "yellow-pop"
    captions: bool = True


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


def build_props(req: RenderRequest, image_paths: list[Path], voice_path: Optional[Path],
                voice_seconds: Optional[float]) -> dict:
    spans = scene_timeline([s.durationSeconds for s in req.scenes], voice_seconds)
    vertical = req.aspectRatio == "9:16"
    cuts, captions = [], []
    for i, (scene, path, (start, end)) in enumerate(zip(req.scenes, image_paths, spans)):
        motion = scene.cameraMotion or "subtle-pulse"
        cuts.append({
            "id": f"scene-{i + 1}",
            "source": path.name,  # resolved against --public-dir (the job folder)
            "in_seconds": start,
            "out_seconds": end,
            "animation": MOTION_MAP.get(motion, motion),
        })
        if req.captions:
            # With narration, captions follow what is spoken; otherwise show the on-screen text.
            line = (scene.voiceoverLine if voice_path else None) or scene.captionOverlay or ""
            captions.extend(word_captions(line, start, end))

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
    if voice_path:
        props["audio"]["narration"] = {"src": voice_path.name, "volume": 1}
    return props


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


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "composer": (COMPOSER_DIR / "node_modules").exists()}


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

        props = build_props(req, image_paths, voice_path, voice_seconds)
        if not _render_lock.acquire(timeout=5):
            raise HTTPException(429, "Another reel is rendering. Try again in a minute.")
        try:
            out = render(props, job_dir, req.aspectRatio)
        finally:
            _render_lock.release()
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return FileResponse(out, media_type="video/mp4", filename="reel-hd.mp4")
