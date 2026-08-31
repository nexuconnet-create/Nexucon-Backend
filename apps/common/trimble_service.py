import csv
import json
import logging
import os
from io import StringIO
from django.conf import settings

logger = logging.getLogger(__name__)

# Trimble Identity endpoints
TRIMBLE_AUTH_URL = "https://id.trimble.com/oauth/authorize"
TRIMBLE_TOKEN_URL = "https://id.trimble.com/oauth/token"

# Trimble Connect API base
TRIMBLE_API_BASE = "https://app.connect.trimble.com/tc/api/2.0"

# In-memory token cache
_token_cache = {
    "access_token": None,
    "refresh_token": None,
}


class TrimbleConnectService:
    """
    Service layer handling integration with Trimble Connect Core APIs for BIM data synchronization.
    Uses OAuth2 Authorization Code flow:
      1. One-time: user authorizes via browser → we get a refresh_token
      2. Ongoing: we use the refresh_token to get fresh access_tokens automatically
    """

    @staticmethod
    def _get_credentials():
        return {
            "client_id": getattr(settings, 'TRIMBLE_CLIENT_ID', os.environ.get('TRIMBLE_CLIENT_ID', '')),
            "client_secret": getattr(settings, 'TRIMBLE_CLIENT_SECRET', os.environ.get('TRIMBLE_CLIENT_SECRET', '')),
            "project_id": getattr(settings, 'TRIMBLE_PROJECT_ID', os.environ.get('TRIMBLE_PROJECT_ID', '')),
            "folder_id": getattr(settings, 'TRIMBLE_FOLDER_ID', os.environ.get('TRIMBLE_FOLDER_ID', 'YxeHTR0XTrA')),
            "redirect_uri": getattr(settings, 'TRIMBLE_REDIRECT_URI', 'http://localhost:8000/api/v1/scans/trimble-callback/'),
        }

    @classmethod
    def get_authorization_url(cls) -> str:
        """
        Returns the URL the user must visit in their browser to authorize the app.
        After login, Trimble redirects to our callback with an authorization code.
        """
        creds = cls._get_credentials()
        redirect_uri = creds["redirect_uri"]
        url = (
            f"{TRIMBLE_AUTH_URL}"
            f"?response_type=code"
            f"&client_id={creds['client_id']}"
            f"&redirect_uri={redirect_uri}"
            f"&scope=openid"
        )
        return url

    @classmethod
    def exchange_code_for_tokens(cls, authorization_code: str) -> bool:
        """
        Exchanges the one-time authorization code for access + refresh tokens.
        Called by the OAuth callback endpoint.
        """
        import requests
        creds = cls._get_credentials()
        
        try:
            resp = requests.post(
                TRIMBLE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "redirect_uri": creds["redirect_uri"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            
            _token_cache["access_token"] = data.get("access_token")
            _token_cache["refresh_token"] = data.get("refresh_token")
            
            # Persist refresh token to .env-safe file so it survives restarts
            token_file = os.path.join(str(settings.BASE_DIR), ".trimble_refresh_token")
            with open(token_file, "w") as f:
                f.write(data.get("refresh_token", ""))
            
            logger.info("✅ Successfully obtained Trimble Connect tokens via Authorization Code flow.")
            return True
        except Exception as e:
            logger.error(f"Failed to exchange Trimble auth code: {e}", exc_info=True)
            return False

    @classmethod
    def _get_access_token(cls) -> str:
        """
        Returns a valid access token. Uses cached token if available,
        otherwise refreshes using the stored refresh token.
        """
        import requests
        
        # Check in-memory cache first
        if _token_cache.get("access_token"):
            return _token_cache["access_token"]

        # Try to load refresh token from disk
        refresh_token = _token_cache.get("refresh_token")
        if not refresh_token:
            token_file = os.path.join(str(settings.BASE_DIR), ".trimble_refresh_token")
            if os.path.exists(token_file):
                with open(token_file) as f:
                    refresh_token = f.read().strip()
                _token_cache["refresh_token"] = refresh_token

        if not refresh_token:
            creds = cls._get_credentials()
            if not creds["client_id"]:
                logger.warning("Trimble credentials not configured — running in simulation mode.")
            else:
                logger.warning(
                    "No Trimble refresh token found. Please authorize first by visiting: "
                    f"{cls.get_authorization_url()}"
                )
            return ""

        # Use refresh token to get a new access token
        creds = cls._get_credentials()
        try:
            resp = requests.post(
                TRIMBLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            
            _token_cache["access_token"] = data.get("access_token")
            # Update refresh token if a new one was issued
            if data.get("refresh_token"):
                _token_cache["refresh_token"] = data["refresh_token"]
                token_file = os.path.join(str(settings.BASE_DIR), ".trimble_refresh_token")
                with open(token_file, "w") as f:
                    f.write(data["refresh_token"])
            
            logger.info("Successfully refreshed Trimble Connect access token.")
            return _token_cache["access_token"]
        except Exception as e:
            logger.error(f"Failed to refresh Trimble token: {e}", exc_info=True)
            # Clear stale token
            _token_cache["access_token"] = None
            return ""

    @classmethod
    def generate_defect_csv(cls, session) -> str:
        """
        Generates a strictly structured CSV of all Defects and Thermal Anomalies
        for integration as BIM Shared Parameters in Trimble Connect.
        """
        from apps.scans.models import Defect, ThermalAnomaly

        output = StringIO()
        writer = csv.writer(output)

        # Header — Trimble Connect / Revit Shared Parameters format
        writer.writerow([
            'Damage_ID', 'Category', 'Severity', 'Location_X', 'Location_Y', 'Location_Z',
            'Description', 'Detected_At', 'Status'
        ])

        for d in Defect.objects.filter(session=session):
            writer.writerow([
                str(d.id),
                f"Visual_{d.type}",
                d.severity,
                d.location_x,
                d.location_y,
                d.location_z,
                d.description or f"AI-detected {d.type}",
                d.created_at.isoformat(),
                d.status
            ])

        for a in ThermalAnomaly.objects.filter(session=session):
            writer.writerow([
                str(a.id),
                "Thermal_Anomaly",
                a.severity,
                a.location_x,
                a.location_y,
                a.location_z,
                a.description or f"Thermal anomaly — variance {a.temperature_variance}°C",
                a.created_at.isoformat(),
                a.status
            ])

        return output.getvalue()

    @classmethod
    def generate_inspection_summary(cls, session) -> str:
        """
        Generates a JSON summary of the overall inspection condition and metrics.
        """
        from apps.scans.models import Defect, ThermalAnomaly, ProgressValidationResult

        defects = Defect.objects.filter(session=session)
        anomalies = ThermalAnomaly.objects.filter(session=session)
        progress = ProgressValidationResult.objects.filter(session=session).first()

        total_issues = defects.count() + anomalies.count()
        critical_issues = (
            defects.filter(severity='critical').count() +
            anomalies.filter(severity='critical').count()
        )

        if critical_issues > 0:
            condition_rating = "Critical"
        elif total_issues > 5:
            condition_rating = "Fair"
        elif total_issues > 0:
            condition_rating = "Good"
        else:
            condition_rating = "Excellent"

        summary = {
            "session_id": str(session.id),
            "project_id": str(session.project.id) if session.project else None,
            "overall_condition_rating": condition_rating,
            "total_defects_detected": defects.count(),
            "total_thermal_anomalies_detected": anomalies.count(),
            "critical_issues": critical_issues,
            "progress_score": progress.progress_score if progress else 0.0,
            "covered_area_sqm": progress.covered_area_sqm if progress else 0.0,
            "timestamp": session.created_at.isoformat(),
            "synced_by": "Nexucon SiteSupervise",
        }

        return json.dumps(summary, indent=4)

    @classmethod
    def generate_ai_overlay_json(cls, session) -> str:
        """
        GeoJSON FeatureCollection of confirmed AI detections for spatial overlay in Trimble Connect.
        """
        from apps.scans.models import Defect

        defects = Defect.objects.filter(session=session, is_false_positive=False)

        features = []
        for d in defects:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [d.location_x or 0.0, d.location_y or 0.0, d.location_z or 0.0]
                },
                "properties": {
                    "defect_id": str(d.id),
                    "type": d.type,
                    "severity": d.severity,
                    "description": d.description or f"AI-detected {d.type}",
                    "confidence_score": d.confidence_score,
                }
            })

        return json.dumps({"type": "FeatureCollection", "features": features}, indent=4)

    @classmethod
    def upload_files_to_trimble(
        cls,
        session,
        defect_csv: str,
        summary_json: str,
        ai_overlay_json: str = None,
        thermal_orthomosaic_url: str = None,
    ) -> bool:
        """
        Authenticates via OAuth2 then uploads all generated inspection files
        to the Trimble Connect project folder.
        """
        creds = cls._get_credentials()
        project_id = creds["project_id"]
        folder_id = creds["folder_id"]

        logger.info(f"Preparing Trimble Connect sync for session {session.id} → project {project_id}")

        # Step 1: Get OAuth2 access token
        access_token = cls._get_access_token()

        if not access_token:
            # Simulation mode — no real upload
            logger.info(
                f"[SIMULATION] Trimble sync skipped (no credentials). "
                f"Would have uploaded: defects.csv, inspection_summary.json, ai_overlay.json"
            )
            return True

        import requests

        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        # Trimble Connect Files API endpoint
        # PUT /projects/{projectId}/folders/{folderId}/files
        upload_url = f"{TRIMBLE_API_BASE}/projects/{project_id}/folders/{folder_id}/files"

        session_label = str(session.id)[:8]
        files_to_upload = [
            (f"nexucon_defects_{session_label}.csv", defect_csv, "text/csv"),
            (f"nexucon_summary_{session_label}.json", summary_json, "application/json"),
        ]
        if ai_overlay_json:
            files_to_upload.append((f"nexucon_ai_overlay_{session_label}.json", ai_overlay_json, "application/json"))
        if thermal_orthomosaic_url:
            meta = json.dumps({"thermal_url": thermal_orthomosaic_url, "session_id": str(session.id)})
            files_to_upload.append((f"nexucon_thermal_meta_{session_label}.json", meta, "application/json"))

        try:
            for filename, content, content_type in files_to_upload:
                resp = requests.post(
                    upload_url,
                    headers=headers,
                    files={"file": (filename, content.encode("utf-8") if isinstance(content, str) else content, content_type)},
                    timeout=30,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"✅ Uploaded '{filename}' to Trimble Connect project {project_id}")
                else:
                    logger.error(
                        f"❌ Failed to upload '{filename}' — HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                    return False

            logger.info(f"✅ All files synced to Trimble Connect project {project_id} / folder {folder_id}")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Trimble Connect network error: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Trimble Connect upload failed: {e}", exc_info=True)
            return False
