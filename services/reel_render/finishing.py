"""The finishing pass every reel gets before it leaves: the "AI-generated" tag burned in top left, and the
audio at -14 LUFS with the true peak at -1 dBTP or below, measured on the encoded AAC track (the encoder
overshoots bright, dense music by a few dB; Ben's QA measured +1.7 dBTP on two batch 9 reels).

Used by the render service (/render, so the ViralReel app's HD reels get it) and by reels_from_scripts.py
(the batch reels), so both come out the same. The renderer's own output never had a label or a loudness
stage before: an app reel came out at about -22 LUFS with no tag."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

TARGET_LUFS = -14.0
TRUE_PEAK_MAX = -1.0  # dBTP, measured on the encoded file
SILENT_LUFS = -50.0   # quieter than this is a captions-only track: never boosted
AI_LABEL = "AI-generated"
# Inter where the batch machine has it, DejaVu on most Linux boxes, Liberation in the renderer's image.
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
# Where the tag sits: below the phone's status bar, above the picture's subject, left aligned.
LABEL_FONT_SIZE = 54
LABEL_X, LABEL_Y = 44, 282


def find_font() -> str | None:
    """A bold font file for the tag, or None (drawtext then uses fontconfig's default, when it has one)."""
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):  # no fontconfig on this machine
        return None
    return out.stdout.strip() or None


def label_filter(font: str | None = None, text: str = AI_LABEL) -> str:
    """The ffmpeg drawtext filter for the tag: white text on a dark box, top left."""
    font_arg = f"fontfile={font}:" if font else ""
    text = text.replace("'", "").replace(":", "")  # drawtext quoting
    return (f"drawtext={font_arg}text='{text}':fontsize={LABEL_FONT_SIZE}:fontcolor=white:box=1:"
            f"boxcolor=black@0.6:boxborderw=10:x={LABEL_X}:y={LABEL_Y}")


def measure(path: Path) -> tuple[float | None, float | None]:
    """(integrated loudness in LUFS, true peak in dBTP) of the file's audio in one pass; None where the
    file has no audio or ffmpeg gave no reading."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
                           "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", proc.stderr)
    peak = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", proc.stderr)
    return (float(lufs[-1]) if lufs else None,
            float(peak[-1]) if peak and peak[-1] != "-inf" else None)


def loudness(path: Path) -> float | None:
    """Integrated loudness (LUFS) of the file's audio."""
    return measure(path)[0]


def true_peak(path: Path) -> float | None:
    """True peak (dBTP) of the file's audio, as encoded."""
    return measure(path)[1]


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60).stdout
    return bool(out.strip())


def shape_audio(raw: Path, gain: float, dest: Path, target_lufs: float = TARGET_LUFS) -> Path:
    """The final AAC track at the target loudness with the true peak at -1 dBTP or below. The AAC encoder
    overshoots bright, dense music by a few dB, so this measures the encoded track and retries with a lower
    limiter ceiling (and the gain to make up the loudness), audio only, so each try takes seconds."""
    limit_db = -1.5
    for _ in range(10):
        af = [f"volume={gain:.2f}dB", "aresample=192000",  # oversampled, so the limiter sees inter-sample peaks
              f"alimiter=limit={10 ** (limit_db / 20):.3f}:attack=1:release=30:level=false", "aresample=48000"]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-vn", "-af", ",".join(af),
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(dest)],
                       check=True, capture_output=True, timeout=300)
        now, tp = measure(dest)
        tp_ok = tp is None or tp <= TRUE_PEAK_MAX
        if tp_ok and (now is None or abs(now - target_lufs) <= 0.5):
            break
        if not tp_ok:
            limit_db -= tp - TRUE_PEAK_MAX + 0.3
        if now is not None:
            gain += target_lufs - now
    return dest


def finish(raw: Path, out: Path, ai_label: bool, normalize: bool = True, target_lufs: float = TARGET_LUFS,
           preset: str = "slow", label_text: str = AI_LABEL) -> dict:
    """The final file: the AI label burned in top left (when asked) and the audio at the target loudness
    (when asked, and the track isn't silent). Returns what was done and the final measurements:
    {"aiLabel": bool, "lufs": float | None, "truePeak": float | None}; the measurements are None when the
    audio was left as it was (so nothing extra is measured) or the file has no audio."""
    vf = [label_filter(find_font(), label_text)] if ai_label else []
    audio = None
    if normalize and has_audio(raw):
        lufs = loudness(raw)
        if lufs is not None and lufs > SILENT_LUFS:  # never boost a silent (captions-only) track
            audio = shape_audio(raw, target_lufs - lufs, out.with_suffix(".audio.m4a"), target_lufs)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw)]
    if audio:
        cmd += ["-i", str(audio), "-map", "0:v", "-map", "1:a"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy"]
    try:
        subprocess.run(cmd + ["-movflags", "+faststart", str(out)], check=True, capture_output=True, timeout=900)
    finally:
        if audio:
            audio.unlink(missing_ok=True)
    final_lufs, final_peak = measure(out) if audio else (None, None)
    return {"aiLabel": ai_label, "lufs": final_lufs, "truePeak": final_peak}
