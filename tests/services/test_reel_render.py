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

    def test_endpoint_accepts_music(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        seen = {}

        def fake_render(props, job_dir, aspect):
            seen["audio"] = props["audio"]
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
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

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"clip-bytes"

        monkeypatch.setattr(rr, "_fal_json", fake_json)
        monkeypatch.setattr(rr.urllib.request, "urlopen", lambda url, timeout=0: FakeResp())
        monkeypatch.setattr(rr, "_fal_file_url", lambda path, mime: f"https://cdn/{path.name}")
        out = rr.talking_clip(face, voice, 1.0, 3.0, tmp_path / "talk-1.mp4", rr.time.monotonic() + 60)
        assert out.read_bytes() == b"clip-bytes"
        assert sent == {"image_url": "https://cdn/face.jpg", "audio_url": "https://cdn/talk-1.mp3"}
        assert abs(rr.probe_duration(tmp_path / "talk-1.mp3") - 2.0) < 0.1  # just the scene's stretch

    def test_endpoint_reports_talking_header(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(rr, "RENDER_TOKEN", "secret")
        monkeypatch.setattr(rr, "transcribe", lambda p: None)
        monkeypatch.setattr(rr, "add_talking_scenes", lambda props, req, job_dir, voice: 1)

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
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
        monkeypatch.setattr(rr, "add_motion_scenes", lambda props, req, job_dir, paths: 1)

        def fake_render(props, job_dir, aspect):
            out = job_dir / "reel.mp4"
            out.write_bytes(b"mp4")
            return out

        monkeypatch.setattr(rr, "render", fake_render)
        body = {"scenes": [{"image": PNG_URL, "durationSeconds": 2}], "motionScenes": [0]}
        res = TestClient(rr.app).post("/render", json=body, headers={"Authorization": "Bearer secret"})
        assert res.status_code == 200
        assert res.headers["x-motion-scenes"] == "1/1"
        assert "x-talking-scenes" not in res.headers
        # Streamed in chunks: Cloud Run rejects single-piece responses over 32 MB.
        assert "content-length" not in res.headers
        assert res.content == b"mp4"


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
