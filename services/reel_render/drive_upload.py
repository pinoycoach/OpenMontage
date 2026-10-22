"""Upload a finished batch folder to Google Drive (KGL DELIVERIES), so Drive for desktop syncs it to Nap's laptop.

The Drive connector only takes small text files; this uses Drive's resumable upload, so MP4s of any size go up.
Credentials: Nap's own Google sign-in, as three environment variables set in the environment's settings (never in
chat, never printed): GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN (from the OAuth Playground, scope
https://www.googleapis.com/auth/drive). Files are owned by Nap and use his Drive storage. (A service account can't be
used: Google gives service accounts no storage of their own in a personal My Drive.) GDRIVE_SA_KEY still works for a
Shared Drive.

  python services/reel_render/drive_upload.py <batch_folder> --parent <drive_folder_id> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

API = "https://www.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/drive"]
SKIP = {".DS_Store"}


class UploadError(RuntimeError):
    pass


def files_to_upload(folder: Path) -> list[Path]:
    """Every file in the batch folder, inputs included, in a stable order."""
    if not folder.is_dir():
        raise UploadError(f"No such folder: {folder}")
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.name not in SKIP)


def _session():
    from google.auth.transport.requests import AuthorizedSession

    env = {k: os.environ.get(k, "").strip() for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN")}
    if all(env.values()):
        from google.oauth2.credentials import Credentials

        creds = Credentials(None, refresh_token=env["GDRIVE_REFRESH_TOKEN"], client_id=env["GDRIVE_CLIENT_ID"],
                            client_secret=env["GDRIVE_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token",
                            scopes=SCOPES)
        return AuthorizedSession(creds), "Nap's Google account"
    key = os.environ.get("GDRIVE_SA_KEY", "").strip()
    if not key:
        missing = [k for k, v in env.items() if not v]
        raise UploadError(f"Drive sign-in is not set: add {', '.join(missing)} in the environment's settings")
    from google.oauth2 import service_account

    try:
        info = json.loads(key)
    except json.JSONDecodeError as exc:
        raise UploadError("GDRIVE_SA_KEY is not valid JSON (paste the whole key file as the value)") from exc
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds), info.get("client_email", "?")


def _folder(s, name: str, parent: str) -> str:
    """Find or create a sub-folder by name (so re-runs don't make duplicates)."""
    q = (f"name = '{name}' and '{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    r = s.get(f"{API}/drive/v3/files", params={"q": q, "fields": "files(id)", "supportsAllDrives": "true",
                                               "includeItemsFromAllDrives": "true"})
    r.raise_for_status()
    hits = r.json().get("files", [])
    if hits:
        return hits[0]["id"]
    r = s.post(f"{API}/drive/v3/files", params={"supportsAllDrives": "true"},
               json={"name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"})
    r.raise_for_status()
    return r.json()["id"]


def _upload(s, path: Path, parent: str) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    r = s.post(f"{API}/upload/drive/v3/files", params={"uploadType": "resumable", "supportsAllDrives": "true"},
               json={"name": path.name, "parents": [parent]},
               headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(path.stat().st_size)})
    r.raise_for_status()
    with path.open("rb") as fh:
        done = s.put(r.headers["Location"], data=fh, headers={"Content-Type": mime})
    done.raise_for_status()
    return done.json()["id"]


def upload_batch(folder: Path, parent: str, dry_run: bool = False) -> list[str]:
    files = files_to_upload(folder)
    lines = [f"{folder.name}/{p.relative_to(folder)}  ({p.stat().st_size / 1e6:.1f} MB)" for p in files]
    if dry_run:
        return lines
    s, who = _session()
    ids = {Path("."): _folder(s, folder.name, parent)}
    for p in files:
        rel = p.relative_to(folder).parent
        for part in [Path(*rel.parts[:i + 1]) for i in range(len(rel.parts))]:
            if part not in ids:
                ids[part] = _folder(s, part.name, ids[part.parent if part.parent != Path("") else Path(".")])
        _upload(s, p, ids[rel if rel != Path("") else Path(".")])
    return lines + [f"uploaded {len(files)} files as {who}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--parent", required=True, help="Drive folder id (KGL DELIVERIES)")
    ap.add_argument("--dry-run", action="store_true", help="list what would go up; no credentials needed")
    args = ap.parse_args(argv)
    try:
        for line in upload_batch(args.folder, args.parent, args.dry_run):
            print(line)
    except UploadError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
