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

## Speed

In testing, a 10-second reel took about 27 seconds on 4 CPUs, plus a few seconds to transcribe
the voice. Render time grows with reel
length, so a 45-second reel takes roughly 2 minutes.
