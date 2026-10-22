"""Frames for the by-eye part of the reel checklist (plates, faces, invented text). The 2 fps contact sheet
is too small to read a plate, so this writes one sharp image per scene: two frames side by side at half
size (540x960), labelled with the scene and time. Motion-clip scenes get a frame every second instead.
Claude (or a person) reads these and writes <reel>-visual.txt; see .claude/skills/reel-visual-check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight  # noqa: E402

TILE = (540, 960)
VISUAL_ITEMS = (
    "no readable plates, phone or hotline numbers",
    "no clear face of a real person (drivers through windshields and faces in mirrors too)",
    "no AI-invented text on signs, gauges or paint",
    "no brand logos or shop names readable (they need Nap's OK)",
)


def sample_times(bounds: list[int], fps: float, motion: set[int]) -> list[list[float]]:
    """Per scene, the times (s) to look at: 25% and 75% into a still scene, every second in a motion scene."""
    out = []
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1] - 1
        if k in motion:
            picks = list(range(a, b + 1, max(1, int(round(fps))))) or [a]
        else:
            picks = [a + (b - a) // 4, a + 3 * (b - a) // 4]
        out.append([round(i / fps, 2) for i in picks])
    return out


def write_review(video: Path, out_dir: Path, motion: set[int] = frozenset()) -> list[Path]:
    small, fps = preflight._frames(video)
    bounds = [0, *preflight.scene_cuts(small), len(small)]
    times = sample_times(bounds, fps, set(motion))
    cap = cv2.VideoCapture(str(video))
    written = []
    for k, ts in enumerate(times):
        tiles = []
        for t in ts:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))
            ok, f = cap.read()
            if not ok:
                continue
            f = cv2.resize(f, TILE, interpolation=cv2.INTER_AREA)
            cv2.putText(f, f"scene {k + 1}  {t:.2f}s", (16, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
            tiles.append(f)
        if tiles:
            path = out_dir / f"{video.stem}-review-s{k + 1:02d}.jpg"
            cv2.imwrite(str(path), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 90])
            written.append(path)
    cap.release()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--motion", default="", help="1-based scene numbers that are motion clips, e.g. 3,4")
    args = ap.parse_args(argv)
    motion = {int(s) - 1 for s in args.motion.split(",") if s.strip()}
    for video in args.videos:
        for path in write_review(video, video.parent, motion):
            print(path)
        template = video.with_name(f"{video.stem}-visual.txt")
        if not template.exists():  # the reviewer replaces each [ ] with PASS or FLAG plus scene and time
            template.write_text(f"{video.stem}: visual check\n" + "".join(f"[ ] {item}\n" for item in VISUAL_ITEMS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
