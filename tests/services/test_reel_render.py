"""Reel render service: request validation, timing, captions and props.

The actual Remotion render is stubbed out; these tests never launch Chrome.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.reel_render import app as rr

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
WAV_URL = "data:audio/wav;base64," + base64.b64encode(b"RIFF....WAVEfmt ").decode()


def _request(**overrides) -> rr.RenderRequest:
    body = {
        "scenes": [
            {"image": PNG_URL, "durationSeconds": 2, "cameraMotion": "zoom-in",
             "captionOverlay": "ON SCREEN", "voiceoverLine": "Stop scrolling now"},
            {"image": PNG_URL, "durationSeconds": 3, "cameraMotion": "subtle-pulse",
             "captionOverlay": "SECOND", "voiceoverLine": "Here is why"},
        ],
    }
    body.update(overrides)
    return rr.RenderRequest(**body)


class TestDecodeDataUrl:
    def test_png(self):
        raw, ext = rr.decode_data_url(PNG_URL, "image/")
        assert raw == PNG_1PX and ext == ".png"

    def test_rejects_remote_urls(self):
        with pytest.raises(HTTPException) as exc:
            rr.decode_data_url("https://example.com/a.png", "image/")
        assert exc.value.status_code == 400

    def test_rejects_wrong_kind(self):
        with pytest.raises(HTTPException):
            rr.decode_data_url(WAV_URL, "image/")

    def test_rejects_unknown_mime(self):
        with pytest.raises(HTTPException):
            rr.decode_data_url("data:image/svg+xml;base64,PHN2Zy8+", "image/")


class TestTimeline:
    def test_keeps_plan_when_voice_fits(self):
        assert rr.scene_timeline([2, 3], 4.0) == [(0.0, 2.0), (2.0, 5.0)]

    def test_stretches_when_voice_is_longer(self):
        spans = rr.scene_timeline([2, 2], 7.5)
        assert spans[-1][1] == pytest.approx(8.0)
        assert spans[0][1] == pytest.approx(4.0)

    def test_no_voice(self):
        assert rr.scene_timeline([1, 1], None) == [(0.0, 1.0), (1.0, 2.0)]


class TestWordCaptions:
    def test_words_stay_inside_scene_in_order(self):
        words = rr.word_captions("one two three", 2.0, 4.0)
        assert [w["word"] for w in words] == ["one", "two", "three"]
        assert words[0]["startMs"] >= 2000 and words[-1]["endMs"] <= 4000
        assert all(a["endMs"] <= b["startMs"] + 1 for a, b in zip(words, words[1:]))
        assert words[-1]["pageBreakAfter"] and not words[0]["pageBreakAfter"]

    def test_empty(self):
        assert rr.word_captions("   ", 0, 2) == []


class TestBuildProps:
    def test_vertical_reel(self, tmp_path: Path):
        req = _request()
        imgs = [tmp_path / "scene-1.png", tmp_path / "scene-2.png"]
        props = rr.build_props(req, imgs, tmp_path / "voiceover.wav", 4.0)
        assert [c["source"] for c in props["cuts"]] == ["scene-1.png", "scene-2.png"]
        assert [c["animation"] for c in props["cuts"]] == ["zoom-in", "ken-burns"]
        assert props["audio"]["narration"]["src"] == "voiceover.wav"
        # With narration, captions follow the spoken line.
        assert props["captions"][0]["word"] == "Stop"
        assert props["captionFontSize"] == 76 and props["plainBackground"] is True
        assert props["themeConfig"]["captionHighlightColor"] == "#FDE047"

    def test_without_voice_uses_on_screen_text(self, tmp_path: Path):
        props = rr.build_props(_request(captionStyle="neon-cyan"), [tmp_path / "a.png"] * 2, None, None)
        assert props["captions"][0]["word"] == "ON"
        assert props["audio"] == {}
        assert props["themeConfig"]["captionHighlightColor"] == "#38BDF8"

    def test_captions_off(self, tmp_path: Path):
        props = rr.build_props(_request(captions=False), [tmp_path / "a.png"] * 2, None, None)
        assert props["captions"] == []


class TestEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"fake-mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
        return TestClient(rr.app)

    def _body(self):
        return {"scenes": [{"image": PNG_URL, "durationSeconds": 2}]}

    def test_requires_token(self, client):
        assert client.post("/render", json=self._body()).status_code == 401
        bad = client.post("/render", json=self._body(), headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401

    def test_refuses_when_token_unset(self, client, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "")
        res = client.post("/render", json=self._body(), headers={"Authorization": "Bearer "})
        assert res.status_code == 503

    def test_renders_mp4(self, client):
        res = client.post("/render", json=self._body(), headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.headers["content-type"] == "video/mp4"
        assert res.content == b"fake-mp4"

    def test_rejects_remote_image(self, client):
        body = {"scenes": [{"image": "https://example.com/x.png", "durationSeconds": 2}]}
        res = client.post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 400

    def test_healthz(self, client):
        assert client.get("/healthz").json()["ok"] is True
