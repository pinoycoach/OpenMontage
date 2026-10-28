"""Reel render service: request validation, timing, captions and props.

The actual Remotion render is stubbed out; these tests never launch Chrome.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
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


def _no_finish(raw, req, job_dir):
    """Stands in for the finishing pass where the render is stubbed: hands the stub file back untouched."""
    return raw, {}


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

    def test_no_caption_gap_at_a_cut(self):
        first = rr.word_captions("Shaved ice, gatas, saging", 0.0, 3.5)
        second = rr.word_captions("Pero bago kainin, kailangan", 3.5, 7.0)
        assert first[0]["startMs"] == 0 and first[-1]["endMs"] == 3500 == second[0]["startMs"]


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
        monkeypatch.setattr(rr, "finish_reel", _no_finish)  # the stub's bytes aren't a video ffmpeg can finish
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


class TestSpeechTiming:
    HEARD = [("Stop", 0.0, 0.26), ("scrolling,", 0.26, 0.7), ("this", 0.96, 1.08), ("changes", 1.08, 1.44),
             ("everything.", 1.44, 2.04), ("Here", 2.3, 2.5), ("is", 2.5, 2.6), ("why.", 2.6, 3.0)]

    def test_align_uses_heard_times(self):
        times = rr.align_words(["Stop", "scrolling", "this", "changes", "everything"], self.HEARD[:5])
        assert times[0] == (0.0, 0.26) and times[-1] == (1.44, 2.04)

    def test_align_fills_misheard_words(self):
        heard = [("Stop", 0.0, 0.3), ("scroll", 0.3, 0.7), ("this", 1.0, 1.1), ("changes", 1.1, 1.4), ("everything", 1.4, 2.0)]
        times = rr.align_words(["Stop", "scrolling", "this", "changes", "everything"], heard)
        assert len(times) == 5
        assert 0.3 <= times[1][0] <= times[1][1] <= 1.0  # fitted between its neighbours

    def test_align_rejects_unrelated_audio(self):
        assert rr.align_words(["one", "two", "three", "four"], [("hello", 0, 1), ("world", 1, 2)]) is None

    def test_timed_spans_cut_between_lines(self):
        times = [(0.0, 0.5), (0.5, 1.0), (1.4, 1.8), (1.8, 2.2)]
        assert rr.timed_spans([(0, 2), (2, 4)], times) == [(0.0, 1.2), (1.2, 3.0)]
        assert rr.timed_spans([(0, 2), (2, 2)], times) is None  # a scene with no words

    def test_build_props_with_speech(self, tmp_path: Path):
        req = _request()  # scene lines: "Stop scrolling now" / "Here is why"
        heard = [("Stop", 0.0, 0.3), ("scrolling", 0.3, 0.8), ("now.", 0.8, 1.1),
                 ("Here", 1.6, 1.8), ("is", 1.8, 1.9), ("why.", 1.9, 2.3)]
        props = rr.build_props(req, [tmp_path / "a.png"] * 2, tmp_path / "voiceover.wav", 2.4, heard)
        assert props["captionTiming"] == "speech"
        assert [(c["in_seconds"], c["out_seconds"]) for c in props["cuts"]] == [(0.0, 1.35), (1.35, 3.1)]
        assert props["captions"][3] == {"word": "Here", "startMs": 1600, "endMs": 1800, "pageBreakAfter": False}

    def test_build_props_falls_back_to_estimates(self, tmp_path: Path):
        props = rr.build_props(_request(), [tmp_path / "a.png"] * 2, tmp_path / "v.wav", 4.0, None)
        assert props["captionTiming"] == "estimated"


class TestMusic:
    def _tone(self, path: Path, seconds: float, freq: int) -> Path:
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={freq}:duration={seconds}", str(path)], check=True)
        return path

    def test_mix_with_voice_has_full_length(self, tmp_path: Path):
        voice = self._tone(tmp_path / "v.wav", 2, 440)
        music = self._tone(tmp_path / "m.wav", 1, 220)  # shorter than the video: must loop
        out = rr.mix_music(voice, music, 5.0, tmp_path, 0.2)
        assert abs(rr.probe_duration(out) - 5.0) < 0.1

    def test_mix_without_voice(self, tmp_path: Path):
        music = self._tone(tmp_path / "m.wav", 8, 220)
        out = rr.mix_music(None, music, 4.0, tmp_path, 0.5)
        assert abs(rr.probe_duration(out) - 4.0) < 0.1

    def _lufs(self, path: Path) -> float:
        import re
        import subprocess
        proc = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                              capture_output=True, text=True)
        return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", proc.stderr)[-1])

    def test_music_bed_is_levelled_whatever_its_master(self, tmp_path: Path):
        import subprocess
        loud, quiet = tmp_path / "loud.wav", tmp_path / "quiet.wav"
        for path, gain in ((loud, 0.9), (quiet, 0.05)):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anoisesrc=d=8:c=pink:a=1", "-af", f"volume={gain}", str(path)], check=True)
        levels = []
        for music in (loud, quiet):
            job = tmp_path / music.stem
            job.mkdir()
            levels.append(self._lufs(rr.mix_music(None, music, 6.0, job, 1.0)))
        assert abs(levels[0] - levels[1]) < 2.0
        assert abs(levels[0] - rr.MUSIC_BED_LUFS) < 2.5
        # musicVolume 0.5 is about 6 dB under that.
        half = self._lufs(rr.mix_music(None, loud, 6.0, tmp_path, 0.5))
        assert 4.5 < levels[0] - half < 7.5

    def test_endpoint_accepts_music(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        seen = {}

        def fake_render(props, job_dir, aspect):
            seen["audio"] = props["audio"]
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
        monkeypatch.setattr(rr, "finish_reel", _no_finish)  # the stub's bytes aren't a video ffmpeg can finish
        tone = self._tone(tmp_path / "m.wav", 1, 220).read_bytes()
        music_url = "data:audio/wav;base64," + base64.b64encode(tone).decode()
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "music": music_url}
        res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert seen["audio"]["narration"]["src"] == "soundtrack.wav"


class TestTalkingScenes:
    def _tone(self, path: Path, seconds: float) -> Path:
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=300:duration={seconds}", str(path)], check=True)
        return path

    def _props(self, tmp_path: Path) -> dict:
        req = _request()
        return rr.build_props(req, [tmp_path / "scene-1.png", tmp_path / "scene-2.png"],
                              tmp_path / "voiceover.wav", 4.0)

    def test_swaps_scenes_and_keeps_image_on_failure(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        voice = self._tone(tmp_path / "voiceover.wav", 4)
        calls = []

        def fake_clip(face, voice_path, start, end, out, deadline):
            calls.append((out.name, start, end))
            if out.name == "talk-2.mp4":
                raise TimeoutError("slow")
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "talking_clip", fake_clip)
        props = self._props(tmp_path)
        req = _request(talkingImage=PNG_URL, talkingScenes=[0, 1, 7])
        assert rr.add_talking_scenes(props, req, tmp_path, voice) == 1
        assert props["cuts"][0]["source"] == "talk-1.mp4"
        assert props["cuts"][1]["source"] == "scene-2.png"  # failed clip keeps the image
        assert sorted(c[0] for c in calls) == ["talk-1.mp4", "talk-2.mp4"]  # index 7 ignored
        assert ("talk-1.mp4", 0.0, 2.0) in calls

    def test_off_without_key_or_voice(self, tmp_path: Path, monkeypatch):
        props = self._props(tmp_path)
        req = _request(talkingImage=PNG_URL, talkingScenes=[0])
        monkeypatch.setattr(rr, "FAL_KEY", "")
        assert rr.add_talking_scenes(props, req, tmp_path, tmp_path / "v.wav") == 0
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        assert rr.add_talking_scenes(props, req, tmp_path, None) == 0
        assert props["cuts"][0]["source"] == "scene-1.png"

    def test_talking_clip_cuts_audio_and_polls_fal(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        monkeypatch.setattr(rr.time, "sleep", lambda s: None)
        voice = self._tone(tmp_path / "voice.wav", 5)
        face = tmp_path / "face.jpg"
        face.write_bytes(PNG_1PX)
        sent, statuses = {}, iter(["IN_QUEUE", "IN_PROGRESS", "COMPLETED"])

        def fake_json(url, body=None, timeout=60, method=None):
            if body is not None:
                sent.update(body)
                return {"status_url": "https://q/status", "response_url": "https://q/result"}
            if url.endswith("/status"):
                return {"status": next(statuses)}
            return {"video": {"url": "https://cdn/clip.mp4"}}

        monkeypatch.setattr(rr, "_fal_json", fake_json)
        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(b"clip-bytes"))
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: f"https://cdn/{path.name}")
        out = rr.talking_clip(face, voice, 1.0, 3.0, tmp_path / "talk-1.mp4", rr.time.monotonic() + 60)
        assert out.read_bytes() == b"clip-bytes"
        assert sent == {"image_url": "https://cdn/face.jpg", "audio_url": "https://cdn/talk-1.mp3"}
        assert abs(rr.probe_duration(tmp_path / "talk-1.mp3") - 2.0) < 0.1  # just the scene's stretch

    def test_endpoint_reports_talking_header(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "transcribe", lambda p: None)
        monkeypatch.setattr(rr, "add_talking_scenes", lambda props, req, job_dir, voice, failures=None: 1)

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
        monkeypatch.setattr(rr, "finish_reel", _no_finish)  # the stub's bytes aren't a video ffmpeg can finish
        tone = self._tone(tmp_path / "v.wav", 2).read_bytes()
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}],
                "voiceover": "data:audio/wav;base64," + base64.b64encode(tone).decode(),
                "talkingImage": PNG_URL, "talkingScenes": [0]}
        res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.headers["x-talking-scenes"] == "1/1"

    def test_talking_clip_cancels_on_timeout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        voice = self._tone(tmp_path / "voice.wav", 3)
        face = tmp_path / "face.jpg"
        face.write_bytes(PNG_1PX)
        calls = []

        def fake_json(url, body=None, timeout=60, method=None):
            calls.append((method or ("POST" if body else "GET"), url))
            if body is not None:
                return {"status_url": "https://q/status", "response_url": "https://q/r", "cancel_url": "https://q/r/cancel"}
            return {"status": "IN_QUEUE"}

        monkeypatch.setattr(rr, "_fal_json", fake_json)
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: f"https://cdn/{path.name}")
        with pytest.raises(TimeoutError):
            rr.talking_clip(face, voice, 0.0, 2.0, tmp_path / "talk-1.mp4", rr.time.monotonic() - 1)
        assert ("PUT", "https://q/r/cancel") in calls

    def test_fal_file_url_uploads_then_falls_back(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        f = tmp_path / "face.jpg"
        f.write_bytes(b"jpeg-bytes")
        puts = []

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=0):
            puts.append((request.get_method(), request.full_url, request.data))
            return FakeResp()

        monkeypatch.setattr(rr, "_fal_json", lambda url, body=None, timeout=60, method=None:
                            {"upload_url": "https://up/signed", "file_url": "https://cdn/face.jpg"})
        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)
        assert rr._fal_file_url(f, "image/jpeg") == "https://cdn/face.jpg"
        assert puts == [("PUT", "https://up/signed", b"jpeg-bytes")]

        def broken(*a, **k):
            raise RuntimeError("fal HTTP 401: no")
        monkeypatch.setattr(rr, "_fal_json", broken)
        assert rr._fal_file_url(f, "image/jpeg").startswith("data:image/jpeg;base64,")

    def test_fal_json_reports_fal_error_detail(self, monkeypatch):
        import io

        def fake_urlopen(request, timeout=0):
            raise rr.urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {},
                                            io.BytesIO(b'{"detail":[{"loc":["body","image_url"]}]}'))

        monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError, match=r"fal HTTP 422: .*image_url"):
            rr._fal_json("https://queue.fal.run/x", {"a": 1})


class TestCardsAndMotion:
    def _tone(self, path: Path, seconds: float) -> Path:
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=300:duration={seconds}", str(path)], check=True)
        return path

    def test_card_scene_and_particles_in_props(self, tmp_path: Path):
        req = _request(particles="fireflies")
        req.scenes[1].card = rr.Card(kind="stat", title="8.1B", label="people on Earth")
        props = rr.build_props(req, [tmp_path / "scene-1.png", tmp_path / "scene-2.png"], None, None)
        assert "type" not in props["cuts"][0]
        assert props["cuts"][1]["type"] == "reel_card"
        assert props["cuts"][1]["card"] == {"kind": "stat", "title": "8.1B", "label": "people on Earth", "items": []}
        assert props["cuts"][1]["source"] == "scene-2.png"  # still card stays as the fallback
        assert props["particles"] == "fireflies"
        assert "particles" not in rr.build_props(_request(), [tmp_path / "a.png", tmp_path / "b.png"], None, None)

    def test_rejects_unknown_particles(self):
        with pytest.raises(ValueError):
            _request(particles="confetti")

    def test_motion_skips_cards_and_talking_and_keeps_image_on_failure(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        scenes = [{"image": PNG_URL, "durationSeconds": 2, "voiceoverLine": f"line {i}"} for i in range(4)]
        scenes[2]["card"] = {"kind": "quote", "title": "Hi"}
        req = rr.RenderRequest(scenes=scenes, motionScenes=[0, 1], talkingImage=PNG_URL, talkingScenes=[0])
        paths = [tmp_path / f"scene-{i + 1}.png" for i in range(4)]
        props = rr.build_props(req, paths, None, None)
        calls = []

        def fake_clip(image, prompt, seconds, out, deadline):
            calls.append((image.name, seconds))
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "motion_clip", fake_clip)
        assert rr.add_motion_scenes(props, req, tmp_path, paths) == 1
        assert calls == [("scene-2.png", 2.0)]  # scene 1 talks
        assert props["cuts"][1]["source"] == "motion-2.mp4"

        req2 = rr.RenderRequest(scenes=scenes, motionScenes=[2, 3])
        monkeypatch.setattr(rr, "motion_clip", lambda *a: (_ for _ in ()).throw(TimeoutError("slow")))
        props2 = rr.build_props(req2, paths, None, None)
        assert rr.add_motion_scenes(props2, req2, tmp_path, paths) == 0
        assert props2["cuts"][3]["source"] == "scene-4.png"
        monkeypatch.setattr(rr, "FAL_KEY", "")
        assert rr.add_motion_scenes(props2, req2, tmp_path, paths) == 0

    def test_motion_clip_sends_kling_input_and_retimes(self, tmp_path: Path, monkeypatch):
        import subprocess
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        image = tmp_path / "scene-1.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x568",
                        "-frames:v", "1", str(image)], check=True)
        clip = tmp_path / "kling.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=5",
                        "-pix_fmt", "yuv420p", str(clip)], check=True)
        sent = {}

        def fake_run(model, body, deadline, tag):
            sent.update(body, model=model)
            return {"video": {"url": "https://cdn/kling.mp4"}}

        monkeypatch.setattr(rr, "_fal_run", fake_run)
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: f"https://cdn/{path.name}")
        monkeypatch.setattr(rr, "_download", lambda url, out: out.write_bytes(clip.read_bytes()) and out)
        out = rr.motion_clip(image, None, 3.4, tmp_path / "motion-1.mp4", rr.time.monotonic() + 60)
        assert sent["model"] == rr.FAL_MOTION_MODEL
        assert sent["duration"] == "4" and sent["generate_audio"] is False
        assert sent["start_image_url"] == "https://cdn/motion-1-still.jpg"
        assert sent["prompt"] == rr.MOTION_DEFAULT_PROMPT
        assert abs(rr.probe_duration(out) - 3.4) < 0.15

    def test_endpoint_reports_motion_header(self, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "add_motion_scenes", lambda props, req, job_dir, paths, failures=None: 1)

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
        monkeypatch.setattr(rr, "finish_reel", _no_finish)  # the stub's bytes aren't a video ffmpeg can finish
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "motionScenes": [0]}
        res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.headers["x-motion-scenes"] == "1/1"
        assert "x-talking-scenes" not in res.headers
        # Streamed in chunks: Cloud Run rejects single-piece responses over 32 MB.
        assert "content-length" not in res.headers
        assert res.content == b"mp4"


class TestEndCardAndSfx:
    def test_end_card_is_appended_after_last_scene(self, tmp_path: Path):
        req = _request(endCard={"handle": "@pinoycoach", "text": "Follow for part 2"})
        props = rr.build_props(req, [tmp_path / "scene-1.png", tmp_path / "scene-2.png"], None, None)
        end = props["cuts"][-1]
        assert len(props["cuts"]) == 3
        assert end["type"] == "reel_card" and end["in_seconds"] == 5.0 and end["out_seconds"] == 7.5
        assert end["card"] == {"kind": "end", "title": "@pinoycoach", "label": "Follow for part 2"}
        assert all(c["startMs"] < 5000 for c in props["captions"])  # no captions on the end card

    def test_motion_and_talking_ignore_the_end_card_index(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        req = _request(endCard={"handle": "@me"}, motionScenes=[2])
        props = rr.build_props(req, [tmp_path / "scene-1.png", tmp_path / "scene-2.png"], None, None)
        assert rr.add_motion_scenes(props, req, tmp_path, []) == 0

    def test_compare_card_kind(self):
        req = _request()
        req.scenes[0].card = rr.Card(kind="compare", items=["Myth: raw", "Fact: boiled"])
        assert req.scenes[0].card.kind == "compare"

    def test_sfx_events(self, tmp_path: Path):
        req = _request(endCard={"handle": "@me"})
        props = rr.build_props(req, [tmp_path / "scene-1.png", tmp_path / "scene-2.png"], None, None)
        assert rr.sfx_events(props) == [("whoosh", 1.75), ("whoosh", 4.75), ("pop", 5.35)]

    def test_mix_sfx_over_voice_and_over_silence(self, tmp_path: Path):
        import subprocess
        voice = tmp_path / "voice.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=3", str(voice)], check=True)
        out = rr.mix_sfx(voice, [("whoosh", 1.0), ("pop", 2.0)], 4.0, tmp_path)
        assert abs(rr.probe_duration(out) - 4.0) < 0.1
        out2 = rr.mix_sfx(None, [("pop", 0.5)], 2.0, tmp_path)
        assert abs(rr.probe_duration(out2) - 2.0) < 0.1


class TestClips:
    def test_drive_file_id(self):
        fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        assert rr.drive_file_id(f"https://drive.google.com/file/d/{fid}/view?usp=sharing") == fid
        assert rr.drive_file_id(f"https://drive.google.com/open?id={fid}") == fid
        with pytest.raises(HTTPException):
            rr.drive_file_id("https://youtube.com/watch?v=abc")

    def test_vertical_crop_follows_face(self):
        assert rr.vertical_crop(1920, 1080, None) == (606, 1080, 657, 0)       # centre
        assert rr.vertical_crop(1920, 1080, 0.15) == (606, 1080, 0, 0)        # face at far left: clamp
        assert rr.vertical_crop(1920, 1080, 0.75) == (606, 1080, 1137, 0)
        assert rr.vertical_crop(1080, 1920, 0.5) == (1080, 1920, 0, 0)        # already vertical

    def test_clip_captions_break_after_sentences(self):
        caps = rr.clip_captions([("Hello", 0.0, 0.4), ("there.", 0.4, 0.9), ("Next", 1.2, 1.5)])
        assert [c["pageBreakAfter"] for c in caps] == [False, True, False]
        assert caps[1]["startMs"] == 400 and caps[1]["endMs"] == 900

    def test_render_rejects_bad_spans(self, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        c = TestClient(rr.app)
        h = {"Authorization": "Bearer secret"}
        assert c.post("/clips/render", json={"driveUrl": "x", "start": 10, "end": 5}, headers=h).status_code == 400
        assert c.post("/clips/render", json={"driveUrl": "x", "start": 0, "end": 200}, headers=h).status_code == 400
        assert c.post("/clips/transcribe", json={"driveUrl": "x"}).status_code == 401

    def test_drive_html_page_is_explained(self, tmp_path: Path, monkeypatch):
        class HtmlResp:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return b""

        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda url, timeout=0: HtmlResp())
        with pytest.raises(HTTPException) as exc:
            rr._download_drive("abc", tmp_path / "v.video")
        assert "Anyone with the link" in exc.value.detail
        assert not (tmp_path / "v.video").exists()


class TestDub:
    def _tone(self, path: Path, seconds: float, freq: int = 300) -> Path:
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={freq}:duration={seconds}", str(path)], check=True)
        return path

    def _line(self, start, end, text="Kumusta"):
        return rr.DubLine(start=start, end=end, audio=PNG_URL.replace("image/png", "audio/mpeg"), text=text)

    def test_voice_track_places_lines_and_speeds_up_long_ones(self, tmp_path: Path):
        import wave
        a = self._tone(tmp_path / "a.mp3", 1.0)
        b = self._tone(tmp_path / "b.mp3", 3.0)  # only 2 s of room before the end of the video
        lines = [self._line(0.5, 1.5), self._line(2.0, 3.5)]
        out = rr.voice_track(lines, [a, b], 4.0, tmp_path / "voices.wav")
        with wave.open(str(out)) as w:
            assert w.getframerate() == rr.VOICE_RATE and w.getnchannels() == 1
            assert w.getnframes() >= 4 * rr.VOICE_RATE
        import numpy as np
        pcm = np.frombuffer(wave.open(str(out)).readframes(10**8), dtype=np.int16)
        rate = rr.VOICE_RATE
        assert np.abs(pcm[: int(0.4 * rate)]).max() < 50  # silence before the first line
        assert np.abs(pcm[int(0.7 * rate): int(1.3 * rate)]).max() > 1000  # first line playing
        # 3 s line squeezed by at most 1.35x into 2 s of room: it still reaches past 4 s * (no further than the buffer)
        assert np.abs(pcm[int(2.2 * rate): int(3.8 * rate)]).max() > 1000

    def test_subtitle_file(self, tmp_path: Path):
        out = rr.subtitle_file([self._line(1.0, 2.5, "Ano {ito}?"), self._line(0.0, 0.8, "")], 720, 1280, 120,
                               tmp_path / "s.ass")
        text = out.read_text()
        assert "PlayResY: 1280" in text
        assert "Dialogue: 0,0:00:01.00,0:00:02.50,Default,Ano (ito)?" in text
        assert text.count("Dialogue:") == 1  # empty lines are skipped

    # A thin band (one subtitle line found by find_subtitle_band) once broke boxblur's radius limit.
    @pytest.mark.parametrize("band", [(0.78, 0.94), (0.632, 0.701), (0.65, 0.67)])
    def test_render_with_background_and_cover(self, tmp_path: Path, monkeypatch, band):
        import subprocess
        src = tmp_path / "src.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=3",
                        "-f", "lavfi", "-i", "sine=frequency=200:duration=3", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
        stem = self._tone(tmp_path / "stem.mp3", 3.0, 120)
        voice = self._tone(tmp_path / "v.mp3", 1.0, 600)
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        monkeypatch.setattr(rr, "source_video", lambda url: src)
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: "https://cdn/x.mp3")
        sent = {}

        def fake_run(model, body, deadline, tag):
            sent.update(body, model=model)
            return {n: {"url": f"https://cdn/{n}.mp3"} for n in ("drums", "bass", "other")}

        monkeypatch.setattr(rr, "_fal_run", fake_run)
        monkeypatch.setattr(rr, "_download", lambda url, out: (out.write_bytes(stem.read_bytes()), out)[1])
        audio = "data:audio/mpeg;base64," + base64.b64encode(voice.read_bytes()).decode()
        body = {"driveUrl": "https://drive.google.com/file/d/abc/view",
                "coverTop": band[0], "coverBottom": band[1],
                "lines": [{"start": 0.5, "end": 1.5, "audio": audio, "text": "Para kay Tala?"}]}
        res = TestClient(rr.app).post("/dub/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200, res.text[:500]
        assert res.headers["x-dub-background"] == "kept"
        assert sent["model"] == rr.FAL_STEMS_MODEL and "vocals" not in sent["stems"]
        out = tmp_path / "out.mp4"
        out.write_bytes(res.content)
        assert abs(rr.probe_duration(out) - 3.0) < 0.2

    @pytest.mark.parametrize("intro_audio", [True, False])
    def test_prepend_intro_fits_and_joins(self, tmp_path: Path, intro_audio: bool):
        import subprocess
        intro = tmp_path / "intro.mp4"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=640x360:d=2:r=25"]
        if intro_audio:
            cmd += ["-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", "-shortest"]
        subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(intro)], check=True)
        main = tmp_path / "main.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=3",
                        "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", "-shortest", str(main)], check=True)
        out = rr.prepend_intro(intro, main, 360, 640, tmp_path / "out.mp4")
        assert abs(rr.probe_duration(out) - 5.0) < 0.3
        assert rr.probe_size(out) == (360, 640)
        assert rr._has_audio(out)

    def test_render_rejects_long_intro(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "source_video", lambda url: tmp_path / ("intro" if "intro" in url else "src"))
        monkeypatch.setattr(rr, "probe_duration", lambda p: 30.0 if p.name == "intro" else 3.0)
        body = {"driveUrl": "src", "introUrl": "intro",
                "lines": [{"start": 0, "end": 1, "audio": "data:audio/mpeg;base64,AAAA"}]}
        res = TestClient(rr.app).post("/dub/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 400 and "intro" in res.json()["detail"]

    def test_render_rejects_bad_cover(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "source_video", lambda url: tmp_path / "missing.mp4")
        monkeypatch.setattr(rr, "probe_duration", lambda p: 3.0)
        body = {"driveUrl": "x", "coverTop": 0.9, "coverBottom": 0.8,
                "lines": [{"start": 0, "end": 1, "audio": "data:audio/mpeg;base64,AAAA"}]}
        res = TestClient(rr.app).post("/dub/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 400

    def test_transcribe_uses_chinese_and_dub_model(self, tmp_path: Path, monkeypatch):
        import subprocess
        src = tmp_path / "src.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x568:d=2",
                        "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(src)], check=True)
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "source_video", lambda url: src)
        seen = {}

        def fake_segments(path, language=None, model_name=None):
            seen.update(language=language, model=model_name)
            return [{"start": 0.0, "end": 1.0, "text": "你好"}]

        monkeypatch.setattr(rr, "transcribe_segments", fake_segments)
        res = TestClient(rr.app).post("/dub/transcribe", json={"driveUrl": "x"}, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        data = res.json()
        assert data["segments"][0]["text"] == "你好" and data["width"] == 320 and data["height"] == 568
        assert seen == {"language": "zh", "model": rr.DUB_WHISPER_MODEL}
        assert data["frames"] and data["frames"][0].startswith("data:image/jpeg;base64,")
        assert "pitch" in data["segments"][0]
        assert "subtitleBand" in data

    def test_find_subtitle_band_spots_outlined_text_rows(self):
        import numpy as np
        rng = np.random.default_rng(1)

        def still(with_text: bool):
            img = rng.integers(70, 200, size=(640, 360), dtype=np.uint8)  # a changing scene
            if with_text:
                for x in range(40, 320, 12):  # white strokes with dark outlines at 64-68% height
                    img[410:436, x - 3: x] = 20
                    img[410:436, x: x + 4] = 250
                    img[410:436, x + 4: x + 7] = 20
            return img

        band = rr.find_subtitle_band([still(True), still(True), still(False), still(True)])
        assert band and 0.6 <= band["top"] <= 0.645 and 0.68 <= band["bottom"] <= 0.72
        assert isinstance(band["top"], float)
        assert rr.find_subtitle_band([still(False), still(False), still(False)]) is None

    def test_segment_subtitle_crops_adds_a_small_image_per_line(self, tmp_path: Path):
        import subprocess
        src = tmp_path / "src.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
        segs = [{"start": 0.0, "end": 0.8}, {"start": 1.0, "end": 1.8}]
        rr.segment_subtitle_crops(src, segs, {"top": 0.6, "bottom": 0.7})
        assert all(s["subtitleImage"].startswith("data:image/jpeg;base64,") for s in segs)

    def test_segment_pitch_tells_low_from_high_voices(self, tmp_path: Path):
        import subprocess
        audio = tmp_path / "a.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*120*t)+0.2*sin(2*PI*240*t)':d=2:s=16000",
                        "-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*220*t)+0.2*sin(2*PI*440*t)':d=2:s=16000",
                        "-filter_complex", "[0][1]concat=n=2:v=0:a=1,aformat=sample_fmts=s16:channel_layouts=mono",
                        str(audio)], check=True)
        segs = [{"start": 0.1, "end": 1.9}, {"start": 2.1, "end": 3.9}, {"start": 0.0, "end": 0.05}]
        rr.segment_pitch(audio, segs)
        assert 110 <= segs[0]["pitch"] <= 130
        assert 205 <= segs[1]["pitch"] <= 235
        assert segs[2]["pitch"] is None


class TestSafety:
    """Token on every route, lock before fal, fal spend log and cap, the source cache, stream size."""

    def _render_stub(self, monkeypatch):
        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"fake-mp4")
            return out
        monkeypatch.setattr(rr, "render", fake_render)
        monkeypatch.setattr(rr, "finish_reel", _no_finish)  # the stub's bytes aren't a video ffmpeg can finish

    def test_every_post_route_needs_the_token_even_with_a_bad_body(self, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        c = TestClient(rr.app)
        posts = sorted(r.path for r in rr.app.routes if "POST" in getattr(r, "methods", ()))
        assert {"/render", "/clips/transcribe", "/clips/render", "/dub/transcribe", "/dub/render"} <= set(posts)
        for path in posts:
            assert c.post(path, json={}).status_code == 401, path
            assert c.post(path, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401, path
        assert c.get("/health").status_code == 200 and c.get("/healthz").status_code == 200

    def test_busy_render_is_refused_before_fal_is_called(self, monkeypatch):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "FAL_KEY", "k")
        monkeypatch.setattr(rr, "LOCK_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(rr, "transcribe", lambda p: None)
        calls = []
        monkeypatch.setattr(rr, "_fal_run", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(rr, "_fal_json", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(rr, "_fal_file_url", lambda *a, **k: calls.append(a))
        # The only ways /render reaches fal.
        monkeypatch.setattr(rr, "add_talking_scenes", lambda *a, **k: calls.append(a) or 0)
        monkeypatch.setattr(rr, "add_motion_scenes", lambda *a, **k: calls.append(a) or 0)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}, {"image": PNG_URL, "durationSeconds": 2}],
                "voiceover": WAV_URL, "talkingImage": PNG_URL, "talkingScenes": [0], "motionScenes": [1]}
        assert rr._render_lock.acquire(timeout=1)
        try:
            res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        finally:
            rr._render_lock.release()
        assert res.status_code == 429
        assert calls == []

    def test_fal_failures_and_expected_bytes_headers(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        self._render_stub(monkeypatch)

        def failing_talk(props, req, job_dir, voice, failures=None):
            failures.append("talking scene 1")
            return 0

        monkeypatch.setattr(rr, "add_talking_scenes", failing_talk)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "talkingImage": PNG_URL, "talkingScenes": [0]}
        res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.headers["x-fal-failures"] == "1" and res.headers["x-talking-scenes"] == "0/1"
        # Still streamed (no Content-Length), but the client can check it got the whole file.
        assert "content-length" not in res.headers
        assert res.headers["x-expected-bytes"] == str(len(b"fake-mp4")) == str(len(res.content))

    def test_fal_job_log_line(self, monkeypatch, tmp_path: Path, capsys):
        import subprocess
        monkeypatch.setattr(rr, "FAL_KEY", "fal-secret-key")
        monkeypatch.setattr(rr, "_fal_spend", {"day": "", "usd": 0.0})
        voice = tmp_path / "voice.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=4", str(voice)],
                       check=True)
        face = tmp_path / "face.jpg"
        face.write_bytes(PNG_1PX)
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: f"https://cdn/{path.name}")
        monkeypatch.setattr(rr, "_fal_run", lambda model, body, deadline, tag: {"video": {"url": "https://cdn/v.mp4"}})
        monkeypatch.setattr(rr, "_download", lambda url, out: (out.write_bytes(b"mp4"), out)[1])
        rr.talking_clip(face, voice, 1.0, 3.0, tmp_path / "talk-1.mp4", time.monotonic() + 60)

        def boom(model, body, deadline, tag):
            raise RuntimeError("fal HTTP 403: out of credit")
        monkeypatch.setattr(rr, "_fal_run", boom)
        with pytest.raises(RuntimeError):
            rr.talking_clip(face, voice, 0.0, 1.0, tmp_path / "talk-2.mp4", time.monotonic() + 60)

        out = capsys.readouterr().out
        logs = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert [(l["event"], l["status"]) for l in logs] == [("fal_job", "ok"), ("fal_job", "failed")]
        assert logs[0]["model"] == rr.FAL_TALKING_MODEL and logs[0]["seconds"] == 2.0
        assert logs[0]["estimateUsd"] == pytest.approx(0.04) and logs[0]["severity"] == "INFO"
        assert "out of credit" in logs[1]["error"] and logs[1]["dayTotalUsd"] == pytest.approx(0.06)
        assert "fal-secret-key" not in out

    def test_daily_cap_refuses_past_the_cap(self, monkeypatch, capsys):
        monkeypatch.setattr(rr, "FAL_DAILY_CAP_USD", 0.10)
        monkeypatch.setattr(rr, "_fal_spend", {"day": "", "usd": 0.0})
        sent = []
        monkeypatch.setattr(rr, "_fal_run", lambda model, body, deadline, tag: sent.append(model) or {"ok": 1})
        for _ in range(2):  # 2 s of FlashTalk is about $0.04
            rr._fal_paid("talking", "fal-ai/flashtalk", {}, time.monotonic() + 60, 2.0)
        with pytest.raises(RuntimeError, match="cap"):
            rr._fal_paid("talking", "fal-ai/flashtalk", {}, time.monotonic() + 60, 2.0)
        assert len(sent) == 2  # the third job never reached fal
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        assert logs[-1]["status"] == "cap" and logs[-1]["capUsd"] == 0.10
        # A new Manila day starts from zero.
        monkeypatch.setattr(rr, "_fal_spend", {"day": "2000-01-01", "usd": 99.0})
        rr._fal_paid("talking", "fal-ai/flashtalk", {}, time.monotonic() + 60, 2.0)
        assert len(sent) == 3

    def test_estimates_follow_the_model(self):
        assert rr.fal_estimate("motion", "fal-ai/kling-video/v2.5-turbo/standard/image-to-video", 5) == 0.21
        assert rr.fal_estimate("motion", "some/new-model", 5) == 0.7  # unknown: the kind's fallback rate
        assert rr.fal_estimate("stems", rr.FAL_STEMS_MODEL, 900) == rr.FAL_STEMS_USD

    def test_fal_result_download_is_capped(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "MAX_FAL_RESULT_BYTES", 10)
        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(b"x" * 20))
        with pytest.raises(RuntimeError, match="over"):
            rr._download("https://cdn/big.mp4", tmp_path / "big.mp4")


class TestSourceCache:
    NEW = "N" * 25

    def _drive(self, monkeypatch, body: bytes):
        class DriveResp(io.BytesIO):
            headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body))}
        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda url, timeout=0: DriveResp(body))

    def _cached(self, cache: Path, name: str, size: int, age: float) -> Path:
        f = cache / f"{name}.video"
        f.write_bytes(b"v" * size)
        then = time.time() - age
        os.utime(f, (then, then))
        return f

    @pytest.fixture
    def cache(self, monkeypatch, tmp_path: Path) -> Path:
        monkeypatch.setattr(rr, "CLIP_CACHE", tmp_path)
        monkeypatch.setattr(rr, "CLIP_CACHE_MAX_BYTES", 10_000)
        monkeypatch.setattr(rr, "probe_duration", lambda p: 60.0)
        return tmp_path

    def test_drops_oldest_sources_but_keeps_ones_in_use(self, cache: Path, monkeypatch):
        import threading
        locked = self._cached(cache, "A" * 25, 4000, 3000)   # oldest, but another request holds its lock
        old = self._cached(cache, "B" * 25, 4000, 2000)      # oldest free one: dropped
        spare = self._cached(cache, "C" * 25, 2000, 1000)    # not needed to make room
        recent = self._cached(cache, "D" * 25, 1000, 10)     # handed out a moment ago
        lock = threading.Lock()
        lock.acquire()
        monkeypatch.setitem(rr._source_locks, locked.stem, lock)
        self._drive(monkeypatch, b"n" * 3000)
        path = rr.source_video(f"https://drive.google.com/file/d/{self.NEW}/view")
        assert path.read_bytes() == b"n" * 3000
        assert not old.exists()
        assert locked.exists() and spare.exists() and recent.exists()
        assert sum(f.stat().st_size for f in cache.iterdir()) <= rr.CLIP_CACHE_MAX_BYTES

    def test_refuses_when_everything_is_in_use(self, cache: Path, monkeypatch):
        busy = self._cached(cache, "D" * 25, 9000, 10)
        self._drive(monkeypatch, b"n" * 3000)
        with pytest.raises(HTTPException) as exc:
            rr.source_video(f"https://drive.google.com/file/d/{self.NEW}/view")
        assert exc.value.status_code == 429
        assert busy.exists() and sorted(f.name for f in cache.iterdir()) == [busy.name]

    def test_too_long_source_is_not_cached(self, cache: Path, monkeypatch):
        monkeypatch.setattr(rr, "probe_duration", lambda p: 100 * 60.0)
        self._drive(monkeypatch, b"n" * 3000)
        with pytest.raises(HTTPException) as exc:
            rr.source_video(f"https://drive.google.com/file/d/{self.NEW}/view")
        assert exc.value.status_code == 400 and "minutes" in exc.value.detail
        assert list(cache.iterdir()) == []
        assert rr._cache_reserved == {}

    def test_download_deadline(self, cache: Path, monkeypatch):
        monkeypatch.setattr(rr, "DRIVE_DOWNLOAD_DEADLINE_S", -1)
        self._drive(monkeypatch, b"n" * 3000)
        with pytest.raises(HTTPException) as exc:
            rr.source_video(f"https://drive.google.com/file/d/{self.NEW}/view")
        assert exc.value.status_code == 504
        assert list(cache.iterdir()) == []


class TestFinishing:
    """Every /render output goes through the finishing pass (the AI tag and -14 LUFS) unless asked not to."""

    def _client(self, monkeypatch, calls: list):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"raw-render")
            return out

        def fake_finish(raw, out, ai_label, normalize=True, **_):
            calls.append({"ai_label": ai_label, "normalize": normalize})
            out.write_bytes(b"finished")
            return {"aiLabel": ai_label, "lufs": -14.2 if normalize else None, "truePeak": -1.3 if normalize else None}

        monkeypatch.setattr(rr, "render", fake_render)
        monkeypatch.setattr(rr.finishing, "finish", fake_finish)
        return TestClient(rr.app)

    def test_label_and_loudness_are_on_by_default(self):
        req = rr.RenderRequest(scenes=[{"image": PNG_URL, "durationSeconds": 2}])
        assert req.aiLabel is True and req.normalizeAudio is True

    def test_render_returns_the_finished_file_and_says_so(self, monkeypatch):
        calls: list = []
        client = self._client(monkeypatch, calls)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}]}
        res = client.post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.content == b"finished"
        assert calls == [{"ai_label": True, "normalize": True}]
        assert res.headers["x-ai-label"] == "1"
        assert res.headers["x-loudness-lufs"] == "-14.2"
        assert res.headers["x-true-peak-dbtp"] == "-1.3"
        assert res.headers["x-expected-bytes"] == str(len(b"finished"))

    def test_label_can_be_switched_off_for_a_reel_with_nothing_ai(self, monkeypatch):
        calls: list = []
        client = self._client(monkeypatch, calls)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "aiLabel": False}
        res = client.post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert calls == [{"ai_label": False, "normalize": True}]
        assert res.headers["x-ai-label"] == "0"

    def test_both_off_returns_the_raw_render_untouched(self, monkeypatch):
        calls: list = []
        client = self._client(monkeypatch, calls)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "aiLabel": False, "normalizeAudio": False}
        res = client.post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.content == b"raw-render"
        assert calls == []
        assert "x-ai-label" not in res.headers and "x-loudness-lufs" not in res.headers

    def test_finish_headers_leave_out_what_was_not_measured(self):
        assert rr.finish_headers({}) == {}
        assert rr.finish_headers({"aiLabel": True, "lufs": None, "truePeak": None}) == {"X-Ai-Label": "1"}
        assert rr.finish_headers({"aiLabel": False, "lufs": -13.96, "truePeak": -1.04}) == {
            "X-Ai-Label": "0", "X-Loudness-Lufs": "-14.0", "X-True-Peak-Dbtp": "-1.0"}


# 28 Oct 2026: with PyAV 19, faster-whisper's decoder calls av.open(..., metadata_errors=...) and gets a
# TypeError, so transcribe() returned None and every reel's captions fell back to estimated timing.
def test_faster_whisper_can_decode_audio(tmp_path):
    import subprocess

    from faster_whisper.audio import decode_audio

    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)], check=True)
    samples = decode_audio(str(wav))
    assert 15000 < len(samples) < 17000  # 1 s at 16 kHz
