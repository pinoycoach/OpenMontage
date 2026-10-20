"""Reels from locked scripts: one scripts file plus an image folder per reel in, one 1080x1920 MP4 per reel
out, using the reel render service in-process (no server, no token, no paid calls).

Every script is used word for word: it is split across the reel's images without changing a word, the
captions show exactly those words (timed to the voice by the renderer), and nothing is generated, so no
face or picture is invented. Run it where the renderer's dependencies are installed (Remotion in
remotion-composer/, ffmpeg, faster-whisper):

    python services/reel_render/reels_from_scripts.py scripts.md reels/ out/ \
        --music music/chopin.mp3 --ai-label

scripts.md holds one "## <slug>" heading per reel (anything after the first word, like "— islands", is
just a label), then that reel's script (60 words or less):

    ## islands
    The Philippines has 7,641 islands. ...

reels/<slug>/ holds that reel's images (shown in file-name order), its credits.txt (source and licence
of every image; required) and, optionally, voice.mp3 (the narration of exactly that script). Without a
voice the reel is captions only. The music file needs a licence file next to it (<name>.txt, LICENSE or
credits.txt). Out: <slug>.mp4 for every reel, credits.txt and report.txt (time and cost per reel).
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_WORDS = 60
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VOICE_NAMES = ("voice.mp3", "voice.m4a", "voice.wav")
# Slow pan and zoom on every still, alternating so cuts don't feel repetitive.
MOTIONS = ["zoom-in", "pan-left", "zoom-out", "pan-right"]
LONG_EDGE = 1920
TARGET_LUFS = -14.0
SILENT_LUFS = -50.0
AI_LABEL = "AI-generated"
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


class ReelError(Exception):
    """A problem with the inputs, reported to the user in plain words."""


def parse_scripts(text: str, max_words: int = MAX_WORDS) -> list[tuple[str, str]]:
    """(slug, script) for every "## slug" section, in file order. Scripts are kept exactly as written
    apart from joining their lines with single spaces."""
    reels: list[tuple[str, str]] = []
    slug, lines = None, []
    for raw in text.splitlines() + ["## "]:
        heading = re.match(r"^##\s+(.*)$", raw)
        if heading:
            if slug is not None:
                script = " ".join(" ".join(lines).split())
                if not script:
                    raise ReelError(f'Reel "{slug}" has no script.')
                words = len(script.split())
                if words > max_words:
                    raise ReelError(f'Reel "{slug}" has {words} words; the limit is {max_words}.')
                reels.append((slug, script))
            # "## reel1 — islands": the first word names the folder, the rest is a label.
            name = heading.group(1).strip()
            slug, lines = (name.split()[0] if name else None), []
            if slug is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
                raise ReelError(f'"{slug}" isn\'t a usable reel name: use letters, numbers, - or _.')
        elif slug is not None:
            lines.append(raw)
    if not reels:
        raise ReelError('No reels found: start each one with a "## <name>" line.')
    seen = set()
    for s, _ in reels:
        if s in seen:
            raise ReelError(f'Reel "{s}" appears twice.')
        seen.add(s)
    return reels


MIN_PIECE_WORDS = 3  # a picture needs about a second of speech; the renderer won't cut faster than 0.8 s


def split_script(script: str, parts: int) -> list[str]:
    """The script cut into `parts` consecutive pieces (at least 3 words each). The cuts are chosen
    together: after a sentence end if at all possible, else after a comma, and pieces of roughly equal
    length. Joining the pieces with spaces gives back the script exactly."""
    words = script.split()
    if parts < 1:
        raise ReelError("A reel needs at least one image.")
    n = len(words)
    if parts * MIN_PIECE_WORDS > n:
        most = max(1, n // MIN_PIECE_WORDS)
        raise ReelError(f"{parts} images is too many for {n} words (each image needs "
                        f"{MIN_PIECE_WORDS}+ words); use at most {most}.")
    target = n / parts

    def cut_cost(i: int) -> float:  # cutting after word i (1-based count of words before the cut)
        end = words[i - 1]
        return 0.0 if end[-1] in ".!?" or end.endswith(('."', ".'", '?"', "?'")) else 3.0 if end[-1] in ",;:" else 9.0

    def piece_cost(length: int) -> float:
        return 4.0 * ((length - target) / target) ** 2

    # best[k][i]: least cost to cut the first i words into k pieces.
    INF = float("inf")
    best = [[INF] * (n + 1) for _ in range(parts + 1)]
    back = [[0] * (n + 1) for _ in range(parts + 1)]
    best[0][0] = 0.0
    for k in range(1, parts + 1):
        for i in range(k * MIN_PIECE_WORDS, n - (parts - k) * MIN_PIECE_WORDS + 1):
            for j in range((k - 1) * MIN_PIECE_WORDS, i - MIN_PIECE_WORDS + 1):
                if best[k - 1][j] == INF:
                    continue
                c = best[k - 1][j] + piece_cost(i - j) + (cut_cost(i) if i < n else 0.0)
                if c < best[k][i]:
                    best[k][i], back[k][i] = c, j
    bounds, i = [n], n
    for k in range(parts, 0, -1):
        i = back[k][i]
        bounds.append(i)
    bounds.reverse()
    pieces = [" ".join(words[x:y]) for x, y in zip(bounds, bounds[1:])]
    assert " ".join(pieces) == " ".join(words) and len(pieces) == parts
    return pieces


def reel_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def reel_voice(folder: Path) -> Path | None:
    for name in VOICE_NAMES:
        if (folder / name).is_file():
            return folder / name
    return None


def music_licence(music: Path) -> Path:
    for candidate in (music.with_suffix(".txt"), music.parent / "LICENSE", music.parent / "LICENSE.txt",
                      music.parent / "credits.txt"):
        if candidate.is_file():
            return candidate
    raise ReelError(f"No licence file for {music.name}: put {music.stem}.txt, LICENSE or credits.txt next to it.")


def check_inputs(reels: list[tuple[str, str]], root: Path) -> None:
    """Every reel has a folder, at least one image and a credits.txt, before anything is rendered."""
    problems = []
    for slug, _ in reels:
        folder = root / slug
        if not folder.is_dir():
            problems.append(f"{slug}: no folder {folder}")
            continue
        if not reel_images(folder):
            problems.append(f"{slug}: no images (jpg, png or webp) in {folder}")
        if not (folder / "credits.txt").is_file():
            problems.append(f"{slug}: no credits.txt (source and licence of every image) in {folder}")
    if problems:
        raise ReelError("Fix these first:\n  " + "\n  ".join(problems))


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def jpeg_data_url(image: Path, work: Path, index: int) -> str:
    """The image as a JPEG no longer than the video's long edge (keeps the request small)."""
    out = work / f"scene-{index + 1}.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(image), "-vf",
                    f"scale='min({LONG_EDGE},iw)':'min({LONG_EDGE},ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "3", str(out)], check=True, capture_output=True, timeout=120)
    return data_url(out)


def build_request(script: str, images: list[Path], voice: Path | None, music: Path | None, work: Path,
                  music_volume: float) -> dict:
    """The renderer's /render body: one scene per image, each carrying its piece of the script."""
    pieces = split_script(script, len(images))
    total_words = len(script.split())
    voice_seconds = probe_seconds(voice) if voice else None
    scenes = []
    for i, (image, piece) in enumerate(zip(images, pieces)):
        n = len(piece.split())
        # The renderer cuts on the spoken words when it can hear them; this length is its fallback. With a
        # voice it's the piece's share of the voice; without, about 2.6 words a second (at least 2.5 s).
        seconds = voice_seconds * n / total_words if voice_seconds else max(2.5, n / 2.6)
        scenes.append({
            "image": jpeg_data_url(image, work, i),
            "durationSeconds": round(max(0.5, seconds), 2),
            "voiceoverLine": piece if voice else None,
            "captionOverlay": None if voice else piece,
            "cameraMotion": MOTIONS[i % len(MOTIONS)],
        })
    body = {"scenes": scenes, "aspectRatio": "9:16", "captions": True, "captionStyle": "yellow-pop",
            "sfx": False}
    if voice:
        body["voiceover"] = data_url(voice)
    if music:
        body["music"] = data_url(music)
        body["musicVolume"] = music_volume
    return body


def probe_seconds(path: Path) -> float | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def find_font() -> str | None:
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True)
    return out.stdout.strip() or None


def loudness(path: Path) -> float | None:
    """Integrated loudness (LUFS) of the file's audio."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null",
                           "-"], capture_output=True, text=True, timeout=300)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", proc.stderr)
    return float(found[-1]) if found else None


def finish(raw: Path, out: Path, ai_label: bool) -> None:
    """The final file: the AI label burned in top left (when asked) and the audio at -14 LUFS."""
    vf = []
    if ai_label:
        font = find_font()
        font_arg = f"fontfile={font}:" if font else ""
        vf.append(f"drawtext={font_arg}text='{AI_LABEL}':fontsize=54:fontcolor=white:box=1:"
                  "boxcolor=black@0.6:boxborderw=10:x=44:y=282")
    lufs = loudness(raw)
    af = []
    if lufs is not None and lufs > SILENT_LUFS:  # never boost a silent (captions-only) track
        af = [f"volume={TARGET_LUFS - lufs:.2f}dB", "alimiter=limit=0.84:level=false"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw)]
    if vf:
        cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    if af:
        cmd += ["-af", ",".join(af), "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-c:a", "copy"]
    subprocess.run(cmd + ["-movflags", "+faststart", str(out)], check=True, capture_output=True, timeout=900)


def render_in_process(body: dict, out: Path) -> dict:
    """POST the body to the renderer app in this process and save the streamed video; returns headers."""
    os.environ.setdefault("RENDER_TOKEN", "local-" + base64.b16encode(os.urandom(8)).decode())
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fastapi.testclient import TestClient  # noqa: E402 - imported where the renderer runs
    import app as renderer  # noqa: E402

    client = TestClient(renderer.app)
    with client.stream("POST", "/render", json=body,
                       headers={"Authorization": f"Bearer {os.environ['RENDER_TOKEN']}"}) as resp:
        if resp.status_code != 200:
            raise ReelError(f"The renderer refused the reel ({resp.status_code}): {resp.read().decode()[:800]}")
        with out.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
        headers = dict(resp.headers)
    expected = headers.get("x-expected-bytes")
    if expected and int(expected) != out.stat().st_size:
        raise ReelError(f"The video came back cut off ({out.stat().st_size} of {expected} bytes).")
    return headers


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(out or "{}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scripts", type=Path, help="file with one '## <slug>' section per reel")
    ap.add_argument("reels", type=Path, help="folder with one sub-folder of images per reel")
    ap.add_argument("out", type=Path, help="where the MP4s, credits.txt and report.txt go")
    ap.add_argument("--music", type=Path, help="music bed (needs a licence file next to it)")
    ap.add_argument("--music-volume", type=float, default=0.15, help="music level under the voice [0.15]")
    ap.add_argument("--ai-label", action="store_true", help='burn in the "AI-generated" tag (AI voice or visuals)')
    ap.add_argument("--max-words", type=int, default=MAX_WORDS, help=f"word limit per script [{MAX_WORDS}]")
    ap.add_argument("--only", nargs="*", help="render only these reels")
    args = ap.parse_args(argv)

    try:
        reels = parse_scripts(args.scripts.read_text(encoding="utf-8"), args.max_words)
        if args.only:
            reels = [r for r in reels if r[0] in set(args.only)]
            if not reels:
                raise ReelError("None of the --only reels are in the scripts file.")
        check_inputs(reels, args.reels)
        if args.music and not args.music.is_file():
            raise ReelError(f"No music file at {args.music}.")
        licence = music_licence(args.music) if args.music else None
    except ReelError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    report, credits = [], []
    t_all = time.monotonic()
    for slug, script in reels:
        folder = args.reels / slug
        images, voice = reel_images(folder), reel_voice(folder)
        t0 = time.monotonic()
        print(f"[{slug}] {len(images)} images, {len(script.split())} words, "
              f"{'voice ' + voice.name if voice else 'no voice (captions only)'}")
        with tempfile.TemporaryDirectory(prefix=f"reel-{slug}-") as tmp:
            work = Path(tmp)
            try:
                body = build_request(script, images, voice, args.music, work, args.music_volume)
                raw = work / "raw.mp4"
                render_in_process(body, raw)
                final = args.out / f"{slug}.mp4"
                finish(raw, final, args.ai_label)
            except (ReelError, subprocess.CalledProcessError) as exc:
                print(f"[{slug}] failed: {exc}", file=sys.stderr)
                report.append(f"{slug}: FAILED after {time.monotonic() - t0:.0f} s: {exc}")
                continue
        info = probe(final)
        video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        seconds = time.monotonic() - t0
        report.append(f"{slug}: {final.name}, {video.get('width')}x{video.get('height')} "
                      f"{video.get('r_frame_rate')}, {float(info.get('format', {}).get('duration', 0)):.1f} s video, "
                      f"made in {seconds:.0f} s, paid calls $0.00, loudness {loudness(final)} LUFS")
        credits += [f"== {slug} ==", (folder / "credits.txt").read_text(encoding="utf-8").strip(), ""]
        print(f"[{slug}] done in {seconds:.0f} s -> {final}")
    if licence:
        credits += ["== music ==", f"{args.music.name}", licence.read_text(encoding="utf-8").strip(), ""]
    if args.ai_label:
        credits += ["== label ==", f'"{AI_LABEL}" is shown on screen (AI voice or AI visuals).', ""]
    (args.out / "credits.txt").write_text("\n".join(credits), encoding="utf-8")
    report.append(f"total: {time.monotonic() - t_all:.0f} s for {len(reels)} reels")
    (args.out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0 if not any("FAILED" in r for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
