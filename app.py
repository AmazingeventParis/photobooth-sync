"""
Photobooth Sync — Service Python Flask
Importe les photos d'un dossier Google Drive vers OVH S3
et insere les lignes dans Supabase appshoot_photos.
"""

import os
import io
import json
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import boto3
import requests
from PIL import Image

app = Flask(__name__)
# Autoriser les appels depuis shootnbox.fr et tous les domaines en local pour debug
CORS(app, resources={r"/*": {"origins": "*", "allow_headers": ["Content-Type", "X-Admin-Password"]}})

# ------------------------------------------------------------------
# Configuration via variables d'environnement
# ------------------------------------------------------------------
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SA_KEY", "").strip()
DRIVE_PARENT_FOLDER  = os.getenv("DRIVE_PARENT_FOLDER", "").strip()
S3_ENDPOINT          = os.getenv("S3_ENDPOINT", "https://s3.sbg.io.cloud.ovh.net")
S3_BUCKET            = os.getenv("S3_BUCKET", "app-media-shootnbox")
S3_PUBLIC_HOST       = os.getenv("S3_PUBLIC_HOST", "app-media-shootnbox.s3.sbg.io.cloud.ovh.net")
S3_REGION            = os.getenv("S3_REGION", "sbg")
S3_KEY               = os.getenv("S3_KEY", "")
S3_SECRET            = os.getenv("S3_SECRET", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "https://supabase-api.swipego.app").rstrip("/")
SUPABASE_KEY         = os.getenv("SUPABASE_KEY", "")
API_PWD              = os.getenv("API_PWD", "Laurytal2!")

# ------------------------------------------------------------------
# Clients
# ------------------------------------------------------------------
_drive = None
_s3 = None


def drive_client():
    global _drive
    if _drive is not None:
        return _drive
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SA_KEY env var missing")
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive


def s3_client():
    global _s3
    if _s3 is not None:
        return _s3
    _s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name=S3_REGION,
    )
    return _s3


# ------------------------------------------------------------------
# Auth helper
# ------------------------------------------------------------------
def auth_ok():
    pwd = request.args.get("pwd") or request.headers.get("X-Admin-Password") or ""
    if request.is_json:
        try:
            j = request.get_json(silent=True) or {}
            pwd = pwd or j.get("pwd", "")
        except Exception:
            pass
    return pwd == API_PWD


# ------------------------------------------------------------------
# Jobs storage (in-memory, sufficient for low-traffic use case)
# ------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "photobooth-sync"})


@app.route("/api/folders/all")
def list_all_folders():
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not DRIVE_PARENT_FOLDER:
        return jsonify({"error": "DRIVE_PARENT_FOLDER not configured"}), 500
    try:
        res = drive_client().files().list(
            q=f"'{DRIVE_PARENT_FOLDER}' in parents and trashed=false "
              f"and mimeType='application/vnd.google-apps.folder'",
            fields="files(id,name,createdTime)",
            pageSize=500,
            orderBy="createdTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return jsonify({"folders": res.get("files", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/folders/search")
def search_folder():
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    num_id = (request.args.get("num_id") or "").strip()
    if not num_id:
        return jsonify({"error": "num_id required"}), 400
    try:
        res = drive_client().files().list(
            q=f"'{DRIVE_PARENT_FOLDER}' in parents and trashed=false "
              f"and mimeType='application/vnd.google-apps.folder' "
              f"and name contains '{num_id}'",
            fields="files(id,name,createdTime)",
            pageSize=20,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return jsonify({"folders": res.get("files", [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/folders/list-files")
def list_files_in_folder():
    """Permet de previsualiser le contenu d'un dossier avant import."""
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    folder_id = (request.args.get("folder_id") or "").strip()
    if not folder_id:
        return jsonify({"error": "folder_id required"}), 400
    try:
        files, page_token = [], None
        while True:
            res = drive_client().files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,size)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        # Stats
        images = [f for f in files if f["mimeType"].startswith("image/")]
        videos = [f for f in files if f["mimeType"].startswith("video/")]
        return jsonify({
            "total": len(files),
            "images": len(images),
            "videos": len(videos),
            "files": files,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/import", methods=["POST"])
def start_import():
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    event_code = (data.get("event_code") or "").strip()
    folder_id  = (data.get("folder_id")  or "").strip()
    if not event_code or not folder_id:
        return jsonify({"error": "event_code and folder_id required"}), 400

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id":     job_id,
            "status":     "queued",
            "event_code": event_code,
            "folder_id":  folder_id,
            "total":      0,
            "imported":   0,
            "skipped":    0,
            "errors":     [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "current":    None,
        }
    threading.Thread(target=run_import, args=(job_id,), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/import/<job_id>")
def get_job(job_id):
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "job not found"}), 404
    return jsonify(j)


@app.route("/api/import/list")
def list_jobs():
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    with JOBS_LOCK:
        items = list(JOBS.values())
    items.sort(key=lambda j: j["started_at"], reverse=True)
    return jsonify({"jobs": items[:50]})


# ------------------------------------------------------------------
# Worker
# ------------------------------------------------------------------
def run_import(job_id):
    with JOBS_LOCK:
        j = JOBS[job_id]
    j["status"] = "running"
    event_code = j["event_code"]
    folder_id  = j["folder_id"]

    try:
        d = drive_client()
        s3 = s3_client()

        # 1) Lister tous les fichiers du dossier Drive
        files, page_token = [], None
        while True:
            res = d.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,size,createdTime)",
                pageSize=1000,
                pageToken=page_token,
                orderBy="createdTime",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        j["total"] = len(files)

        # 2) Recuperer ce qui est deja importe (par drive_id dans l'URL)
        existing_ids = set()
        try:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/appshoot_photos",
                params={
                    "event_code": f"eq.{event_code}",
                    "photo_type": "in.(photobooth,video)",
                    "select":     "photo_url",
                },
                headers=_supa_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                for row in resp.json():
                    url = row.get("photo_url") or ""
                    if "/photobooth/" in url:
                        name = url.rsplit("/", 1)[-1]
                        # extension à virer + suffixe _thumb potentiel
                        drive_id = name.split(".")[0].replace("_thumb", "")
                        if drive_id:
                            existing_ids.add(drive_id)
        except Exception as e:
            j["errors"].append(f"existing check: {e}")

        # 3) Boucle d'import
        for f in files:
            j["current"] = f.get("name")
            drive_id = f["id"]
            try:
                if drive_id in existing_ids:
                    j["skipped"] += 1
                    continue

                mime = f["mimeType"]
                is_image = mime.startswith("image/")
                is_video = mime.startswith("video/")
                if not (is_image or is_video):
                    j["skipped"] += 1
                    continue

                # Download
                req = d.files().get_media(fileId=drive_id, supportsAllDrives=True)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, req, chunksize=1024 * 1024 * 4)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                data = buf.getvalue()

                # Extension
                ext_map = {
                    "image/jpeg":       "jpg",
                    "image/png":        "png",
                    "image/gif":        "gif",
                    "image/webp":       "webp",
                    "video/mp4":        "mp4",
                    "video/quicktime":  "mov",
                    "video/mov":        "mov",
                }
                ext = ext_map.get(mime, "bin")

                # Compress JPEG si > 2 MB
                if mime == "image/jpeg" and len(data) > 2_000_000:
                    try:
                        img = Image.open(io.BytesIO(data))
                        img.thumbnail((2048, 2048), Image.LANCZOS)
                        out = io.BytesIO()
                        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
                        data = out.getvalue()
                    except Exception:
                        pass  # on garde l'original

                # Thumbnail JPEG 400px (pour images non-animees uniquement)
                thumb_url = None
                if is_image and mime != "image/gif":
                    try:
                        img = Image.open(io.BytesIO(data))
                        img.thumbnail((400, 400), Image.LANCZOS)
                        out = io.BytesIO()
                        img.convert("RGB").save(out, "JPEG", quality=80, optimize=True)
                        thumb_bytes = out.getvalue()
                        thumb_key = f"Myshootnbox/{event_code}/photobooth/{drive_id}_thumb.jpg"
                        s3.put_object(
                            Bucket=S3_BUCKET,
                            Key=thumb_key,
                            Body=thumb_bytes,
                            ContentType="image/jpeg",
                            ACL="public-read",
                        )
                        thumb_url = f"https://{S3_PUBLIC_HOST}/{thumb_key}"
                    except Exception as e:
                        j["errors"].append(f"thumb {drive_id}: {e}")

                # Upload main
                key = f"Myshootnbox/{event_code}/photobooth/{drive_id}.{ext}"
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=data,
                    ContentType=mime,
                    ACL="public-read",
                )
                photo_url = f"https://{S3_PUBLIC_HOST}/{key}"

                # Insert Supabase
                photo_type = "video" if is_video else "photobooth"
                payload = {
                    "event_code":   event_code,
                    "guest_pseudo": "Photobooth",
                    "avatar_color": 0,
                    "photo_url":    photo_url,
                    "photo_type":   photo_type,
                    "thumbnail_url": thumb_url,
                    "created_at":   f.get("createdTime"),
                }
                r = requests.post(
                    f"{SUPABASE_URL}/rest/v1/appshoot_photos",
                    json=payload,
                    headers={**_supa_headers(),
                             "Content-Type": "application/json",
                             "Prefer": "return=minimal"},
                    timeout=30,
                )
                if r.status_code in (200, 201):
                    j["imported"] += 1
                else:
                    j["errors"].append(f"{f['name']}: supabase {r.status_code} {r.text[:120]}")

            except Exception as e:
                j["errors"].append(f"{f.get('name','?')}: {str(e)[:200]}")

        j["status"] = "completed"
    except Exception as e:
        j["status"] = "failed"
        j["errors"].append(f"FATAL: {str(e)[:500]}")
    finally:
        j["current"] = None
        j["finished_at"] = datetime.now(timezone.utc).isoformat()


def _supa_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
