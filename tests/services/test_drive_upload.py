"""Tests for the Drive uploader (no network, no credentials)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "reel_render"))
import drive_upload as du  # noqa: E402


def _batch(tmp_path: Path) -> Path:
    b = tmp_path / "batch9_feelers"
    (b / "inputs" / "arnis").mkdir(parents=True)
    (b / "arnis_feeler_v1.mp4").write_bytes(b"x" * 10)
    (b / "REPORT.md").write_text("r")
    (b / "inputs" / "arnis" / "01.jpg").write_bytes(b"j")
    (b / ".DS_Store").write_bytes(b"")
    return b


def test_lists_every_file_including_inputs_and_skips_junk(tmp_path):
    names = [p.name for p in du.files_to_upload(_batch(tmp_path))]
    assert names == ["REPORT.md", "arnis_feeler_v1.mp4", "01.jpg"]


def test_dry_run_needs_no_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GDRIVE_SA_KEY", raising=False)
    lines = du.upload_batch(_batch(tmp_path), "PARENT", dry_run=True)
    assert lines[1].startswith("batch9_feelers/arnis_feeler_v1.mp4")


def test_a_real_upload_without_a_key_stops_with_a_clear_message(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GDRIVE_SA_KEY", raising=False)
    assert du.main([str(_batch(tmp_path)), "--parent", "PARENT"]) == 1
    assert "GDRIVE_SA_KEY is not set" in capsys.readouterr().err


def test_a_bad_key_is_reported_without_echoing_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GDRIVE_SA_KEY", "not-json-secret-123")
    assert du.main([str(_batch(tmp_path)), "--parent", "PARENT"]) == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "secret-123" not in err
