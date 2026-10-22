"""Tests for services/reel_render/finishing.py: the AI tag and the loudness pass shared by /render and
reels_from_scripts.py. The ffmpeg ones skip where ffmpeg isn't installed (Cloud Build has it)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "reel_render"))
import finishing  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


class _Proc:
    def __init__(self, stderr: str = "", stdout: str = ""):
        self.stderr, self.stdout, self.returncode = stderr, stdout, 0


def test_measure_reads_loudness_and_true_peak_from_one_ffmpeg_pass(monkeypatch):
    stderr = ("[Parsed_ebur128_0 @ 0x1] Summary:\n\n  Integrated loudness:\n    I:         -16.3 LUFS\n"
              "    Threshold: -26.5 LUFS\n\n  True peak:\n    Peak:        0.7 dBFS\n")
    seen = []
    monkeypatch.setattr(finishing.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc(stderr))
    assert finishing.measure(Path("x.mp4")) == (-16.3, 0.7)
    assert finishing.loudness(Path("x.mp4")) == -16.3 and finishing.true_peak(Path("x.mp4")) == 0.7
    assert all("ebur128=peak=true" in c for c in seen) and len(seen) == 3


def test_measure_gives_none_for_silence_or_no_audio(monkeypatch):
    monkeypatch.setattr(finishing.subprocess, "run", lambda cmd, **kw: _Proc("    I:         -70.0 LUFS\n    Peak:       -inf dBFS\n"))
    assert finishing.measure(Path("x.mp4")) == (-70.0, None)
    monkeypatch.setattr(finishing.subprocess, "run", lambda cmd, **kw: _Proc(""))
    assert finishing.measure(Path("x.mp4")) == (None, None)


def test_find_font_never_raises_without_fontconfig(monkeypatch):
    monkeypatch.setattr(finishing, "FONT_CANDIDATES", ["/nowhere/font.ttf"])

    def no_fc_match(cmd, **kw):
        raise FileNotFoundError("fc-match")

    monkeypatch.setattr(finishing.subprocess, "run", no_fc_match)
    assert finishing.find_font() is None


def test_find_font_takes_the_first_candidate_that_exists(tmp_path: Path, monkeypatch):
    font = tmp_path / "Bold.ttf"
    font.write_bytes(b"ttf")
    monkeypatch.setattr(finishing, "FONT_CANDIDATES", ["/nowhere/a.ttf", str(font)])
    assert finishing.find_font() == str(font)


def test_label_filter_puts_the_tag_top_left_on_a_dark_box():
    f = finishing.label_filter("/f/Bold.ttf")
    assert f.startswith("drawtext=fontfile=/f/Bold.ttf:text='AI-generated':")
    assert "box=1" in f and "boxcolor=black@0.6" in f and "x=44:y=282" in f
    assert "fontfile" not in finishing.label_filter(None)


def _clip(path: Path, seconds: float = 3, audio: str | None = "sine=f=440:d=3") -> Path:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=gray:s=108x192:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", audio, "-shortest", "-c:a", "aac"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _frame(path: Path, at: float = 1.0):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{at}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(192, 108)


@needs_ffmpeg
def test_finish_burns_the_tag_and_sets_the_loudness(tmp_path: Path, monkeypatch):
    # The tag is drawn at a fixed pixel position for 1080x1920; scale it for the small test frame.
    monkeypatch.setattr(finishing, "LABEL_FONT_SIZE", 12)
    monkeypatch.setattr(finishing, "LABEL_X", 4)
    monkeypatch.setattr(finishing, "LABEL_Y", 28)
    raw = _clip(tmp_path / "raw.mp4")
    out = tmp_path / "out.mp4"
    info = finishing.finish(raw, out, ai_label=True)
    assert info["aiLabel"] is True
    assert info["lufs"] is not None and abs(info["lufs"] - finishing.TARGET_LUFS) <= 0.5
    assert info["truePeak"] is not None and info["truePeak"] <= finishing.TRUE_PEAK_MAX
    frame = _frame(out)
    band = frame[24:48, 0:100]  # the rows the tag sits in
    assert band.max() > 200 and band.min() < 80, "white text on a dark box where the tag should be"
    assert abs(int(frame[150:190, :].mean()) - 128) < 8, "the rest of the frame is the untouched gray"


@needs_ffmpeg
def test_finish_without_the_tag_or_normalising_leaves_the_picture_and_the_level(tmp_path: Path):
    raw = _clip(tmp_path / "raw.mp4")
    before = finishing.loudness(raw)
    out = tmp_path / "out.mp4"
    info = finishing.finish(raw, out, ai_label=False, normalize=False)
    assert info == {"aiLabel": False, "lufs": None, "truePeak": None}
    frame = _frame(out)
    assert abs(int(frame.mean()) - 128) < 8
    after = finishing.loudness(out)
    assert before is not None and after is not None and abs(after - before) < 1.0


@needs_ffmpeg
def test_finish_never_boosts_a_silent_track(tmp_path: Path):
    raw = _clip(tmp_path / "raw.mp4", audio="anullsrc=r=48000:cl=stereo:d=3")
    out = tmp_path / "out.mp4"
    info = finishing.finish(raw, out, ai_label=False)
    assert info["lufs"] is None and out.exists()
    assert finishing.loudness(out) in (None,) or finishing.loudness(out) < finishing.SILENT_LUFS
