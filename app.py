"""
Photobooth Sync — Service Python Flask
Importe les photos d'un dossier Google Drive vers OVH S3
et insere les lignes dans Supabase appshoot_photos.
"""

import os
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

# Regex pour matcher un vrai numero de commande (FA + 4-6 chiffres)
FA_REGEX = re.compile(r"FA\d{4,6}", re.IGNORECASE)

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
        # Filtrer : ne garder que les dossiers qui contiennent un vrai num_id FA12345 (anciens clients ignores)
        all_folders = res.get("files", [])
        filtered = [f for f in all_folders if FA_REGEX.search(f.get("name") or "")]
        return jsonify({"folders": filtered, "ignored_count": len(all_folders) - len(filtered)})
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

        # 2) Recuperer ce qui est deja importe + savoir si thumbnail manquant
        # existing_map[drive_id] = {"id": uuid, "photo_url": ..., "thumbnail_url": ..., "photo_type": ...}
        existing_map = {}
        try:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/appshoot_photos",
                params={
                    "event_code": f"eq.{event_code}",
                    "photo_type": "in.(photobooth,video)",
                    "select":     "id,photo_url,thumbnail_url,photo_type",
                },
                headers=_supa_headers(),
                timeout=30,
            )
            if resp.status_code == 200:
                for row in resp.json():
                    url = row.get("photo_url") or ""
                    if "/photobooth/" in url:
                        name = url.rsplit("/", 1)[-1]
                        drive_id = name.split(".")[0].replace("_thumb", "")
                        if drive_id:
                            existing_map[drive_id] = row
        except Exception as e:
            j["errors"].append(f"existing check: {e}")

        # 3) Boucle d'import
        for f in files:
            j["current"] = f.get("name")
            drive_id = f["id"]
            try:
                mime = f["mimeType"]
                is_image = mime.startswith("image/")
                is_video = mime.startswith("video/")
                if not (is_image or is_video):
                    j["skipped"] += 1
                    continue

                # Deja importe : on regen le thumb video si manquant, sinon skip
                if drive_id in existing_map:
                    row = existing_map[drive_id]
                    if is_video and not row.get("thumbnail_url"):
                        try:
                            req = d.files().get_media(fileId=drive_id, supportsAllDrives=True)
                            buf = io.BytesIO()
                            downloader = MediaIoBaseDownload(buf, req, chunksize=1024 * 1024 * 4)
                            done = False
                            while not done:
                                _, done = downloader.next_chunk()
                            video_data = buf.getvalue()
                            thumb_url = _generate_video_thumb(video_data, event_code, drive_id, s3)
                            if thumb_url:
                                requests.patch(
                                    f"{SUPABASE_URL}/rest/v1/appshoot_photos",
                                    params={"id": f"eq.{row['id']}"},
                                    json={"thumbnail_url": thumb_url},
                                    headers={**_supa_headers(), "Content-Type": "application/json"},
                                    timeout=30,
                                )
                                j["imported"] += 1
                                continue
                        except Exception as e:
                            j["errors"].append(f"thumb regen {f['name']}: {str(e)[:200]}")
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

                # Thumbnail JPEG 400px
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
                elif is_video:
                    try:
                        thumb_url = _generate_video_thumb(data, event_code, drive_id, s3)
                    except Exception as e:
                        j["errors"].append(f"video thumb {drive_id}: {e}")

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


def _generate_video_thumb(video_bytes: bytes, event_code: str, drive_id: str, s3) -> str | None:
    """Extrait le 1er frame d'une video via ffmpeg, l'upload sur S3 et retourne l'URL."""
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, f"in_{drive_id}.bin")
        out_path = os.path.join(td, f"thumb_{drive_id}.jpg")
        with open(in_path, "wb") as f:
            f.write(video_bytes)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", in_path,
                    "-vf", "thumbnail,scale=800:-1",
                    "-frames:v", "1",
                    "-q:v", "5",
                    out_path,
                ],
                check=True, capture_output=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        if not os.path.exists(out_path):
            return None
        with open(out_path, "rb") as f:
            thumb_bytes = f.read()
    thumb_key = f"Myshootnbox/{event_code}/photobooth/{drive_id}_thumb.jpg"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=thumb_key,
        Body=thumb_bytes,
        ContentType="image/jpeg",
        ACL="public-read",
    )
    return f"https://{S3_PUBLIC_HOST}/{thumb_key}"


def _supa_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


# ------------------------------------------------------------------
# DRIVE SCAN — boucle de fond qui scanne Drive toutes les 10 min
# et met a jour drive_photo_count + drive_count_stable_since dans Supabase
# pour gater les boutons "Importer" / "Envoyer" dans admin-app
# ------------------------------------------------------------------
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "600"))  # 10 min default
SCAN_STATS = {
    "last_run_at":     None,
    "last_run_status": "never_run",
    "events_scanned":  0,
    "events_changed":  0,
    "events_stable":   0,
    "events_no_folder": 0,
    "errors":          [],
    "duration_sec":    0,
}
SCAN_LOCK = threading.Lock()


def _count_drive_files(folder_id):
    """Compte les fichiers media (image/video) dans un dossier Drive."""
    d = drive_client()
    count = 0
    page_token = None
    while True:
        res = d.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,mimeType)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in res.get("files", []):
            m = f.get("mimeType", "")
            if m.startswith("image/") or m.startswith("video/"):
                count += 1
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return count


def _find_drive_folder_by_num_id(num_id):
    """Cherche le dossier Drive contenant num_id dans son nom. Retourne le plus recent ou None."""
    if not num_id or not DRIVE_PARENT_FOLDER:
        return None
    try:
        res = drive_client().files().list(
            q=f"'{DRIVE_PARENT_FOLDER}' in parents and trashed=false "
              f"and mimeType='application/vnd.google-apps.folder' "
              f"and name contains '{num_id}'",
            fields="files(id,name,createdTime)",
            pageSize=10,
            orderBy="createdTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = res.get("files", [])
        if not folders:
            return None
        # Filtre strict : le nom doit contenir le num_id complet (pas un prefix qui matche par hasard)
        exact = [f for f in folders if num_id.upper() in (f.get("name") or "").upper()]
        return exact[0] if exact else None
    except Exception:
        return None


def scan_drive_for_all_events():
    """Scan Drive pour tous les events Supabase concernes et update les colonnes.

    Concerne uniquement les events ou review_status est 'idle' ou 'pending'
    (= pas encore "Envoye"). Les events deja envoyes ne sont plus scannes.
    """
    started = time.time()
    stats = {
        "events_scanned": 0,
        "events_changed": 0,
        "events_stable":  0,
        "events_no_folder": 0,
        "errors":         [],
    }
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/appshoot_events",
            params={
                "select":        "event_code,num_id,drive_photo_count,drive_count_stable_since",
                "review_status": "in.(idle,pending)",
                "order":         "event_date.desc",
            },
            headers=_supa_headers(),
            timeout=30,
        )
        if r.status_code != 200:
            stats["errors"].append(f"supabase fetch events: HTTP {r.status_code}")
            return _save_scan_stats(stats, started, "fetch_failed")
        events = r.json()

        for e in events:
            num_id     = (e.get("num_id") or "").strip()
            event_code = (e.get("event_code") or "").strip()
            if not num_id or not event_code:
                continue
            stats["events_scanned"] += 1

            try:
                folder = _find_drive_folder_by_num_id(num_id)
                if folder is None:
                    new_count = 0
                else:
                    new_count = _count_drive_files(folder["id"])
            except Exception as ex:
                stats["errors"].append(f"{num_id}: scan {str(ex)[:120]}")
                continue

            old_count = e.get("drive_photo_count") or 0
            old_stable = e.get("drive_count_stable_since")

            # Cas 1 : aucune photo en Drive
            if new_count == 0:
                if old_count != 0 or old_stable is not None:
                    _update_event_drive_state(event_code, 0, None)
                stats["events_no_folder"] += 1
                continue

            # Cas 2 : count change -> reset stable_since
            if new_count != old_count:
                _update_event_drive_state(event_code, new_count, datetime.now(timezone.utc).isoformat())
                stats["events_changed"] += 1
                continue

            # Cas 3 : count identique
            # Si stable_since etait null (= 1er scan stable), on l'initialise maintenant.
            # Sinon on touche a rien (la stabilite s'accumule au fil des scans).
            if old_stable is None:
                _update_event_drive_state(event_code, new_count, datetime.now(timezone.utc).isoformat())
                stats["events_changed"] += 1
            else:
                stats["events_stable"] += 1

        return _save_scan_stats(stats, started, "ok")

    except Exception as ex:
        stats["errors"].append(f"FATAL: {str(ex)[:300]}")
        return _save_scan_stats(stats, started, "fatal_error")


def _update_event_drive_state(event_code, count, stable_since):
    payload = {"drive_photo_count": count, "drive_count_stable_since": stable_since}
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/appshoot_events",
            params={"event_code": f"eq.{event_code}"},
            json=payload,
            headers={**_supa_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=15,
        )
    except Exception:
        pass  # log silencieux, le prochain scan re-tentera


def _save_scan_stats(stats, started, status):
    duration = round(time.time() - started, 2)
    with SCAN_LOCK:
        SCAN_STATS["last_run_at"]      = datetime.now(timezone.utc).isoformat()
        SCAN_STATS["last_run_status"]  = status
        SCAN_STATS["events_scanned"]   = stats["events_scanned"]
        SCAN_STATS["events_changed"]   = stats["events_changed"]
        SCAN_STATS["events_stable"]    = stats["events_stable"]
        SCAN_STATS["events_no_folder"] = stats["events_no_folder"]
        SCAN_STATS["errors"]           = stats["errors"][-20:]
        SCAN_STATS["duration_sec"]     = duration
    return SCAN_STATS


def scan_loop():
    """Thread daemon qui scanne Drive toutes les SCAN_INTERVAL_SEC."""
    # Delai au boot pour laisser Flask demarrer
    time.sleep(30)
    while True:
        try:
            scan_drive_for_all_events()
        except Exception:
            pass
        time.sleep(SCAN_INTERVAL_SEC)


@app.route("/api/drive_scan", methods=["GET", "POST"])
def manual_drive_scan():
    """Declenche un scan manuel (debug)."""
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    s = scan_drive_for_all_events()
    return jsonify(s)


@app.route("/api/drive_scan/stats")
def drive_scan_stats():
    """Renvoie les stats du dernier scan automatique."""
    if not auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    with SCAN_LOCK:
        return jsonify(dict(SCAN_STATS))


# Demarre le thread de scan au load du module (= au boot gunicorn)
_scan_thread = threading.Thread(target=scan_loop, daemon=True, name="drive-scan-loop")
_scan_thread.start()


# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
