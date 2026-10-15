#!/bin/bash
# SessionStart hook: prepare OpenMontage in Claude Code cloud sessions.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Python venv + deps (incl. pytest for the test suite)
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements-dev.txt
.venv/bin/python -m pip install --quiet piper-tts || echo "[skip] piper-tts install failed"

# Remotion composer (npm install, not ci, so the container cache is reused)
(cd remotion-composer && npm install --no-audit --no-fund --loglevel=error)

# Warm the HyperFrames npx cache
npx --yes hyperframes --version >/dev/null 2>&1 || echo "[skip] HyperFrames cache-warm failed"

# Local .env (API keys are added by the user; never committed)
[ -f .env ] || cp .env.example .env

# Make the venv the default python for the session
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\"" >> "$CLAUDE_ENV_FILE"
  echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# Google Cloud / Vertex AI: cloud environments can only hold env vars, not files,
# so materialize a service-account key from GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON
# or base64) into a file outside the repo and point GOOGLE_APPLICATION_CREDENTIALS at it.
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
  sa_dir="$HOME/.config/openmontage"
  sa_file="$sa_dir/gcp-service-account.json"
  mkdir -p "$sa_dir"
  (
    umask 077
    case "$GOOGLE_SERVICE_ACCOUNT_JSON" in
      \{*) printf '%s' "$GOOGLE_SERVICE_ACCOUNT_JSON" > "$sa_file" ;;
      *) printf '%s' "$GOOGLE_SERVICE_ACCOUNT_JSON" | base64 -d > "$sa_file" ;;
    esac
  )
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export GOOGLE_APPLICATION_CREDENTIALS=\"$sa_file\"" >> "$CLAUDE_ENV_FILE"
    echo "export GOOGLE_GENAI_USE_VERTEXAI=true" >> "$CLAUDE_ENV_FILE"
  fi
fi
