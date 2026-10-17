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

It returns `video/mp4`. Assets must be base64 data URLs, so the service never fetches
outside URLs.

- **With a voiceover**, the captions follow `voiceoverLine`. The service transcribes the voice with
  faster-whisper and lines the heard word timings up with the script. Each word lights up when
  it's spoken, and each scene cuts between the end of one line and the start of the next. If
  transcription fails, or doesn't match the script well enough, it falls back to spreading the
  words evenly across each scene, which is then stretched to fit the voice.
- **Without a voiceover**, the captions show `captionOverlay`.
- **`music`** (optional) is looped or trimmed to the video and faded in and out. It's ducked under
  the voice, so a sidechain compressor lowers it while someone speaks. `musicVolume` (0–1,
  default 0.2) sets its level before ducking.

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
gcloud builds submit --config services/reel_render/cloudbuild.yaml --substitutions=_REGION=$REGION .

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

## Cards, particles and motion scenes (optional)

- **Animated cards.** A scene with `card: {kind: "stat"|"list"|"quote", title, label, items, source}`
  is drawn as an animated card (the number counts up, list items slide in, quote words appear one by
  one). The scene's `image` is still required; it is the still version of the card.
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

Limits: `MAX_SOURCE_MINUTES` (default 90), `MAX_SOURCE_MB` (default 3000), clips up to 90 s.
Downloaded videos live in memory on Cloud Run, so give the service 8 GiB for long sources:
`gcloud run services update openmontage-render --region $REGION --memory 8Gi`.

## Speed

In testing, a 10-second reel took about 27 seconds on 4 CPUs, plus a few seconds to transcribe
the voice. Render time grows with reel
length, so a 45-second reel takes roughly 2 minutes.
