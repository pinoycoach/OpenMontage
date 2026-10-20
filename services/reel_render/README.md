# Reel render service

A small web service that renders a ViralReel Studio reel with OpenMontage's Remotion
`Explainer` composition and returns an HD MP4: 1080×1920 (or 1920×1080), H.264 + AAC, with
the reel's zoom and pan moves, the voiceover, and big word-by-word captions that sit above the
Reels/TikTok buttons. ViralReel Studio calls it from its server when `OPENMONTAGE_RENDER_URL`
is set; without that setting the app doesn't change.

It only renders. It calls no paid AI APIs, so it only costs Cloud Run CPU time.

## API

`POST /render` with `Authorization: Bearer <RENDER_TOKEN>` and JSON:

```json
{
  "aspectRatio": "9:16",
  "captionStyle": "yellow-pop",
  "captions": true,
  "voiceover": "data:audio/wav;base64,...",
  "music": "data:audio/mpeg;base64,...",
  "musicVolume": 0.2,
  "scenes": [
    { "image": "data:image/jpeg;base64,...", "durationSeconds": 3,
      "cameraMotion": "zoom-in", "captionOverlay": "HOOK", "voiceoverLine": "Stop scrolling." }
  ]
}
```

It returns `video/mp4`, streamed in chunks (Cloud Run caps a one-piece response at 32 MB), so there
is no `Content-Length`; `X-Expected-Bytes` gives the file size instead, so the caller can tell a
cut-off stream from a whole one (every video route sends it). Assets must be base64 data URLs, so
the service never fetches outside URLs. One video renders at a time per instance; a request that
can't get its turn within a few seconds gets `429`, before any fal clip is made.

- **With a voiceover**, the captions follow `voiceoverLine`. The service transcribes the voice with
  faster-whisper and lines the heard word timings up with the script. Each word lights up when
  it's spoken, and each scene cuts between the end of one line and the start of the next. If
  transcription fails, or doesn't match the script well enough, it falls back to spreading the
  words evenly across each scene, which is then stretched to fit the voice.
- **Without a voiceover**, the captions show `captionOverlay`.
- **`music`** (optional) is looped or trimmed to the video and faded in and out. It's ducked under
  the voice, so a sidechain compressor lowers it while someone speaks. `musicVolume` (0–1,
  default 0.2) sets its level before ducking.

Every route except the health check needs the token (it's an app-wide dependency, so a new route is
covered too). A body that isn't JSON at all gets `422` before the token is checked.

`GET /health` is a health check. Cloud Run reserves paths ending in "z", so `/healthz` only works
locally. `CAPTION_SYNC=off` turns off the speech timing.

Run locally from the repo root:

```bash
RENDER_TOKEN=dev .venv/bin/uvicorn services.reel_render.app:app --port 8090
```

## Deploy to Cloud Run (in Cloud Shell)

Use the same Google Cloud project as ViralReel Studio, so the free-trial credit covers it.

```bash
PROJECT=$(gcloud config get-value project)
REGION=asia-southeast1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create openmontage --repository-format=docker --location=$REGION

# A random token that only the app's server knows (it never reaches the browser)
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create reel-render-token --data-file=-
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding reel-render-token \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

git clone https://github.com/pinoycoach/OpenMontage.git && cd OpenMontage
gcloud builds submit --config services/reel_render/cloudbuild.yaml \
  --substitutions=_REGION=$REGION,SHORT_SHA=$(git rev-parse --short HEAD) .

gcloud run deploy openmontage-render \
  --image $REGION-docker.pkg.dev/$PROJECT/openmontage/reel-render:latest \
  --region $REGION \
  --allow-unauthenticated \
  --cpu 4 --memory 4Gi \
  --concurrency 1 --max-instances 3 --min-instances 0 \
  --timeout 900 \
  --set-secrets RENDER_TOKEN=reel-render-token:latest
```

`--allow-unauthenticated` only opens the address. Every render still needs the token.
`--min-instances 0` means it costs nothing while idle, but the first render after a quiet
spell waits for the service to start, usually under 30 seconds.

To deploy automatically on every push to `main`, add a Cloud Build trigger for this repo with
config file `services/reel_render/cloudbuild.yaml` and the substitution
`_SERVICE=openmontage-render`. The build then also runs `gcloud run deploy` with the new image, and
the service keeps its memory, timeout, secrets and environment. Every build first runs
`python -m pytest -q tests/services/test_reel_render.py` and stops if a test fails; images are
tagged with the commit (`$SHORT_SHA`, which is what gets deployed) and `latest`.
`services/reel_render/.env.example` lists every setting the service reads.

Then turn it on in ViralReel Studio. See that repo's README, section "HD Reel export (OpenMontage)".

## Talking scenes (optional)

With a fal key, a request can ask for talking scenes: `talkingImage` (the persona photo as a data
URL) and `talkingScenes` (0-based scene numbers, at most 3). For each one the service cuts that
scene's stretch of the narration, sends it with the photo to fal's `fal-ai/flashtalk` model, and
plays the lip-synced clip instead of the image. Because the audio comes from the narration
itself, the lips match what the viewer hears. A clip that fails, or isn't ready within
`TALKING_TIMEOUT_SECONDS` (default 360), falls back to the scene's image; the response header
`X-Talking-Scenes` says how many were made, e.g. `2/2`. `/health` reports `"talking": true`
when a key is set.

Store the key once and give the service access:

```bash
read -s -p "fal key: " K && printf '%s' "$K" | gcloud secrets create fal-api-key --data-file=- && unset K
gcloud secrets add-iam-policy-binding fal-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud run services update openmontage-render --region $REGION --update-secrets FAL_KEY=fal-api-key:latest
```

fal bills about $0.02 per second of talking clip, charged to the fal account, not Google Cloud.

Every fal job writes one JSON log line (`"event": "fal_job"`: model, seconds, estimated USD,
`status` `ok`, `failed` or `cap`, and the day's estimated total). With `FAL_DAILY_CAP_USD` set, a
job that would take the day's estimate (Manila date, counted per instance, reset when the instance
restarts) past it isn't sent; the scene or background falls back as if fal had failed. `/render`
and `/dub/render` report failed or refused fal jobs in `X-Fal-Failures` (a count). The estimates are in
`FAL_USD_PER_SECOND` and `FAL_STEMS_USD` in `app.py`; the fal dashboard has the real charges.

## Cards, particles and motion scenes (optional)

- **Animated cards.** A scene with `card: {kind: "stat"|"list"|"quote", title, label, items, source}`
  is drawn as an animated card (the number counts up, list items slide in, quote words appear one by
  one). The scene's `image` is still required; it is the still version of the card.
- **Comparison cards.** `card.kind: "compare"` with two `items` written as `"Label: text"` (e.g.
  `"Myth: …"`, `"Fact: …"`) shows the two sides one after the other, red then green, with an
  optional heading in `title`.
- **End card.** `endCard: {handle, text}` adds a 2.5 s closing card after the last scene with the
  handle and a call-to-action button (`text` defaults to "Follow for more").
- **Sound effects.** `sfx: true` adds a soft whoosh before each scene change and a pop when a card
  appears, mixed under the voice. The effects are made with ffmpeg, so there are no sound files.
- **Particles.** `particles` (`fireflies`, `petals`, `sparkles`, `mist` or `light-rays`) floats a
  light particle layer over the whole video, for styles such as an anime look.
- **Motion scenes.** With the fal key set, `motionScenes` (0-based scene numbers, at most 2) turns
  those scene images into short video clips with `FAL_MOTION_MODEL` (default
  `fal-ai/kling-video/v3/standard/image-to-video`, about $0.14 per second of clip, 3–10 s). A
  scene's optional `motionPrompt` says what should move. Each clip is retimed to the scene's length.
  Card scenes and talking scenes are skipped. A clip that fails, or isn't ready within
  `MOTION_TIMEOUT_SECONDS` (default 420), keeps the image. The header `X-Motion-Scenes` says how many
  were made. Motion and talking clips are made at the same time.

## Clips (long video → short vertical clips)

For the app's Clips tab. The source is a Google Drive file shared as "Anyone with the link".

- `POST /clips/transcribe {driveUrl}` downloads the video (cached on the instance), transcribes
  it and returns `{duration, segments: [{start, end, text}]}`. The app picks the moments.
- `POST /clips/render {driveUrl, start, end, captionStyle}` cuts that span, finds the speaker's
  face (OpenCV) and crops to 9:16 around it, then adds word-by-word captions from the speech.

Limits: `MAX_SOURCE_MINUTES` (default 90), `MAX_SOURCE_MB` (default 1500), clips up to 90 s, and
`DRIVE_DOWNLOAD_DEADLINE_S` (default 600) for one download. A video's length is checked before it
is cached, so a refused one doesn't stay in memory. The cache is kept under `CLIP_CACHE_MAX_MB`
(default 2500) by dropping the oldest sources first; a source used in the last 15 minutes is kept,
and if nothing can go the request gets `429`.
Downloaded videos live in memory on Cloud Run, so give the service 8 GiB for long sources:
`gcloud run services update openmontage-render --region $REGION --memory 8Gi`.

## Dub (one episode at a time)

`POST /dub/transcribe {driveUrl, language}` returns the episode's lines in its own language
(`language` defaults to `zh`; transcribed with `DUB_WHISPER_MODEL`, default `small`), plus the frame
size. Each line also carries a rough voice `pitch` in Hz (men about 85-160, women about 165-260; a
hint for telling speakers apart), and `frames` holds a few small JPEG stills taken while people
speak, so the app can find where the burned-in subtitles sit. The app translates the lines and makes
the new voices.

`POST /dub/render {driveUrl, lines: [{start, end, audio, text}], keepBackground, coverSubtitles,
coverTop, coverBottom, subtitles}` lays each new voice line (an audio data URL) where the original
line started, speeding it up by at most 1.35x if it would run into the next line.
- `keepBackground` splits the original soundtrack with Demucs on fal (`FAL_STEMS_MODEL`, about
  $0.0007 per second) and keeps the music and effects without the voices.
- `coverSubtitles` blurs and darkens the band between `coverTop` and `coverBottom` (fractions of the
  frame height) where the old burned-in subtitles sit.
- `subtitles` burns the new `text` lines into that band.

The header `X-Dub-Background` says `kept`, `failed`, `unavailable` (no fal key) or `off`.

## Speed

In testing, a 10-second reel took about 27 seconds on 4 CPUs, plus a few seconds to transcribe
the voice. Render time grows with reel
length, so a 45-second reel takes roughly 2 minutes.
