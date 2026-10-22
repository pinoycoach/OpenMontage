"""Tests for services/reel_render/reels_from_scripts.py: scripts are used word for word, inputs are
checked before anything renders, and the render request follows the voice. No rendering here."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "reel_render"))
import reels_from_scripts as rfs  # noqa: E402

SCRIPT = "The jeepney began as a surplus army jeep. Drivers painted them bright. Now they carry millions every day."


def test_parse_scripts_reads_every_section_in_order():
    text = "intro line is ignored\n## islands\nFirst line.\nSecond line.\n\n## jeepney\n" + SCRIPT + "\n"
    reels = rfs.parse_scripts(text)
    assert reels == [("islands", "First line. Second line."), ("jeepney", SCRIPT)]


def test_parse_scripts_takes_the_first_word_of_the_heading_as_the_folder():
    text = "# Locked scripts\n\n## reel1 — islands\nAkala mo 7,107 islands ang Pilipinas? Mali na 'yan.\n"
    assert rfs.parse_scripts(text) == [("reel1", "Akala mo 7,107 islands ang Pilipinas? Mali na 'yan.")]


@pytest.mark.parametrize("text, problem", [
    ("## a\n" + " ".join(["word"] * 61), "61 words"),
    ("## a\nOne.\n## a\nTwo.", "twice"),
    ("## bad/name\nOne.", "usable reel name"),
    ("## empty\n\n## b\nTwo.", "no script"),
    ("no headings at all", "No reels"),
])
def test_parse_scripts_refuses_bad_files(text, problem):
    with pytest.raises(rfs.ReelError, match=problem):
        rfs.parse_scripts(text)


@pytest.mark.parametrize("parts", [1, 2, 3, 4, 6])
def test_split_keeps_every_word_in_order(parts):
    pieces = rfs.split_script(SCRIPT, parts)
    assert len(pieces) == parts
    assert " ".join(pieces) == SCRIPT
    assert all(len(p.split()) >= rfs.MIN_PIECE_WORDS for p in pieces)


def test_split_prefers_sentence_ends():
    assert rfs.split_script(SCRIPT, 3) == [
        "The jeepney began as a surplus army jeep.", "Drivers painted them bright.", "Now they carry millions every day."]


def test_split_refuses_more_images_than_the_words_can_carry():
    with pytest.raises(rfs.ReelError, match="too many"):
        rfs.split_script("Same price as last year.", 2)


def _reel(root: Path, slug: str, images: int = 2, credits: bool = True) -> Path:
    folder = root / slug
    folder.mkdir(parents=True)
    for i in range(images):
        (folder / f"{i}.jpg").write_bytes(b"jpg")
    if credits:
        (folder / "credits.txt").write_text("0.jpg: Wikimedia Commons, public domain\n")
    return folder


def test_check_inputs_needs_folder_images_and_credits(tmp_path):
    _reel(tmp_path, "ok")
    _reel(tmp_path, "nocredits", credits=False)
    _reel(tmp_path, "noimages", images=0)
    with pytest.raises(rfs.ReelError) as exc:
        rfs.check_inputs([("ok", SCRIPT), ("nocredits", SCRIPT), ("noimages", SCRIPT), ("missing", SCRIPT)], tmp_path)
    msg = str(exc.value)
    assert "nocredits: no credits.txt" in msg and "noimages: no images" in msg and "missing: no folder" in msg
    assert "ok:" not in msg
    rfs.check_inputs([("ok", SCRIPT)], tmp_path)


def test_music_needs_a_licence_file(tmp_path):
    music = tmp_path / "chopin.mp3"
    music.write_bytes(b"mp3")
    with pytest.raises(rfs.ReelError, match="No licence"):
        rfs.music_licence(music)
    (tmp_path / "chopin.txt").write_text("Public domain recording")
    assert rfs.music_licence(music) == tmp_path / "chopin.txt"


def test_request_with_voice_carries_the_script_and_follows_the_voice(tmp_path, monkeypatch):
    folder = _reel(tmp_path, "jeepney", images=3)
    voice = folder / "voice.mp3"
    voice.write_bytes(b"mp3")
    monkeypatch.setattr(rfs, "jpeg_data_url", lambda image, work, i: f"data:image/jpeg;base64,{i}")
    monkeypatch.setattr(rfs, "probe_seconds", lambda path: 9.0)
    body = rfs.build_request(SCRIPT, rfs.reel_images(folder), voice, None, tmp_path, 0.15)
    scenes = body["scenes"]
    assert " ".join(s["voiceoverLine"] for s in scenes) == SCRIPT
    assert all(s["captionOverlay"] is None for s in scenes)
    assert sum(s["durationSeconds"] for s in scenes) == pytest.approx(9.0, abs=0.05)
    assert [s["cameraMotion"] for s in scenes] == ["zoom-in", "pan-left", "zoom-out"]
    assert body["voiceover"].startswith("data:audio/mpeg;base64,")
    assert "music" not in body and body["captions"] is True and body["aspectRatio"] == "9:16"


def test_request_without_voice_shows_the_script_as_captions(tmp_path, monkeypatch):
    folder = _reel(tmp_path, "islands", images=2)
    music = tmp_path / "grieg.mp3"
    music.write_bytes(b"mp3")
    monkeypatch.setattr(rfs, "jpeg_data_url", lambda image, work, i: f"data:image/jpeg;base64,{i}")
    body = rfs.build_request(SCRIPT, rfs.reel_images(folder), None, music, tmp_path, 0.2)
    assert " ".join(s["captionOverlay"] for s in body["scenes"]) == SCRIPT
    assert all(s["voiceoverLine"] is None for s in body["scenes"])
    assert all(s["durationSeconds"] >= 2.5 for s in body["scenes"])
    assert "voiceover" not in body and body["musicVolume"] == 0.2


def test_main_stops_before_rendering_when_inputs_are_missing(tmp_path, capsys, monkeypatch):
    scripts = tmp_path / "scripts.md"
    scripts.write_text("## jeepney\n" + SCRIPT)
    (tmp_path / "reels").mkdir()
    monkeypatch.setattr(rfs, "render_in_process", lambda *a: pytest.fail("rendered despite missing inputs"))
    assert rfs.main([str(scripts), str(tmp_path / "reels"), str(tmp_path / "out")]) == 2
    assert "jeepney: no folder" in capsys.readouterr().err


# --- renderer caption paging and the preflight checks ------------------------------------------------
import os  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

os.environ.setdefault("RENDER_TOKEN", "test")
import app as renderer  # noqa: E402
import preflight  # noqa: E402


@pytest.mark.parametrize("count, sizes", [(1, [1]), (4, [4]), (5, [5]), (6, [6]), (7, [4, 3]), (9, [3, 3, 3]), (13, [4, 3, 3, 3])])
def test_caption_pages_are_balanced_and_never_one_word(count, sizes):
    breaks = sorted(renderer.page_breaks(count, 4))
    got = [b - a for a, b in zip([-1, *breaks], breaks)]
    assert got == sizes
    assert count == 1 or min(got) >= 2


def _frame(v):
    return np.full((480, 270, 3), v, np.uint8)


def test_preflight_finds_cuts_and_dips():
    frames = [_frame(120)] * 20 + [_frame(5)] * 2 + [_frame(200)] * 20
    cuts = preflight.scene_cuts(frames)
    assert cuts == [20]
    lumas = [preflight._luma(f) for f in frames]
    c = cuts[0]
    assert min(lumas[c - 2:c + 3]) < preflight.DIP_RATIO * min(lumas[c - 4], lumas[c + 4])


def test_preflight_straight_cut_is_not_a_dip():
    frames = [_frame(120)] * 20 + [_frame(200)] * 20
    c = preflight.scene_cuts(frames)[0]
    lumas = [preflight._luma(f) for f in frames]
    assert not min(lumas[c - 2:c + 3]) < preflight.DIP_RATIO * min(lumas[c - 4], lumas[c + 4])


def _caption_frame(bg_value: int, box: bool, text: bool):
    import cv2
    f = np.full((480, 270, 3), bg_value, np.uint8)
    if box:
        cv2.rectangle(f, (40, 330), (230, 362), (20, 20, 20), -1)
        if text:
            cv2.putText(f, "Comment below", (52, 352), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
    return f


def test_caption_box_found_with_good_contrast_on_bright_frame():
    b = preflight.caption_box(_caption_frame(235, box=True, text=True))
    assert b and b["contrast"] >= preflight.MIN_CONTRAST and b["bg"] <= 50


def test_no_caption_box_without_box_or_text():
    assert preflight.caption_box(_caption_frame(235, box=False, text=False)) is None
    assert preflight.caption_box(_caption_frame(235, box=True, text=False)) is None


# --- review frames for the by-eye checks --------------------------------------------------------------
import review_frames  # noqa: E402


def test_review_samples_two_frames_per_still_and_every_second_of_a_clip():
    times = review_frames.sample_times([0, 40, 130], 30.0, motion={1})
    assert times[0] == [0.3, 0.97]
    assert times[1] == [1.33, 2.33, 3.33]


def test_review_writes_one_image_per_scene(tmp_path):
    import cv2
    video = tmp_path / "reel.mp4"
    w = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 30, (270, 480))
    for v in [60] * 30 + [200] * 30:
        w.write(np.full((480, 270, 3), v, np.uint8))
    w.release()
    paths = review_frames.write_review(video, tmp_path)
    assert [p.name for p in paths] == ["reel-review-s01.jpg", "reel-review-s02.jpg"]
    assert cv2.imread(str(paths[0])).shape == (960, 1080, 3)


def _textured(h=1440, w=960, seed=3):
    rng = np.random.default_rng(seed)
    img = (rng.random((h // 16, w // 16)) * 255).astype(np.uint8)
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_CUBIC)


def test_stretch_check_finds_a_real_stretch_and_passes_a_uniform_scale(tmp_path):
    src = _textured()
    path = tmp_path / "src.png"
    cv2.imwrite(str(path), src)
    h = 480
    uniform = cv2.resize(src, (int(src.shape[1] * h / src.shape[0]), h))[:, :270]
    stretched = cv2.resize(src, (int(src.shape[1] * h / src.shape[0] * 1.15), h))[:, :270]
    assert preflight.stretch_of(cv2.cvtColor(uniform, cv2.COLOR_GRAY2BGR), path) == 1.0
    assert preflight.stretch_of(cv2.cvtColor(stretched, cv2.COLOR_GRAY2BGR), path) == 1.15


def test_stretch_check_hands_a_plain_background_to_the_eye(tmp_path):
    """A stick on a bare floor once failed as 'x1.08' with no stretch at all (arnis, batch 6)."""
    src = np.full((1920, 1080), 180, np.uint8)
    cv2.line(src, (540, 300), (560, 1600), 60, 18)
    path = tmp_path / "plain.png"
    cv2.imwrite(str(path), src)
    frame = cv2.cvtColor(cv2.resize(src, (270, 480)), cv2.COLOR_GRAY2BGR)
    assert preflight.stretch_of(frame, path) in (None, 1.0)


def test_finish_keeps_the_true_peak_at_or_below_minus_one(tmp_path):
    """A loud, bright track (the kind AAC overshoots) comes out at -1 dBTP or below after finish()."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    raw = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=108x192:d=3",
                    "-f", "lavfi", "-i", "anoisesrc=d=3:c=white:a=0.9", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(raw)], check=True)
    out = tmp_path / "out.mp4"
    rfs.finish(raw, out, ai_label=False)
    tp = rfs.true_peak(out)
    assert tp is not None and tp <= rfs.TRUE_PEAK_MAX
