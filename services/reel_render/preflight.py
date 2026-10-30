"""Reel pre-send checklist, automated where a machine can judge it (see CLAUDE.md, "Reel pre-send
checklist"). Every reel gets PASS / FAIL / MANUAL lines and a 2 fps contact sheet; a reel with any FAIL
is not sent. MANUAL items (plates, faces, invented text) need eyes on the contact sheet; faces the
detector finds are listed to look at first."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

BLACK_LUMA = 8          # a frame darker than this (mean, 0-255) counts as black
FADE_IN_SECONDS = 0.3   # the only place a dark frame is allowed
DIP_RATIO = 0.5         # at a cut, a frame darker than half of both neighbours is a dip
CUT_DIFF = 20           # mean frame difference (0-255, small frames) that marks a scene change
LUFS_TARGET, LUFS_TOL = -14.0, 1.0
HOLD_SECONDS = 0.5
# Caption band of a vertical reel (fractions of the height): the box sits 460 px above the bottom.
CAPTION_BAND = (0.66, 0.765)
MIN_CONTRAST = 4.5      # WCAG AA for text
MUSIC_TAIL_DB = 30      # after the voice, the music bed must be within this many dB of the voice


@dataclass
class Check:
    section: str
    item: str
    status: str  # PASS | FAIL | MANUAL | SKIP
    detail: str = ""

    def line(self) -> str:
        return f"[{self.status}] {self.section}: {self.item}" + (f" ({self.detail})" if self.detail else "")


def _frames(video: Path, size=(270, 480)) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(cv2.resize(f, size, interpolation=cv2.INTER_AREA))
    cap.release()
    return out, fps


def _luma(f: np.ndarray) -> float:
    return float(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean())


def scene_cuts(frames: list[np.ndarray]) -> list[int]:
    """Frame indexes where a new scene starts (big change from the previous frame)."""
    cuts = []
    for i in range(1, len(frames)):
        d = float(cv2.absdiff(frames[i], frames[i - 1]).mean())
        if d > CUT_DIFF and (not cuts or i - cuts[-1] > 10):
            cuts.append(i)
    return cuts


def stretch_of(frame: np.ndarray, source: Path) -> float | None:
    """Horizontal scale (1.00 = uniform, no stretch) that best matches the frame to the source photo."""
    src = cv2.imread(str(source), cv2.IMREAD_GRAYSCALE)
    if src is None:
        return None
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    # Below the AI tag (ends at about 18% of the height), above the captions, inside pans.
    tpl = g[int(h * 0.20):int(h * 0.62), int(w * 0.15):int(w * 0.85)]
    if float(tpl.std()) < 12:  # a plain background (a stick on a bare floor) can't show a stretch: check by eye
        return None
    scores = {}
    for sx in (0.85, 0.92, 1.0, 1.08, 1.15):
        for zoom in np.arange(1.0, 1.31, 0.03):  # fine steps: a coarse grid matched a zoom-in as a stretch
            k = h / src.shape[0] * zoom
            img = cv2.resize(src, (max(1, int(src.shape[1] * k * sx)), max(1, int(src.shape[0] * k))))
            if img.shape[0] < tpl.shape[0] or img.shape[1] < tpl.shape[1]:
                continue
            score = float(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED).max())
            scores[sx] = max(score, scores.get(sx, -1.0))
    if not scores or max(scores.values()) <= 0.45:
        return None
    best = max(scores, key=scores.get)
    # Call a stretch only when it clearly beats the uniform scale; on low-texture frames the scores tie.
    if scores.get(1.0, -1.0) >= scores[best] - 0.02:
        return 1.0
    return best


def _rel_lum(v: float) -> float:
    c = v / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def caption_box(frame: np.ndarray) -> dict | None:
    """The caption box in the caption band, if one is there: its background and text luma and the
    contrast ratio between them. None when there's no box with text (no dark run holding bright text)."""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    band = g[int(h * CAPTION_BAND[0]):int(h * CAPTION_BAND[1]), int(w * 0.08):int(w * 0.92)]
    dark = band < 45
    rows = np.where(dark.mean(axis=1) > 0.30)[0]   # rows that are mostly box
    if len(rows) < 3:
        return None
    box = band[rows.min():rows.max() + 1]
    cols = np.where((box < 45).mean(axis=0) > 0.30)[0]
    if len(cols) < band.shape[1] * 0.15:
        return None
    box = box[:, cols.min():cols.max() + 1]
    text = box[box > 150]
    if text.size < box.size * 0.01:
        return None
    bg = float(np.median(box[box < 60])) if (box < 60).any() else float(box.min())
    fg = float(np.percentile(text, 90))
    contrast = (_rel_lum(fg) + 0.05) / (_rel_lum(bg) + 0.05)
    return {"bg": bg, "fg": fg, "contrast": contrast}


def faces_at(frames: list[np.ndarray], fps: float) -> list[float]:
    """Times (s) of sampled frames where a frontal face is detected: candidates to check by eye."""
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits = []
    step = max(1, int(fps / 2))
    for i in range(0, len(frames), step):
        g = cv2.cvtColor(cv2.resize(frames[i], (540, 960)), cv2.COLOR_BGR2GRAY)
        if len(det.detectMultiScale(g, 1.1, 6, minSize=(40, 40))):
            hits.append(round(i / fps, 2))
    return hits


def audio_levels(video: Path) -> dict:
    """Integrated loudness and the level of the last second, in dB."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-af", "ebur128", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=300)
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", proc.stderr)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, timeout=120).stdout
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    if not len(x):
        return {"lufs": None}

    def db(seg: np.ndarray) -> float:
        return float(20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9)) if len(seg) else -120.0

    sr = 8000
    return {"lufs": float(lufs[-1]) if lufs else None,
            "before_fade_db": db(x[-int(2.5 * sr):-int(1.0 * sr)]),
            "last_quarter_db": db(x[-int(0.25 * sr):]),
            "last_50ms_db": db(x[-int(0.05 * sr):])}


def probe(video: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "default=nw=1", str(video)],
                         capture_output=True, text=True, timeout=60).stdout
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    num, _, den = vals.get("r_frame_rate", "0/1").partition("/")
    return {"width": int(vals.get("width", 0)), "height": int(vals.get("height", 0)),
            "fps": float(num) / float(den or 1), "duration": float(vals.get("duration", 0))}


def contact_sheet(frames: list[np.ndarray], fps: float, out: Path, per_second: int = 2) -> None:
    step = max(1, int(round(fps / per_second)))
    picks = [frames[i] for i in range(0, len(frames), step)]
    cols = 10
    rows = [picks[i:i + cols] for i in range(0, len(picks), cols)]
    blank = np.zeros_like(picks[0])
    sheet = np.vstack([np.hstack(r + [blank] * (cols - len(r))) for r in rows])
    for k in range(len(picks)):
        y, x = (k // cols) * picks[0].shape[0], (k % cols) * picks[0].shape[1]
        cv2.putText(sheet, f"{k / per_second:.1f}s", (x + 6, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    cv2.imwrite(str(out), sheet)


def check_reel(video: Path, images: list[Path], clips: dict[int, Path], credits_text: str,
               has_voice: bool, has_music: bool, sheet: Path, script_split: bool = False) -> list[Check]:
    """Run the checklist on one finished reel. `images` in scene order; `clips` maps scene index to a
    supplied motion clip (those scenes are matched against the clip's first frame instead).
    `script_split`: the reel was cut by reels_from_scripts.split_script, which gives every image 3+ words;
    any other reel (a hand edit, a silent shot on purpose) gets that check by ear."""
    checks: list[Check] = []
    frames, fps = _frames(video)
    info = probe(video)
    contact_sheet(frames, fps, sheet)

    # --- Inputs -------------------------------------------------------------------------------------
    cuts = scene_cuts(frames)
    bounds = [0, *cuts, len(frames)]
    stretched, unmatched = [], []
    if len(bounds) - 1 == len(images):
        for k, image in enumerate(images):
            mid = frames[(bounds[k] + bounds[k + 1]) // 2]
            ref = image
            if k in clips:  # a motion clip: compare with its own first frame
                cap = cv2.VideoCapture(str(clips[k]))
                ok, f0 = cap.read()
                cap.release()
                if ok:
                    ref = sheet.with_name(f"_ref{k}.png")
                    cv2.imwrite(str(ref), f0)
                    mid = frames[bounds[k] + 2]
            sx = stretch_of(mid, ref)
            if sx is None:
                unmatched.append(image.name)
            elif abs(sx - 1.0) > 0.01:
                stretched.append(f"{image.name} x{sx:.2f}")
            if ref != image:
                ref.unlink(missing_ok=True)
        status = "FAIL" if stretched else ("MANUAL" if unmatched else "PASS")
        detail = ("stretched: " + ", ".join(stretched)) if stretched else (
            f"uniform scale on all {len(images)} scenes" + (f"; couldn't match {', '.join(unmatched)}: check by eye"
                                                            if unmatched else ""))
        checks.append(Check("Inputs", "every image cover-cropped, no stretch", status, detail))
    else:
        checks.append(Check("Inputs", "every image cover-cropped, no stretch", "MANUAL",
                            f"found {len(bounds) - 1} scenes for {len(images)} images; check by eye"))
    checks.append(Check("Inputs", "no readable plates, phone or hotline numbers", "MANUAL",
                        "check the contact sheet"))
    faces = faces_at(frames, fps)
    checks.append(Check("Inputs", "no clear face of a real person", "MANUAL",
                        f"face detector hits at {faces} s: look there first" if faces else "detector found none; still look"))
    checks.append(Check("Inputs", "no AI-invented text on signs, gauges or paint", "MANUAL" if clips else "PASS",
                        f"look at the motion scenes {sorted(k + 1 for k in clips)}" if clips else "no AI motion in this reel"))
    checks.append(Check("Inputs", "each image has 3+ script words", "PASS" if script_split else "MANUAL",
                        "enforced when the script is split" if script_split
                        else "not cut by the script split: check by ear (a silent shot on purpose is fine)"))

    # --- Render -------------------------------------------------------------------------------------
    lumas = [_luma(f) for f in frames]
    skip = int(FADE_IN_SECONDS * fps) + 1
    # Every frame after the fade-in, the last one included (0.25 s sampling missed a 1-frame black at a cut).
    black = [round(i / fps, 2) for i in range(skip, len(frames)) if lumas[i] < BLACK_LUMA]
    checks.append(Check("Render", "no black or empty frames after the fade-in", "FAIL" if black else "PASS",
                        f"black at {black[:6]} s" if black else "checked every frame"))
    dips = []
    for c in cuts:
        a, b = lumas[max(0, c - 4)], lumas[min(len(lumas) - 1, c + 4)]
        lo = min(lumas[max(0, c - 2):min(len(lumas), c + 3)])
        if lo < DIP_RATIO * min(a, b):
            dips.append(round(c / fps, 2))
    checks.append(Check("Render", "transitions are straight cuts, no dip to black", "FAIL" if dips else "PASS",
                        f"dips at {dips} s" if dips else f"{len(cuts)} cuts checked"))
    # Every spoken line has a caption on screen: from the first caption to the end, on every frame
    # (sampling every 0.25 s missed a 2-3 frame caption flicker at a cut).
    boxes = {i: caption_box(frames[i]) for i in range(skip, len(frames))}
    first = next((i for i, b in boxes.items() if b), None)
    missing_cap = [round(i / fps, 2) for i, b in boxes.items() if first is not None and i >= first and not b]
    if first is None:
        checks.append(Check("Render", "every spoken line has a visible caption", "FAIL", "no caption found at all"))
    else:
        checks.append(Check("Render", "every spoken line has a visible caption", "FAIL" if missing_cap else "PASS",
                            f"no readable caption at {missing_cap[:8]} s" if missing_cap else
                            f"caption on screen in all {sum(1 for i in boxes if i >= first)} frames from {first / fps:.2f} s"))
    found = [b for b in boxes.values() if b]
    low = [round(i / fps, 2) for i, b in boxes.items() if b and (b["contrast"] < MIN_CONTRAST or b["bg"] > 50)]
    worst = min((b["contrast"] for b in found), default=0)
    checks.append(Check("Render", "captions on a near-solid dark box, readable on bright frames",
                        "FAIL" if low or not found else "PASS",
                        f"low contrast or see-through box at {low[:8]} s (worst {worst:.1f}:1)" if low else
                        f"worst contrast {worst:.1f}:1 (need {MIN_CONTRAST}:1), box background never above 50/255"))
    tail = frames[-int(HOLD_SECONDS * fps):]
    tail_cut = [c for c in cuts if c >= len(frames) - int(HOLD_SECONDS * fps)]
    band = [float(cv2.cvtColor(f[int(f.shape[0] * 0.70):int(f.shape[0] * 0.78)], cv2.COLOR_BGR2GRAY).min()) for f in tail]
    hold_ok = not tail_cut and min(_luma(f) for f in tail) >= BLACK_LUMA and max(band) < 40
    checks.append(Check("Render", "last caption on the last photo, held 0.5 s+", "PASS" if hold_ok else "FAIL",
                        "no cut, photo visible and caption box present in the last 0.5 s" if hold_ok else
                        "a cut, a black frame or no caption box in the last 0.5 s"))
    lv = audio_levels(video)
    if has_music or has_voice:
        lufs_ok = lv.get("lufs") is not None and abs(lv["lufs"] - LUFS_TARGET) <= LUFS_TOL
        fade_ok = lv.get("last_quarter_db", 0) <= lv.get("before_fade_db", 0) - 12 and lv.get("last_50ms_db", 0) < -40
        checks.append(Check("Render", "audio -14 LUFS, 1 s fade-out ending with the video",
                            "PASS" if lufs_ok and fade_ok else "FAIL",
                            f"{lv.get('lufs')} LUFS; last 0.25 s {lv.get('last_quarter_db', 0):.0f} dB vs "
                            f"{lv.get('before_fade_db', 0):.0f} dB before the fade; last 50 ms {lv.get('last_50ms_db', 0):.0f} dB"))
    else:
        checks.append(Check("Render", "audio -14 LUFS, 1 s fade-out", "SKIP", "no audio in this reel"))
    checks.append(Check("Render", "voice word for word, music ducked under it", "MANUAL" if has_voice else "SKIP",
                        "captions are the script; listen for misreads" if has_voice else "no voice yet"))
    fmt_ok = (info["width"], info["height"]) == (1080, 1920) and round(info["fps"]) in (24, 30) and info["duration"] < 60
    checks.append(Check("Render", "1080x1920, 24 or 30 fps, under 60 s", "PASS" if fmt_ok else "FAIL",
                        f"{info['width']}x{info['height']}, {info['fps']:.0f} fps, {info['duration']:.1f} s"))
    missing = [im.name for im in images if im.name not in credits_text]
    checks.append(Check("Render", "every image credited in credits.txt", "FAIL" if missing else "PASS",
                        f"missing {missing}" if missing else f"{len(images)} images credited"))
    return checks
