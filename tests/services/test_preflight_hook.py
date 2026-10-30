"""The hook checks in preflight: frame 1 is the picture (no fade-in) and nothing is black."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "reel_render"))
import preflight  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _reel(tmp: Path, fade: bool) -> Path:
    out = tmp / ("fade.mp4" if fade else "cut.mp4")
    vf = "fade=t=in:st=0:d=0.3" if fade else "null"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x4a7fb0:s=540x960:r=30:d=2", "-vf", vf, "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


def _check(tmp: Path, video: Path, name: str) -> str:
    img = tmp / "01.jpg"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x4a7fb0:s=1080x1920:d=1", "-frames:v", "1", str(img)], check=True)
    checks = preflight.check_reel(video, [img], {}, "01.jpg: test", False, False, tmp / "sheet.jpg")
    return next(c.line() for c in checks if name in c.line())


def test_a_fade_in_fails_the_frame_1_check(tmp_path):
    assert _check(tmp_path, _reel(tmp_path, fade=True), "frame 1 is the picture").startswith("[FAIL]")


def test_a_hard_cut_opening_passes_both_frame_1_checks(tmp_path):
    video = _reel(tmp_path, fade=False)
    assert _check(tmp_path, video, "frame 1 is the picture").startswith("[PASS]")
    assert _check(tmp_path, video, "frame 1 included").startswith("[PASS]")
