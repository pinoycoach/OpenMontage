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
