# Photobooth Sync

Service Python Flask qui importe les photos d'un dossier Google Drive vers OVH S3
et insère les lignes dans Supabase `appshoot_photos` (rubrique "photobooth" de l'app MyShootnbox).

## Variables d'environnement requises

| Variable | Description |
|---|---|
| `GOOGLE_SA_KEY` | JSON complet de la clé du Service Account Google Drive |
| `DRIVE_PARENT_FOLDER` | ID du dossier Drive parent qui contient les sous-dossiers événements |
| `S3_ENDPOINT` | `https://s3.sbg.io.cloud.ovh.net` |
| `S3_BUCKET` | `app-media-shootnbox` |
| `S3_PUBLIC_HOST` | `app-media-shootnbox.s3.sbg.io.cloud.ovh.net` |
| `S3_REGION` | `sbg` |
| `S3_KEY` | Access key OVH S3 |
| `S3_SECRET` | Secret key OVH S3 |
| `SUPABASE_URL` | `https://supabase-api.swipego.app` |
| `SUPABASE_KEY` | Service role key Supabase |
| `API_PWD` | Mot de passe pour protéger les endpoints |

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/folders/all?pwd=X` | Liste tous les sous-dossiers du dossier parent |
| GET | `/api/folders/search?num_id=FA14130&pwd=X` | Cherche un sous-dossier par num_id |
| GET | `/api/folders/list-files?folder_id=X&pwd=Y` | Compte les fichiers d'un sous-dossier |
| POST | `/api/import` | Lance un import. Body: `{event_code, folder_id, pwd}` |
| GET | `/api/import/<job_id>?pwd=X` | Statut d'un import en cours |
| GET | `/api/import/list?pwd=X` | Liste les 50 derniers jobs |

## Workflow

1. L'admin choisit un événement et le dossier Drive correspondant
2. POST `/api/import` retourne un `job_id`
3. L'admin poll `/api/import/<job_id>` toutes les 2s pour suivre la progression
4. Le worker télécharge chaque fichier, compresse, génère un thumbnail, upload S3, insère Supabase
5. Les photos apparaissent dans l'onglet "Photobooth" de la galerie Flutter

## Déploiement

Via Coolify sur serveur 217, source GitHub `AmazingeventParis/photobooth-sync`.
