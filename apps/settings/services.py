import hashlib
import logging
import os
import secrets
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied

logger = logging.getLogger(__name__)

from .models import (
    TersusDevice, BIMIntegration, DocumentSystemIntegration,
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog,
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow,
    WorkflowStep, InspectionTemplate, ChecklistItem, ComplianceStandard,
    StatutoryDocument, NotificationRoutingRule, NotificationPreferenceCategory,
    WebhookSubscription, AgencyProfile, ReportTemplate
)
from apps.audit.models import AuditEvent

User = get_user_model()


def _http_probe(url, headers=None, timeout=10):
    """
    Perform a REAL network request and measure the actual latency.
    Returns (ok, http_status, latency_ms, error_detail). Never fabricates a
    result — a request that was not made is never reported as one.
    """
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=headers or {}, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency_ms = int((time.monotonic() - start) * 1000)
            return True, resp.status, latency_ms, ''
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return False, exc.code, latency_ms, f'HTTP {exc.code}'
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return False, None, latency_ms, str(exc)


def _log_pending_event(service_name, event_name, missing_env_vars, direction="Outbound"):
    """
    Record — honestly — that an integration action could not run because the
    required credentials are not configured. No network call is made and no
    success is implied.
    """
    IntegrationService.log_integration_event(
        service_name=service_name,
        event_name=event_name,
        status="Pending",
        payload_size=None,
        http_status_code=0,
        duration_ms=0,
        direction=direction,
        details=f"Credentials not configured ({', '.join(missing_env_vars)}). "
                "No network call was made."
    )

# ==========================================
# PROVIDER ABSTRACTION LAYER
# ==========================================

class BaseIntegrationProvider:
    """Base abstract provider interface for external integrations."""
    @classmethod
    def test_connection(cls, entity_id: str, user=None) -> dict:
        raise NotImplementedError

    @classmethod
    def health_check(cls, entity_id: str = None, user=None) -> dict:
        raise NotImplementedError

    @classmethod
    def sync(cls, entity_id: str, user=None) -> dict:
        raise NotImplementedError


class TersusProvider(BaseIntegrationProvider):
    """
    Tersus GNSS RTK base station, rover, point cloud & positioning telemetry provider.
    Reuses existing Tersus sensor models and Site Verification telemetry.
    """
    @classmethod
    def test_connection(cls, device_id: str, user=None) -> dict:
        """
        Real telemetry ping. Without Tersus API credentials the check reports
        PENDING_CREDENTIALS and makes no network call — latency and success
        status are never fabricated.
        """
        device = TersusDevice.objects.get(device_id=device_id) if isinstance(device_id, str) and not device_id.startswith('000') else TersusDevice.objects.get(id=device_id)

        api_url = os.getenv('TERSUS_API_URL', '')
        api_key = os.getenv('TERSUS_API_KEY', '')
        result = {
            "provider": "Tersus GNSS",
            "device_id": device.device_id,
            "response_time_ms": None,
            "satellites": device.satellites_tracked,
            "fix_status": device.rtk_fix_status,
            "checked_at": timezone.now().isoformat()
        }

        if not api_url or not api_key:
            _log_pending_event(
                "Tersus GNSS",
                f"GNSS Telemetry Ping: {device.name}",
                ['TERSUS_API_URL', 'TERSUS_API_KEY'],
            )
            result["status"] = "PENDING_CREDENTIALS"
            return result

        ok, http_status, latency_ms, error_detail = _http_probe(api_url, headers={'X-API-Key': api_key})

        IntegrationService.log_integration_event(
            service_name="Tersus GNSS",
            event_name=f"GNSS Receiver Telemetry Ping: {device.name}",
            status="Success" if ok else "Failed",
            payload_size=None,
            http_status_code=http_status or 0,
            duration_ms=latency_ms,
            direction="Inbound",
            details=(
                f"Live probe of {api_url} returned HTTP {http_status} in {latency_ms} ms. "
                f"Last recorded telemetry: {device.satellites_tracked} satellites, RTK fix {device.rtk_fix_status}."
                if ok else f"Probe of {api_url} failed after {latency_ms} ms: {error_detail}."
            )
        )

        if ok:
            device.status = 'Active'
            device.last_sync = timezone.now()
            device.save()
            IntegrationService.log_audit(
                user=user,
                action="INTEGRATION_TESTED",
                resource_id=device.id,
                new_state={"provider": "Tersus GNSS", "device_id": device.device_id, "status": "Active", "latency_ms": latency_ms}
            )
            result["status"] = "HEALTHY"
        else:
            result["status"] = "UNREACHABLE"
        result["response_time_ms"] = latency_ms
        return result

    @classmethod
    def sync(cls, device_id: str, user=None):
        """
        Forced sync. Telemetry rows are written by the device's own pushes (and
        by the real API pull below when credentials exist) — nothing is counted
        or claimed as synchronized without a real call.
        """
        device = TersusDevice.objects.get(device_id=device_id) if isinstance(device_id, str) and not device_id.startswith('000') else TersusDevice.objects.get(id=device_id)

        api_url = os.getenv('TERSUS_API_URL', '')
        api_key = os.getenv('TERSUS_API_KEY', '')
        if not api_url or not api_key:
            _log_pending_event(
                "Tersus GNSS",
                f"Telemetry sync requested for {device.name}",
                ['TERSUS_API_URL', 'TERSUS_API_KEY'],
            )
            return device

        ok, http_status, latency_ms, error_detail = _http_probe(api_url, headers={'X-API-Key': api_key})
        if ok:
            device.last_sync = timezone.now()
            device.save()
        IntegrationService.log_integration_event(
            service_name="Tersus GNSS",
            event_name=f"Telemetry sync for {device.name}",
            status="Success" if ok else "Failed",
            payload_size=None,
            http_status_code=http_status or 0,
            duration_ms=latency_ms,
            direction="Inbound",
            details=(
                f"Live probe of {api_url} returned HTTP {http_status} in {latency_ms} ms."
                if ok else f"Probe of {api_url} failed: {error_detail}."
            )
        )
        return device


class BIMProvider(BaseIntegrationProvider):
    """
    BIM and 3D Model Review provider supporting Trimble Connect (default), Autodesk, Procore, and Bentley.
    """
    @classmethod
    def _platform_credentials(cls, provider_name: str) -> dict:
        """Real configured credentials for the platform, from the environment only."""
        name = (provider_name or '').lower()
        if 'trimble' in name:
            values = {
                'TRIMBLE_CLIENT_ID': os.getenv('TRIMBLE_CLIENT_ID', ''),
                'TRIMBLE_CLIENT_SECRET': os.getenv('TRIMBLE_CLIENT_SECRET', ''),
                'TRIMBLE_API_BASE': os.getenv('TRIMBLE_API_BASE', ''),
            }
            missing = [k for k, v in values.items() if not v]
            return {"configured": not missing, "api_base": values['TRIMBLE_API_BASE'], "missing": missing}
        # Autodesk / Procore / Bentley connectors have no credentials wired yet.
        return {"configured": False, "api_base": '', "missing": [f"{provider_name} API credentials (not yet wired)"]}

    @classmethod
    def test_connection(cls, bim_id: str, user=None) -> dict:
        bim = BIMIntegration.objects.get(id=bim_id)
        creds = cls._platform_credentials(bim.provider)
        result = {
            "provider": bim.provider,
            "response_time_ms": None,
            "synced_models": bim.synced_models_count,
            "checked_at": timezone.now().isoformat()
        }

        if not creds["configured"]:
            _log_pending_event(
                bim.provider,
                f"Health check: {bim.provider}",
                creds["missing"],
            )
            result["status"] = "PENDING_CREDENTIALS"
            return result

        ok, http_status, latency_ms, error_detail = _http_probe(creds["api_base"])

        IntegrationService.log_integration_event(
            service_name=bim.provider,
            event_name=f"Health check: {bim.provider}",
            status="Success" if ok else "Failed",
            payload_size=None,
            http_status_code=http_status or 0,
            duration_ms=latency_ms,
            direction="Inbound",
            details=(
                f"Live probe of {creds['api_base']} returned HTTP {http_status} in {latency_ms} ms. "
                "Per-connection OAuth authorization is handled by the Trimble Connect integration."
                if ok else f"Probe of {creds['api_base']} failed after {latency_ms} ms: {error_detail}."
            )
        )

        if ok:
            bim.status = 'Connected'
            bim.last_successful_sync = timezone.now()
            bim.save()
            IntegrationService.log_audit(
                user=user,
                action="INTEGRATION_TESTED",
                resource_id=bim.id,
                new_state={"provider": bim.provider, "status": "Connected", "latency_ms": latency_ms}
            )
            result["status"] = "HEALTHY"
        else:
            result["status"] = "UNREACHABLE"
        result["response_time_ms"] = latency_ms
        return result

    @classmethod
    def sync(cls, bim_id: str, user=None):
        """
        Dispatch the REAL Celery Trimble sync task (digital_eye.trimble_sync) —
        the task itself records what actually synced. Model counts are only
        ever updated from real ingestion, never incremented here.
        """
        bim = BIMIntegration.objects.get(id=bim_id)
        creds = cls._platform_credentials(bim.provider)

        if not creds["configured"]:
            _log_pending_event(
                bim.provider,
                f"Model sync requested for {bim.provider}",
                creds["missing"],
            )
            return bim

        from apps.digital_eye.tasks import trimble_sync
        trimble_sync.delay()
        IntegrationService.log_integration_event(
            service_name=bim.provider,
            event_name=f"Model sync dispatched for {bim.provider}",
            status="Success",
            payload_size=None,
            http_status_code=0,
            duration_ms=0,
            direction="Outbound",
            details="Trimble Connect sync task queued; the task logs the actual ingestion results."
        )
        IntegrationService.log_audit(
            user=user,
            action="INTEGRATION_SYNC_DISPATCHED",
            resource_id=bim.id,
            new_state={"provider": bim.provider}
        )
        return bim


class DocumentProvider(BaseIntegrationProvider):
    """
    Document and media storage connectors (Cloudflare R2, Cloudinary).
    """
    @classmethod
    def _probe_storage(cls, dms):
        """
        Perform a REAL reachability check appropriate to the provider.
        Returns (state, latency_ms, detail) where state is 'HEALTHY',
        'UNREACHABLE', or 'PENDING_CREDENTIALS'.
        """
        provider = f"{dms.storage_provider} {dms.name} {dms.endpoint_url}".lower()
        start = time.monotonic()

        if 'cloudflare' in provider or 'r2' in provider:
            access_key = os.getenv('CLOUDFLARE_R2_ACCESS_KEY_ID', '')
            secret_key = os.getenv('CLOUDFLARE_R2_SECRET_ACCESS_KEY', '')
            endpoint = os.getenv('CLOUDFLARE_R2_ENDPOINT_URL', '')
            bucket = os.getenv('CLOUDFLARE_R2_BUCKET_NAME', '')
            missing = [k for k, v in (
                ('CLOUDFLARE_R2_ACCESS_KEY_ID', access_key),
                ('CLOUDFLARE_R2_SECRET_ACCESS_KEY', secret_key),
                ('CLOUDFLARE_R2_ENDPOINT_URL', endpoint),
                ('CLOUDFLARE_R2_BUCKET_NAME', bucket)) if not v]
            if missing:
                return 'PENDING_CREDENTIALS', None, (
                    f"Credentials not configured ({', '.join(missing)}). No network call was made.")

            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
            try:
                client = boto3.client(
                    's3', endpoint_url=endpoint, region_name='auto',
                    aws_access_key_id=access_key, aws_secret_access_key=secret_key)
                client.head_bucket(Bucket=bucket)
                return 'HEALTHY', int((time.monotonic() - start) * 1000), (
                    f"Real head_bucket on '{bucket}' at {endpoint} succeeded.")
            except (ClientError, BotoCoreError) as exc:
                return 'UNREACHABLE', int((time.monotonic() - start) * 1000), str(exc)

        if 'cloudinary' in provider:
            cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME', '')
            api_key = os.getenv('CLOUDINARY_API_KEY', '')
            api_secret = os.getenv('CLOUDINARY_API_SECRET', '')
            missing = [k for k, v in (
                ('CLOUDINARY_CLOUD_NAME', cloud_name),
                ('CLOUDINARY_API_KEY', api_key),
                ('CLOUDINARY_API_SECRET', api_secret)) if not v]
            if missing:
                return 'PENDING_CREDENTIALS', None, (
                    f"Credentials not configured ({', '.join(missing)}). No network call was made.")

            import cloudinary
            from cloudinary.api import ping
            cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret)
            try:
                ping()
                return 'HEALTHY', int((time.monotonic() - start) * 1000), (
                    f"Real Cloudinary ping succeeded for cloud '{cloud_name}'.")
            except Exception as exc:
                return 'UNREACHABLE', int((time.monotonic() - start) * 1000), str(exc)

        if dms.endpoint_url:
            ok, http_status, latency_ms, error_detail = _http_probe(dms.endpoint_url)
            if ok:
                return 'HEALTHY', latency_ms, f"HTTP {http_status} from {dms.endpoint_url}."
            return 'UNREACHABLE', latency_ms, error_detail

        return 'PENDING_CREDENTIALS', None, (
            f"No credentials or endpoint configured for {dms.storage_provider}. No network call was made.")

    @classmethod
    def test_connection(cls, dms_id: str, user=None) -> dict:
        dms = DocumentSystemIntegration.objects.get(id=dms_id)
        state, latency_ms, detail = cls._probe_storage(dms)

        IntegrationService.log_integration_event(
            service_name=dms.name,
            event_name=f"Storage Handshake: {dms.name}",
            status={'HEALTHY': 'Success', 'UNREACHABLE': 'Failed'}.get(state, 'Pending'),
            payload_size=None,
            http_status_code=200 if state == 'HEALTHY' else 0,
            duration_ms=latency_ms or 0,
            direction="Inbound",
            details=detail
        )

        if state == 'HEALTHY':
            dms.status = 'Active'
            dms.save()
            IntegrationService.log_audit(
                user=user,
                action="INTEGRATION_TESTED",
                resource_id=dms.id,
                new_state={"provider": dms.name, "bucket": dms.bucket_or_drive_name, "latency_ms": latency_ms}
            )

        return {
            "status": state,
            "provider": dms.name,
            "bucket": dms.bucket_or_drive_name,
            "response_time_ms": latency_ms,
            "checked_at": timezone.now().isoformat()
        }

    @classmethod
    def sync(cls, dms_id: str, user=None):
        """
        Storage sync. File counts only change when real files are uploaded
        through the application — no invented increments or checksum claims.
        """
        dms = DocumentSystemIntegration.objects.get(id=dms_id)
        state, latency_ms, detail = cls._probe_storage(dms)

        IntegrationService.log_integration_event(
            service_name=dms.name,
            event_name=f"Storage sync for {dms.name}",
            status={'HEALTHY': 'Success', 'UNREACHABLE': 'Failed'}.get(state, 'Pending'),
            payload_size=None,
            http_status_code=200 if state == 'HEALTHY' else 0,
            duration_ms=latency_ms or 0,
            direction="Inbound",
            details=detail
        )

        if state == 'HEALTHY':
            dms.save()  # last_sync is auto_now — a real successful probe counts as a sync point
            IntegrationService.log_audit(
                user=user,
                action="INTEGRATION_SYNC_COMPLETED",
                resource_id=dms.id,
                new_state={"provider": dms.name, "bucket": dms.bucket_or_drive_name, "latency_ms": latency_ms}
            )
        return dms


class GovernmentAPIProvider(BaseIntegrationProvider):
    """
    Government & Regulatory Inter-Agency API Bridge (CAC, LASRRA, e-GIS, FMW).
    Adheres strictly to Rule 44: Do Not Fabricate APIs. Where client credentials are pending,
    reports PENDING CLIENT API DOCUMENTATION/CREDENTIALS clearly.
    """
    @classmethod
    def _provider_token(cls, provider_code: str) -> str:
        """Inter-agency API bearer token, from the environment only."""
        code = (provider_code or '').upper().replace('-', '_')
        return os.getenv(f'GOV_{code}_API_TOKEN', '')

    @classmethod
    def test_connection(cls, gov_id: str, user=None) -> dict:
        """
        Real health ping against the agency endpoint. Without the agency's API
        token the check reports PENDING_CREDENTIALS and makes no network call —
        no 'connected' status or latency is fabricated.
        """
        gov = GovernmentAPIIntegration.objects.get(id=gov_id) if not str(gov_id).startswith('cac_') and not str(gov_id).startswith('lasrra_') and not str(gov_id).startswith('egis_') else GovernmentAPIIntegration.objects.get(api_key_identifier=gov_id)

        token = cls._provider_token(gov.provider_code)
        result = {
            "provider": gov.name,
            "endpoint": gov.endpoint_url,
            "response_time_ms": None,
            "documentation_status": gov.documentation_status,
            "checked_at": timezone.now().isoformat()
        }

        if not token:
            _log_pending_event(
                gov.name,
                f"Inter-Agency Health Ping: {gov.name}",
                [f"GOV_{(gov.provider_code or '').upper().replace('-', '_')}_API_TOKEN"],
            )
            if gov.status == 'connected':
                gov.status = 'pending_credentials'
                gov.save()
            result["status"] = "PENDING_CREDENTIALS"
            return result

        ok, http_status, latency_ms, error_detail = _http_probe(
            gov.endpoint_url, headers={'Authorization': f'Bearer {token}'})

        IntegrationService.log_integration_event(
            service_name=gov.name,
            event_name=f"Inter-Agency Health Ping: {gov.name}",
            status="Success" if ok else "Failed",
            payload_size=None,
            http_status_code=http_status or 0,
            duration_ms=latency_ms,
            direction="Outbound",
            details=(
                f"Live probe of {gov.endpoint_url} (auth: {gov.auth_method}) returned HTTP {http_status} in {latency_ms} ms."
                if ok else f"Probe of {gov.endpoint_url} failed after {latency_ms} ms: {error_detail}."
            )
        )

        IntegrationService.log_audit(
            user=user,
            action="INTEGRATION_TESTED",
            resource_id=gov.id,
            new_state={"provider": gov.name, "identifier": gov.api_key_identifier, "latency_ms": latency_ms, "reachable": ok}
        )

        if ok:
            gov.status = 'connected'
            gov.save()
            result["status"] = "HEALTHY"
        else:
            result["status"] = "UNREACHABLE"
        result["response_time_ms"] = latency_ms
        return result

    @classmethod
    def verify_entity(cls, provider_code: str, query_identifier: str, user=None) -> dict:
        """
        Executes an authorized regulatory verification lookup (e.g. CAC
        registration or e-GIS parcel coordinates) against the real agency API.
        Until the agency supplies API credentials the lookup reports
        PENDING_CREDENTIALS — a 'verified' status is NEVER fabricated.
        """
        code = (provider_code or '').upper()
        env_name = f"GOV_{code.replace('-', '_')}_API_TOKEN"
        provider_names = {
            'CAC': 'Corporate Affairs Commission (CAC)',
            'EGIS': 'Lagos e-GIS Land Registry',
            'E-GIS': 'Lagos e-GIS Land Registry',
            'LASRRA': 'Lagos State Residents Registration Agency (LASRRA)',
        }
        provider_name = provider_names.get(code, code)

        token = cls._provider_token(code)
        if not token:
            result = {
                "provider": provider_name,
                "query": query_identifier,
                "verified": False,
                "status": "PENDING_CREDENTIALS",
                "reason": (
                    f"{provider_name} verification API credentials are not configured "
                    f"({env_name}). Regulatory verification requires the agency's API "
                    "documentation and client credentials — no verification result was fabricated."
                ),
                "timestamp": timezone.now().isoformat()
            }
            _log_pending_event(
                provider_name,
                f"Regulatory Verification Lookup: {query_identifier}",
                [env_name],
            )
            IntegrationService.log_audit(
                user=user,
                action="REGULATORY_VERIFICATION_UNAVAILABLE",
                resource_id=query_identifier,
                new_state={"provider": provider_name, "status": "PENDING_CREDENTIALS"}
            )
            return result

        # Credentials configured: query the real agency endpoint and return
        # exactly what the agency responds with — interpretation stays with the
        # reviewing officer (Human-in-the-Loop), never auto-declared here.
        gov = GovernmentAPIIntegration.objects.filter(provider_code__iexact=code).first()
        if not gov or not gov.endpoint_url:
            return {
                "provider": provider_name,
                "query": query_identifier,
                "verified": False,
                "status": "PENDING_CONFIGURATION",
                "reason": f"No {provider_name} endpoint URL is configured.",
                "timestamp": timezone.now().isoformat()
            }

        url = f"{gov.endpoint_url.rstrip('/')}/{urllib.parse.quote(str(query_identifier))}"
        start = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                latency_ms = int((time.monotonic() - start) * 1000)
                body = resp.read(65536).decode('utf-8', 'replace')
                http_status = resp.status
        except urllib.error.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            IntegrationService.log_integration_event(
                service_name=provider_name,
                event_name=f"Regulatory Verification Lookup: {query_identifier}",
                status="Failed",
                payload_size=None,
                http_status_code=exc.code,
                duration_ms=latency_ms,
                direction="Outbound",
                details=f"Lookup against {url} returned HTTP {exc.code}."
            )
            return {
                "provider": provider_name,
                "query": query_identifier,
                "verified": False,
                "status": "AGENCY_REJECTED",
                "reason": f"Agency responded HTTP {exc.code}.",
                "timestamp": timezone.now().isoformat()
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            IntegrationService.log_integration_event(
                service_name=provider_name,
                event_name=f"Regulatory Verification Lookup: {query_identifier}",
                status="Failed",
                payload_size=None,
                http_status_code=0,
                duration_ms=latency_ms,
                direction="Outbound",
                details=f"Lookup against {url} failed: {exc}"
            )
            return {
                "provider": provider_name,
                "query": query_identifier,
                "verified": False,
                "status": "UNREACHABLE",
                "reason": str(exc),
                "timestamp": timezone.now().isoformat()
            }

        try:
            import json as _json
            agency_response = _json.loads(body)
        except ValueError:
            agency_response = body

        IntegrationService.log_integration_event(
            service_name=provider_name,
            event_name=f"Regulatory Verification Lookup: {query_identifier}",
            status="Success",
            payload_size=None,
            http_status_code=http_status,
            duration_ms=latency_ms,
            direction="Outbound",
            details=f"Live lookup against {url} returned HTTP {http_status} in {latency_ms} ms."
        )
        IntegrationService.log_audit(
            user=user,
            action="REGULATORY_ENTITY_LOOKUP_COMPLETED",
            resource_id=query_identifier,
            new_state={"provider": provider_name, "http_status": http_status}
        )
        return {
            "provider": provider_name,
            "query": query_identifier,
            "status": "LOOKUP_COMPLETED",
            "http_status": http_status,
            "agency_response": agency_response,
            "timestamp": timezone.now().isoformat()
        }


class APIKeyGateway:
    """
    API Credential Management Gateway.
    Generates hashed tokens with key prefix masking (e.g. ••••••••••••8A72),
    manages key rotation, and enforces token revocation.
    """
    @classmethod
    def generate_key(cls, name: str, app_type: str = 'OAuth 2.0 App', volume_tier: str = 'High (450k/day)', user=None) -> dict:
        raw_secret = f"nx_live_{secrets.token_urlsafe(32)}"
        key_prefix = raw_secret[:12]
        hashed = hashlib.sha256(raw_secret.encode('utf-8')).hexdigest()

        cred = APIKeyCredential.objects.create(
            name=name,
            key_prefix=key_prefix,
            hashed_key=hashed,
            app_type=app_type,
            volume_tier=volume_tier,
            status='Healthy',
            rate_limit_per_min=600
        )

        IntegrationService.log_integration_event(
            service_name="API Gateway",
            event_name=f"Provisioned API Credentials for {name}",
            status="Success",
            payload_size=None,
            http_status_code=0,
            duration_ms=0,
            direction="Inbound",
            details=f"Application '{name}' issued token prefix '{key_prefix}...'. Secret displayed once."
        )

        IntegrationService.log_audit(
            user=user,
            action="API_KEY_GENERATED",
            resource_id=cred.id,
            new_state={"name": name, "prefix": key_prefix, "app_type": app_type}
        )

        return {
            "id": str(cred.id),
            "name": cred.name,
            "key_prefix": cred.key_prefix,
            "raw_key": raw_secret,
            "app_type": cred.app_type,
            "volume_tier": cred.volume_tier,
            "status": cred.status,
            "created_at": cred.created_at
        }

    @classmethod
    def rotate_key(cls, key_id: str, user=None) -> dict:
        cred = APIKeyCredential.objects.get(id=key_id)
        raw_secret = f"nx_live_{secrets.token_urlsafe(32)}"
        new_prefix = raw_secret[:12]
        hashed = hashlib.sha256(raw_secret.encode('utf-8')).hexdigest()

        old_prefix = cred.key_prefix
        cred.key_prefix = new_prefix
        cred.hashed_key = hashed
        cred.status = 'Healthy'
        cred.save()

        IntegrationService.log_integration_event(
            service_name="API Gateway",
            event_name=f"Rotated API Credentials for {cred.name}",
            status="Success",
            payload_size=None,
            http_status_code=0,
            duration_ms=0,
            direction="Inbound",
            details=f"Rotated secret for '{cred.name}'. Old prefix '{old_prefix}', new prefix '{new_prefix}'."
        )

        IntegrationService.log_audit(
            user=user,
            action="API_KEY_ROTATED",
            resource_id=cred.id,
            new_state={"name": cred.name, "new_prefix": new_prefix, "old_prefix": old_prefix}
        )

        return {
            "id": str(cred.id),
            "name": cred.name,
            "key_prefix": cred.key_prefix,
            "raw_key": raw_secret,
            "status": cred.status,
            "rotated_at": timezone.now().isoformat()
        }

    @classmethod
    def revoke_key(cls, key_id: str, user=None) -> dict:
        cred = APIKeyCredential.objects.get(id=key_id)
        cred.status = 'Revoked'
        cred.revoked_at = timezone.now()
        cred.save()

        IntegrationService.log_integration_event(
            service_name="API Gateway",
            event_name=f"Revoked API Credentials for {cred.name}",
            status="Warning",
            payload_size=None,
            http_status_code=0,
            duration_ms=0,
            direction="Inbound",
            details=f"Revoked token access for '{cred.name}' ({cred.key_prefix}...)."
        )

        IntegrationService.log_audit(
            user=user,
            action="API_KEY_REVOKED",
            resource_id=cred.id,
            new_state={"name": cred.name, "status": "Revoked"}
        )

        return {
            "id": str(cred.id),
            "name": cred.name,
            "status": "Revoked",
            "revoked_at": cred.revoked_at.isoformat()
        }


# ==========================================
# MAIN INTEGRATION ORCHESTRATION SERVICE
# ==========================================

class IntegrationService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None, metadata=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Integration",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state,
                metadata=metadata or {}
            )
        except Exception:
            pass

    @classmethod
    def force_sync_device(cls, device_id: str, user=None):
        return TersusProvider.sync(device_id, user)

    @classmethod
    def test_tersus_health(cls, device_id: str, user=None):
        return TersusProvider.test_connection(device_id, user)

    @classmethod
    def sync_bim_platform(cls, bim_id: str, user=None):
        return BIMProvider.sync(bim_id, user)

    @classmethod
    def test_bim_health(cls, bim_id: str, user=None):
        return BIMProvider.test_connection(bim_id, user)

    @classmethod
    def sync_document_system(cls, dms_id: str, user=None):
        return DocumentProvider.sync(dms_id, user)

    @classmethod
    def test_document_health(cls, dms_id: str, user=None):
        return DocumentProvider.test_connection(dms_id, user)

    @classmethod
    def verify_government_api(cls, api_key_identifier: str, user=None):
        return GovernmentAPIProvider.test_connection(api_key_identifier, user)

    @classmethod
    def verify_government_entity(cls, provider_code: str, query_identifier: str, user=None):
        return GovernmentAPIProvider.verify_entity(provider_code, query_identifier, user)

    @classmethod
    def generate_api_key(cls, name: str, app_type: str = 'OAuth 2.0 App', volume_tier: str = 'High (450k/day)', user=None):
        return APIKeyGateway.generate_key(name, app_type, volume_tier, user)

    @classmethod
    def rotate_api_key(cls, key_id: str, user=None):
        return APIKeyGateway.rotate_key(key_id, user)

    @classmethod
    def revoke_api_key(cls, key_id: str, user=None):
        return APIKeyGateway.revoke_key(key_id, user)

    @classmethod
    def log_integration_event(cls, service_name: str, event_name: str, status: str = "Success",
                              payload_size: str = None, http_status_code: int = 0, details: str = None,
                              duration_ms: int = 0, error_code: str = None, direction: str = "Outbound"):
        return IntegrationLog.objects.create(
            service_name=service_name,
            event_name=event_name,
            status=status,
            payload_size=payload_size,
            http_status_code=http_status_code,
            duration_ms=duration_ms,
            error_code=error_code,
            direction=direction,
            details=details
        )

    @classmethod
    def get_integration_stats(cls):
        since = timezone.now() - datetime.timedelta(hours=24)
        requests_24h = IntegrationLog.objects.filter(created_at__gte=since).count()
        failed_24h = IntegrationLog.objects.filter(created_at__gte=since).exclude(status='Success').count()
        return {
            "total_requests_24h": str(requests_24h),
            "active_webhooks": WebhookSubscription.objects.filter(status='Active').count(),
            "failed_requests_rate": f"{round(failed_24h / requests_24h * 100, 2)}%" if requests_24h else None,
            "active_devices_count": TersusDevice.objects.filter(status='Active').count(),
            "total_devices_count": TersusDevice.objects.count(),
            "connected_bim_count": BIMIntegration.objects.filter(status='Connected').count(),
            "active_dms_count": DocumentSystemIntegration.objects.filter(status='Active').count(),
        }

class SettingsService:
    """Core domain service for user administration, RBAC, workflows, inspection checklists, standards, and alerts."""

    @classmethod
    def get_staff_users(cls, search: str = None, department: str = None, role: str = None):
        cls.seed_initial_settings()
        users_qs = User.objects.all().order_by('-date_joined')
        
        results = []
        user_emails = set()

        for u in users_qs:
            dept = getattr(u, 'department', 'Urban Planning')
            user_role = getattr(u, 'role', 'Reviewer') if getattr(u, 'role', None) else ('System Administrator' if u.is_superuser else 'Reviewer')
            
            # Search filter
            if search:
                s = search.lower()
                full_name = f"{u.first_name} {u.last_name}".lower()
                if s not in full_name and s not in u.email.lower() and s not in user_role.lower():
                    continue
            if department and department.lower() not in dept.lower():
                continue
            if role and role.lower() not in user_role.lower():
                continue

            user_emails.add(u.email.lower())
            results.append({
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}".strip() or u.username,
                "email": u.email,
                "role": user_role,
                "department": dept,
                "phone": getattr(u, 'phone_number', '') or getattr(u, 'phone', '') or "",
                "status": "Active" if u.is_active else "Inactive",
                "lastLogin": "2 mins ago" if u.last_login else "Never"
            })

        # Also include Pending User Invitations
        pending_invitations = UserInvitation.objects.filter(status='Pending').order_by('-created_at')
        for inv in pending_invitations:
            if inv.email.lower() in user_emails:
                continue

            if search:
                s = search.lower()
                if s not in inv.name.lower() and s not in inv.email.lower() and s not in inv.role.lower():
                    continue
            if department and department.lower() not in inv.department.lower():
                continue
            if role and role.lower() not in inv.role.lower():
                continue

            results.append({
                "id": str(inv.id),
                "name": inv.name,
                "email": inv.email,
                "role": inv.role,
                "department": inv.department,
                "phone": "",
                "status": "Pending",
                "lastLogin": "Invite Sent",
                "invited_at": inv.created_at.isoformat() if inv.created_at else None
            })

        return results

    @classmethod
    def invite_user(
        cls,
        email: str,
        name: str,
        role: str = "Reviewer",
        department: str = "Urban Planning",
        invited_by=None,
        agency_id=None,
        district_id=None,
        assigned_projects=None,
        invite_code: str = None,
        expires_days: int = 7
    ):
        import uuid
        code = (invite_code or f"{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[4:8].upper()}").strip().upper()
        temp_password = f"Nexucon@{uuid.uuid4().hex[:4].upper()}2026!"

        from apps.government.models import Agency, District
        agency = Agency.objects.filter(id=agency_id).first() if agency_id else None
        district = District.objects.filter(id=district_id).first() if district_id else None
        projects_list = assigned_projects if isinstance(assigned_projects, list) else []

        invitation, _ = UserInvitation.objects.update_or_create(
            email=email.strip().lower(),
            defaults={
                'name': name.strip(),
                'role': role,
                'department': department,
                'invited_by': invited_by if getattr(invited_by, 'is_authenticated', False) else None,
                'status': 'Pending',
                'invite_code': code,
                'temporary_password': temp_password,
                'agency': agency,
                'district': district,
                'assigned_projects': projects_list,
                'expires_at': timezone.now() + timezone.timedelta(days=expires_days)
            }
        )

        # Pre-provision User in Django Database with temporary password
        name_parts = name.strip().split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        user = User.objects.filter(email=email.strip().lower()).first()
        if not user:
            user = User.objects.create_user(
                username=email.strip().lower(),
                email=email.strip().lower(),
                first_name=first_name,
                last_name=last_name,
                password=temp_password,
                is_active=True,
                is_verified=False
            )
        else:
            user.first_name = first_name or user.first_name
            user.last_name = last_name or user.last_name
            user.set_password(temp_password)
            user.save()

        # Link Profile
        from apps.government.models import Profile, Role
        role_obj = Role.objects.filter(name__iexact=role).first()
        if not role_obj:
            role_obj = Role.objects.create(name=role)

        profile, _ = Profile.objects.get_or_create(user=user)
        if agency:
            profile.agency = agency
        if district:
            profile.district = district
        profile.role = role_obj
        profile.save()

        if getattr(invited_by, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=invited_by,
                user_name=f"{invited_by.first_name} {invited_by.last_name}".strip() or invited_by.username,
                action="INVITE_STAFF_USER",
                resource_type="UserInvitation",
                resource_id=str(invitation.id),
                new_state={
                    "email": email,
                    "role": role,
                    "department": department,
                    "agency": agency.name if agency else None,
                    "district": district.name if district else None,
                    "assigned_projects": projects_list,
                    "invite_code": code
                }
            )

        # Dispatch Resend HTML Invitation Email with Temporary Passcode
        try:
            from apps.notifications.email_service import EmailService
            EmailService.send_invitation_email(
                email=email,
                name=name,
                role=role,
                department=department,
                invite_token=str(invitation.id),
                invited_by=invited_by,
                temp_password=temp_password,
                invite_code=code
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch invitation email via Resend: {e}")

        return invitation

    @classmethod
    def validate_inspector_invitation(cls, token: str = None, invite_code: str = None, email: str = None, temp_password: str = None):
        """
        Strictly validate inspector invitation by invite_code, temporary_password, or token.
        Inspectors CANNOT proceed unless officially registered via the Government Directorate.
        """
        import uuid
        from django.db.models import Q
        email_clean = (email or '').strip().lower()
        invitation = None

        if token:
            try:
                val_uuid = uuid.UUID(str(token))
                invitation = UserInvitation.objects.filter(Q(id=val_uuid) | Q(token=str(token))).first()
            except (ValueError, AttributeError):
                invitation = UserInvitation.objects.filter(token=str(token)).first()

        if not invitation and email_clean:
            invitation = UserInvitation.objects.filter(email__iexact=email_clean).first()

        if not invitation and invite_code:
            norm_code = invite_code.strip().replace('-', '').upper()
            for inv in UserInvitation.objects.all():
                if (inv.invite_code or '').strip().replace('-', '').upper() == norm_code:
                    invitation = inv
                    break

        if not invitation:
            return {
                "valid": False,
                "error_code": "NOT_REGISTERED",
                "message": "Access Denied: This email has not been registered as an accredited inspector by the Agency Directorate. Access is strictly invite-based."
            }

        if invitation.status == 'Revoked':
            return {
                "valid": False,
                "error_code": "REVOKED",
                "message": "This invitation has been revoked. Contact your Agency Directorate."
            }

        if invitation.status == 'Accepted':
            from django.contrib.auth import get_user_model
            User = get_user_model()
            existing_user = User.objects.filter(email__iexact=invitation.email).first()
            if existing_user and existing_user.is_verified:
                return {
                    "valid": False,
                    "error_code": "ALREADY_ACCEPTED",
                    "message": "This inspector account is already activated. Please sign in directly with your permanent password."
                }

        if invitation.expires_at and timezone.now() > invitation.expires_at:
            if invitation.status != 'Expired':
                invitation.status = 'Expired'
                invitation.save(update_fields=['status'])
            return {
                "valid": False,
                "error_code": "EXPIRED",
                "message": "This invitation has expired. Contact your Agency Directorate."
            }

        # Verification of Invite Code or Temporary Password
        code_matched = False
        temp_matched = False

        from django.contrib.auth import get_user_model
        User = get_user_model()
        u = User.objects.filter(email__iexact=invitation.email).first()

        expected_code = (invitation.invite_code or '').strip().replace('-', '').upper()
        expected_temp = (invitation.temporary_password or '').strip()

        # Check all provided credentials against both expected invite_code and temporary_password
        provided_creds = [c.strip() for c in [invite_code, temp_password] if c and c.strip()]
        for cred in provided_creds:
            norm_cred = cred.replace('-', '').upper()
            if expected_code and (norm_cred == expected_code or cred.upper() == expected_code):
                code_matched = True
            if expected_temp and cred == expected_temp:
                temp_matched = True
            if u and u.check_password(cred):
                temp_matched = True

        token_matched = bool(token and invitation and (str(invitation.id) == str(token) or invitation.token == str(token)))

        if not (code_matched or temp_matched or token_matched):
            return {
                "valid": False,
                "error_code": "INVALID_CREDENTIALS",
                "message": "Verification failed: You must input the valid Invite Code or Temporary Password provided in your dispatch notice."
            }

        from apps.projects.models import Project
        assigned_projects_data = []
        if invitation.assigned_projects and isinstance(invitation.assigned_projects, list):
            projects = Project.objects.filter(id__in=invitation.assigned_projects)
            for p in projects:
                assigned_projects_data.append({
                    "id": str(p.id),
                    "name": p.name,
                    "reference_number": p.reference_number,
                    "site_address": p.site_address or f"{p.lga or ''}, {p.state or ''}".strip(', '),
                    "status": p.status,
                    "project_type": p.project_type or 'General Construction'
                })

        return {
            "valid": True,
            "id": str(invitation.id),
            "token": invitation.token,
            "invite_code": invitation.invite_code,
            "email": invitation.email,
            "name": invitation.name,
            "role": invitation.role,
            "department": invitation.department,
            "agency_name": invitation.agency.name if invitation.agency else "State Building Control Agency",
            "district_name": invitation.district.name if invitation.district else "Central Directorate",
            "assigned_projects": assigned_projects_data,
            "temporary_password": invitation.temporary_password,
            "expires_at": invitation.expires_at
        }

    @classmethod
    def accept_invitation(cls, email: str = None, token: str = None, password: str = None, full_name: str = None, invite_code: str = None, temp_password: str = None):
        """Finalize invite acceptance with strict verification of invite_code or temporary_password."""
        from django.db.models import Q
        email_clean = (email or '').strip().lower()
        invitation = None

        if token:
            try:
                import uuid
                val_uuid = uuid.UUID(str(token))
                invitation = UserInvitation.objects.filter(Q(id=val_uuid) | Q(token=str(token))).first()
            except (ValueError, AttributeError):
                invitation = UserInvitation.objects.filter(token=str(token)).first()

        if not invitation and email_clean:
            invitation = UserInvitation.objects.filter(email__iexact=email_clean).first()

        if not invitation and invite_code:
            norm_code = invite_code.strip().replace('-', '').upper()
            for inv in UserInvitation.objects.all():
                if (inv.invite_code or '').strip().replace('-', '').upper() == norm_code:
                    invitation = inv
                    break

        if not invitation:
            return {"success": False, "message": f"Access Denied: No invitation record found for {email_clean or 'provided credentials'}. Registration must be completed via the Government Directorate."}

        # Check credentials match
        code_matched = False
        temp_matched = False

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(email__iexact=invitation.email).first()

        expected_code = (invitation.invite_code or '').strip().replace('-', '').upper()
        expected_temp = (invitation.temporary_password or '').strip()

        provided_creds = [c.strip() for c in [invite_code, temp_password] if c and c.strip()]
        for cred in provided_creds:
            norm_cred = cred.replace('-', '').upper()
            if expected_code and (norm_cred == expected_code or cred.upper() == expected_code):
                code_matched = True
            if expected_temp and cred == expected_temp:
                temp_matched = True
            elif user and user.check_password(cred):
                temp_matched = True

        token_matched = bool(token and invitation and (str(invitation.id) == str(token) or invitation.token == str(token)))

        if not (code_matched or temp_matched or token_matched):
            return {
                "success": False,
                "message": "Access Restricted: You must input the valid Invite Code or Temporary Password issued by the Agency Directorate."
            }

        if not password or len(password) < 8:
            return {"success": False, "message": "Permanent password must be at least 8 characters long."}

        name_parts = (full_name or invitation.name or '').strip().split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''
        user_email = invitation.email

        if not user:
            user = User.objects.create_user(
                username=user_email,
                email=user_email,
                first_name=first_name,
                last_name=last_name,
                password=password,
                is_active=True,
                is_verified=True
            )
        else:
            if first_name: user.first_name = first_name
            if last_name: user.last_name = last_name
            user.set_password(password)
            user.is_active = True
            user.is_verified = True
            user.save()

        invitation.status = 'Accepted'
        invitation.accepted_at = timezone.now()
        invitation.save()

        from apps.government.models import Profile, Role
        user_role_str = invitation.role or 'Inspector'
        role_obj = Role.objects.filter(name__iexact=user_role_str).first()
        if not role_obj:
            role_obj = Role.objects.create(name=user_role_str)

        profile, _ = Profile.objects.get_or_create(user=user)
        if invitation.agency:
            profile.agency = invitation.agency
        if invitation.district:
            profile.district = invitation.district
        profile.role = role_obj
        profile.is_active_staff = True
        profile.save()

        if invitation.assigned_projects and isinstance(invitation.assigned_projects, list):
            from apps.projects.models import Project
            from apps.inspections.models import Inspection
            for proj_id in invitation.assigned_projects:
                proj = Project.objects.filter(id=proj_id).first()
                if proj:
                    proj.assigned_inspector = user.get_full_name() or user.email
                    proj.save(update_fields=['assigned_inspector'])
                    Inspection.objects.filter(
                        project=proj,
                        status__in=['REQUESTED', 'SCHEDULED'],
                        inspector__isnull=True
                    ).update(inspector=user, inspector_name=user.get_full_name() or user.email)

        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(user)

        from apps.accounts.serializers import UserMeSerializer
        user_data = UserMeSerializer(user).data

        return {
            "success": True,
            "message": "Inspector terminal successfully activated.",
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": user_data
        }

    @classmethod
    def toggle_user_status(cls, user_id: str, actor=None):
        user = User.objects.get(id=user_id)
        user.is_active = not user.is_active
        user.save()

        if getattr(actor, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=actor,
                user_name=f"{actor.first_name} {actor.last_name}".strip() or actor.username,
                action="TOGGLE_USER_STATUS",
                resource_type="User",
                resource_id=str(user.id),
                new_state={"is_active": user.is_active}
            )
        return user

    @classmethod
    def get_roles(cls):
        cls.seed_initial_settings()
        return CustomRole.objects.all().order_by('name')

    @classmethod
    def create_custom_role(cls, name: str, description: str = None, actor=None):
        if CustomRole.objects.filter(name__iexact=name).exists():
            raise ValidationError(f"Role '{name}' already exists.")

        role = CustomRole.objects.create(
            name=name,
            description=description,
            role_type='Custom Role',
            is_system_default=False,
            active_users_count=0
        )

        # Populate baseline permissions
        default_perms = [
            ("Permits & Approvals", "View Permit Applications", True),
            ("Permits & Approvals", "Approve/Reject Permits", False),
            ("Permits & Approvals", "Grant Zoning Variances", False),
            ("Permits & Approvals", "Sign Off Final Occupancy", False),
            ("Site Inspections", "View Inspection Logs", True),
            ("Site Inspections", "Generate Non-Conformance (NCR)", False),
            ("Site Inspections", "Halt Construction (Work Stoppage)", False),
            ("System & Audit", "View Audit Records", False),
            ("System & Audit", "Export Compliance Packages", False),
            ("System & Audit", "Manage Roles & Permissions", False),
        ]
        for mod, perm, granted in default_perms:
            RolePermission.objects.create(role=role, module=mod, permission_name=perm, is_granted=granted)

        return role

    @classmethod
    def get_roles_matrix(cls):
        cls.seed_initial_settings()
        roles = CustomRole.objects.all().order_by('name')
        
        # Unique modules and permission names
        modules = [
            {
                "module": "Permits & Approvals",
                "permissions": [
                    "View Permit Applications",
                    "Approve/Reject Permits",
                    "Grant Zoning Variances",
                    "Sign Off Final Occupancy"
                ]
            },
            {
                "module": "Site Inspections",
                "permissions": [
                    "View Inspection Logs",
                    "Generate Non-Conformance (NCR)",
                    "Halt Construction (Work Stoppage)"
                ]
            },
            {
                "module": "System & Audit",
                "permissions": [
                    "View Audit Records",
                    "Export Compliance Packages",
                    "Manage Roles & Permissions"
                ]
            }
        ]

        # Fetch all permissions
        perm_map = {}
        for rp in RolePermission.objects.select_related('role'):
            key = f"{rp.role.name}::{rp.module}::{rp.permission_name}"
            perm_map[key] = rp.is_granted

        structured_modules = []
        for m in modules:
            mod_perms = []
            for p in m["permissions"]:
                row = {
                    "name": p,
                    "admin": perm_map.get(f"System Administrator::{m['module']}::{p}", True),
                    "planner": perm_map.get(f"City Planner::{m['module']}::{p}", False),
                    "inspector": perm_map.get(f"Lead Inspector::{m['module']}::{p}", False),
                    "reviewer": perm_map.get(f"Reviewer::{m['module']}::{p}", False)
                }
                mod_perms.append(row)
            structured_modules.append({
                "module": m["module"],
                "permissions": mod_perms
            })

        return {
            "roles": [{"name": r.name, "users": r.active_users_count, "type": r.role_type} for r in roles],
            "permission_modules": structured_modules
        }

    @classmethod
    def update_role_permission(cls, role_name: str, module: str, permission_name: str, is_granted: bool, actor=None):
        cls.seed_initial_settings()
        role, _ = CustomRole.objects.get_or_create(name=role_name, defaults={"role_type": "Custom Role", "is_system_default": False})
        rp, _ = RolePermission.objects.get_or_create(role=role, module=module, permission_name=permission_name)
        rp.is_granted = is_granted
        rp.save()
        return rp

    @classmethod
    def get_workflows(cls):
        cls.seed_initial_settings()
        return ApprovalWorkflow.objects.prefetch_related('steps').all().order_by('id')

    @classmethod
    def create_workflow(cls, name: str, steps: list, description: str = None, actor=None):
        with transaction.atomic():
            wf = ApprovalWorkflow.objects.create(
                name=name,
                description=description,
                status='Active'
            )
            for idx, s in enumerate(steps or []):
                title = s.get('title', f"Step {idx+1}") if isinstance(s, dict) else str(s)
                role = s.get('role', 'Reviewer') if isinstance(s, dict) else 'Reviewer'
                icon_name = s.get('icon', 'ShieldCheck') if isinstance(s, dict) else 'ShieldCheck'
                WorkflowStep.objects.create(
                    workflow=wf,
                    step_order=idx + 1,
                    title=title,
                    role=role,
                    icon_name=icon_name,
                    is_system_enforced=False
                )
            return wf

    @classmethod
    def get_templates(cls):
        cls.seed_initial_settings()
        return InspectionTemplate.objects.prefetch_related('items').all().order_by('-created_at')

    @classmethod
    def create_template(cls, name: str, department: str, items: list = None, actor=None):
        with transaction.atomic():
            tpl = InspectionTemplate.objects.create(
                name=name,
                department=department,
                status='Active',
                version='v1.0'
            )
            if items:
                for idx, it in enumerate(items):
                    title = it.get('title', 'Check Item') if isinstance(it, dict) else str(it)
                    field_type = it.get('field_type', 'Pass/Fail Toggle') if isinstance(it, dict) else 'Pass/Fail Toggle'
                    is_required = it.get('is_required', True) if isinstance(it, dict) else True
                    ChecklistItem.objects.create(
                        template=tpl,
                        item_order=idx + 1,
                        title=title,
                        field_type=field_type,
                        is_required=is_required
                    )
            return tpl

    @classmethod
    def add_checklist_item(cls, template_id: str, title: str, field_type: str = 'Pass/Fail Toggle', is_required: bool = True):
        tpl = InspectionTemplate.objects.get(id=template_id)
        next_order = (tpl.items.count() or 0) + 1
        return ChecklistItem.objects.create(
            template=tpl,
            item_order=next_order,
            title=title,
            field_type=field_type,
            is_required=is_required
        )

    @classmethod
    def delete_template(cls, template_id: str, actor=None):
        tpl = InspectionTemplate.objects.get(id=template_id)
        tpl.delete()
        return True

    @classmethod
    def get_standards(cls):
        cls.seed_initial_settings()
        return ComplianceStandard.objects.all().order_by('category', 'key')

    @classmethod
    def update_standards(cls, thresholds: dict, actor=None):
        cls.seed_initial_settings()
        updated = []
        for key, val in thresholds.items():
            try:
                std = ComplianceStandard.objects.get(key=key)
                std.num_value = float(val)
                std.save()
                updated.append(std)
            except (ComplianceStandard.DoesNotExist, ValueError):
                continue
        return updated

    @classmethod
    def get_statutory_documents(cls):
        cls.seed_initial_settings()
        return StatutoryDocument.objects.all().order_by('code')

    @classmethod
    def add_statutory_document(cls, code: str, name: str, connected_features: list, document_url: str = None, actor=None):
        cls.seed_initial_settings()
        return StatutoryDocument.objects.create(
            code=code,
            name=name,
            connected_features=connected_features or [],
            document_url=document_url
        )

    @classmethod
    def get_notification_preferences(cls):
        cls.seed_initial_settings()
        cats = NotificationPreferenceCategory.objects.all().order_by('category', 'event_label')
        
        grouped = {}
        for c in cats:
            if c.category not in grouped:
                grouped[c.category] = []
            grouped[c.category].append({
                "id": str(c.id),
                "event_label": c.event_label,
                "label": c.event_label,
                "in_app": c.in_app,
                "email": c.email,
                "sms": c.sms,
                "is_locked": c.is_locked,
                "locked": c.is_locked
            })

        categories_def = [
            ("Critical Safety Incidents", "Work stoppages, severe environmental breaches, and major safety hazards.", "text-red-500"),
            ("Permits & Approvals", "New submissions, required reviews, and final sign-offs.", "text-blue-500"),
            ("Field Inspections", "Inspection requests, NCR generation, and schedule changes.", "text-emerald-500")
        ]

        result = []
        for cat_name, desc, color in categories_def:
            items_list = grouped.get(cat_name, [])
            result.append({
                "category": cat_name,
                "title": cat_name,
                "description": desc,
                "color": color,
                "items": items_list,
                "settings": items_list
            })

        return result

    @classmethod
    def update_notification_preference(cls, category: str, event_label: str, channel: str, enabled: bool, actor=None):
        cls.seed_initial_settings()
        pref, _ = NotificationPreferenceCategory.objects.get_or_create(
            category=category,
            event_label=event_label,
            defaults={"in_app": True, "email": True, "sms": False, "is_locked": False}
        )
        if pref.is_locked and channel in ['in_app', 'email'] and not enabled:
            raise PermissionDenied("Critical safety alert channels are locked and cannot be disabled.")

        if channel in ['in_app', 'push']:
            pref.in_app = enabled
        elif channel == 'email':
            pref.email = enabled
        elif channel == 'sms':
            pref.sms = enabled
        pref.save()
        return pref

    @classmethod
    def get_routing_rules(cls):
        cls.seed_initial_settings()
        return NotificationRoutingRule.objects.all().order_by('-created_at')

    @classmethod
    def add_routing_rule(cls, trigger_event: str, primary_recipient: str, sla_timeline: str, escalation_target: str, actor=None):
        return NotificationRoutingRule.objects.create(
            trigger_event=trigger_event,
            primary_recipient=primary_recipient,
            sla_timeline=sla_timeline,
            escalation_target=escalation_target,
            is_active=True
        )

    @classmethod
    def delete_routing_rule(cls, rule_id: str, actor=None):
        rule = NotificationRoutingRule.objects.get(id=rule_id)
        rule.delete()
        return True

    @classmethod
    def get_webhooks(cls):
        cls.seed_initial_settings()
        return WebhookSubscription.objects.all().order_by('-created_at')

    @classmethod
    def create_webhook(cls, name: str, target_url: str, events: list, actor=None):
        return WebhookSubscription.objects.create(
            name=name,
            target_url=target_url,
            events=events or ["permit.created", "permit.updated", "inspection.failed"],
            status='Active'
        )

    @classmethod
    def get_agency_profile(cls):
        cls.seed_initial_settings()
        profile = AgencyProfile.objects.first()
        if not profile:
            profile = AgencyProfile.objects.create()
        return profile

    @classmethod
    def update_agency_profile(cls, data: dict, user=None):
        profile = cls.get_agency_profile()
        for key, val in data.items():
            if hasattr(profile, key) and key not in ['id', 'created_at', 'updated_at']:
                setattr(profile, key, val)
        profile.save()

        if getattr(user, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=user,
                user_name=f"{user.first_name} {user.last_name}".strip() or user.username,
                action="AGENCY_PROFILE_UPDATED",
                resource_type="AgencyProfile",
                resource_id=str(profile.id),
                new_state={"agency_name": profile.agency_name, "agency_code": profile.agency_code, "status": profile.status}
            )
        return profile

    @classmethod
    def get_report_templates(cls):
        cls.seed_initial_settings()
        return ReportTemplate.objects.all().order_by('-is_active_default', 'name')

    @classmethod
    def get_active_report_template(cls):
        cls.seed_initial_settings()
        return ReportTemplate.objects.filter(is_active_default=True).first() or ReportTemplate.objects.first()

    @classmethod
    def set_active_report_template(cls, template_id: str, user=None):
        ReportTemplate.objects.all().update(is_active_default=False)
        tpl = ReportTemplate.objects.get(id=template_id)
        tpl.is_active_default = True
        tpl.save()

        if getattr(user, 'is_authenticated', False):
            AuditEvent.objects.create(
                user=user,
                user_name=f"{user.first_name} {user.last_name}".strip() or user.username,
                action="REPORT_TEMPLATE_ACTIVATED",
                resource_type="ReportTemplate",
                resource_id=tpl.id,
                new_state={"name": tpl.name, "theme_style": tpl.theme_style, "is_active_default": True}
            )
        return tpl

    @classmethod
    def delete_webhook(cls, webhook_id: str, actor=None):
        wh = WebhookSubscription.objects.get(id=webhook_id)
        wh.delete()
        return True

    @classmethod
    def seed_initial_settings(cls):
        # 0. Seed Agency Profile
        if not AgencyProfile.objects.exists():
            AgencyProfile.objects.create(
                agency_name="Lagos State Ministry of Physical Planning & Urban Development (MPP&UD)",
                agency_code="LASG-MPPUD-01",
                logo_url="/images/agency-logo.png",
                description="Central Statutory Enforcement, Development Control, and Building Clearance Authority.",
                government_level="State",
                jurisdiction="Lagos State, Federal Republic of Nigeria",
                official_email="planning@lagosstate.gov.ng",
                phone="+234 1 234 5678",
                website="https://mppud.lagosstate.gov.ng",
                office_address="Block 15, The Secretariat, Alausa, Ikeja, Lagos",
                country="Nigeria",
                state="Lagos State",
                lga="Ikeja",
                timezone="Africa/Lagos (GMT+1)",
                default_language="English (NG)",
                status="Active"
            )

        # 0.1 Seed Report Presentation Templates
        if not ReportTemplate.objects.exists():
            ReportTemplate.objects.create(
                id="RPT-EXEC-01",
                name="Executive Ministerial Presentation Template",
                description="Vibrant executive format with detailed cover page, full KPI cards, non-technical project footer, and comprehensive Nigerian Industrial Standards (NIS blocks, cement, steel, concrete) citations.",
                theme_style="Executive Vibrant",
                cover_page_style="Detailed Architectural Hero",
                header_color="#022C4F",
                accent_color="#2563EB",
                is_active_default=True,
                footer_config={
                    "show_client_name": True,
                    "show_project_name": True,
                    "show_lga_zone": True,
                    "show_officer_sig": True,
                    "disclaimer": "Confidential statutory document issued under the National Building Code of Nigeria & comprehensive Nigerian Industrial Standards (NIS 87 Blocks, NIS 11 Cement, NIS 117 Steel Rebar, NIS 156 Concrete). Accessible executive layout."
                },
                building_code_citations=[
                    "National Building Code of Nigeria (NBC 2006/2020 Revision)",
                    "SON NIS 87:2007 - Standard for Sandcrete Blocks & Precast Masonry Units",
                    "SON NIS 11:2014 / NIS 444 - Portland Cement Specifications (CEM I / II)",
                    "SON NIS 117:2004 - High-Yield Deformed Steel Rebar Standards",
                    "SON NIS 156 / NIS 820 - Structural Concrete Aggregates & Ready-Mix Criteria",
                    "SON NIS 74 / NIS 378 - Building Electrical Installation & Cable Standards",
                    "SON NIS 384 - Building Plumbing & Sanitary Installation Systems",
                    "Lagos State Urban and Regional Planning and Development Law (2019/2024)",
                    "Lagos State Building Control Agency (LASBCA) Regulations"
                ]
            )
            ReportTemplate.objects.create(
                id="RPT-STAT-02",
                name="Statutory Compliance & Technical Audit",
                description="Formal governmental inspection audit with emerald/teal header accents, multi-material NIS regulatory clause references, and statutory sign-off block.",
                theme_style="Statutory Technical",
                cover_page_style="State Coat of Arms Gradient",
                header_color="#0F766E",
                accent_color="#10B981",
                is_active_default=False,
                footer_config={
                    "show_client_name": True,
                    "show_project_name": True,
                    "show_lga_zone": True,
                    "show_officer_sig": True,
                    "disclaimer": "Statutory audit certified by the Directorate of Building Control and Safety Enforcement in compliance with SON NIS 87, NIS 11, NIS 117, NIS 156 & NBC."
                },
                building_code_citations=[
                    "National Building Code of Nigeria (NBC Part II Structural & Materials Requirements)",
                    "SON NIS 87:2007 - Sandcrete Blocks (Compressive Strength >= 3.45 N/mm2 / 7.0 N/mm2)",
                    "SON NIS 11:2014 - Ordinary Portland Cement Benchmark",
                    "SON NIS 117:2004 - Steel Rebar Minimum Yield Strength (460/500 N/mm2)",
                    "SON NIS 156 - Concrete Quality & Silt Content Thresholds (<3%)",
                    "Lagos State Urban and Regional Planning and Development Law"
                ]
            )
            ReportTemplate.objects.create(
                id="RPT-BRIEF-03",
                name="Modern Vibrant Stakeholder Brief",
                description="Colorful stakeholder report layout designed for public transparency, non-technical readers, and ministerial briefings with full NIS citations.",
                theme_style="Modern Architectural",
                cover_page_style="Split Grid Presentation",
                header_color="#4338CA",
                accent_color="#8B5CF6",
                is_active_default=False,
                footer_config={
                    "show_client_name": True,
                    "show_project_name": True,
                    "show_lga_zone": True,
                    "show_officer_sig": True,
                    "disclaimer": "Quarterly executive briefing intended for public and non-technical stakeholders under Nigerian Industrial Standards."
                },
                building_code_citations=[
                    "National Building Code of Nigeria",
                    "SON NIS 87 Blocks, NIS 11 Cement & NIS 117 Steel Rebar Specifications",
                    "Lagos State Building Control Agency (LASBCA) Regulations"
                ]
            )
            ReportTemplate.objects.create(
                id="RPT-ENG-04",
                name="Standard Engineering Inspection Sheet",
                description="Clean, high-density engineering sheet focused on test metrics, sandcrete block crushing tests, concrete core sampling, and structural measurements.",
                theme_style="Minimalist Slate",
                cover_page_style="Clean Executive Header",
                header_color="#1E293B",
                accent_color="#F59E0B",
                is_active_default=False,
                footer_config={
                    "show_client_name": True,
                    "show_project_name": True,
                    "show_lga_zone": True,
                    "show_officer_sig": True,
                    "disclaimer": "Field inspection sheet certified by the Lead Structural Surveyor, Materials Quality Engineer, and Site Geotechnical Inspector."
                },
                building_code_citations=[
                    "National Building Code of Nigeria (Section 13: Site Safety, Excavations & Concrete)",
                    "SON NIS 87 - Compressive Strength Crushing Tests for Blocks",
                    "SON NIS 117 - Tensile and Bending Steel Rebar Tests",
                    "SON NIS 156 / NIS 820 - Slump, Curing and Concrete Cube Crushing Tests"
                ]
            )

        # 1. Seed Roles
        if not CustomRole.objects.exists():
            admin_r = CustomRole.objects.create(name="System Administrator", role_type="System Default", is_system_default=True, active_users_count=0)
            planner_r = CustomRole.objects.create(name="City Planner", role_type="Custom Role", is_system_default=False, active_users_count=0)
            inspector_r = CustomRole.objects.create(name="Lead Inspector", role_type="Custom Role", is_system_default=False, active_users_count=0)
            reviewer_r = CustomRole.objects.create(name="Reviewer", role_type="Custom Role", is_system_default=False, active_users_count=0)

            # Seed Permissions
            perms = [
                ("Permits & Approvals", "View Permit Applications", True, True, True, True),
                ("Permits & Approvals", "Approve/Reject Permits", True, True, False, False),
                ("Permits & Approvals", "Grant Zoning Variances", True, True, False, False),
                ("Permits & Approvals", "Sign Off Final Occupancy", True, True, True, False),
                ("Site Inspections", "View Inspection Logs", True, True, True, True),
                ("Site Inspections", "Generate Non-Conformance (NCR)", True, False, True, False),
                ("Site Inspections", "Halt Construction (Work Stoppage)", True, False, True, False),
                ("System & Audit", "View Audit Records", True, False, False, False),
                ("System & Audit", "Export Compliance Packages", True, False, False, False),
                ("System & Audit", "Manage Roles & Permissions", True, False, False, False),
            ]
            for mod, p_name, adm, pln, ins, rev in perms:
                RolePermission.objects.create(role=admin_r, module=mod, permission_name=p_name, is_granted=adm)
                RolePermission.objects.create(role=planner_r, module=mod, permission_name=p_name, is_granted=pln)
                RolePermission.objects.create(role=inspector_r, module=mod, permission_name=p_name, is_granted=ins)
                RolePermission.objects.create(role=reviewer_r, module=mod, permission_name=p_name, is_granted=rev)

        # 2. Seed Workflows
        if not ApprovalWorkflow.objects.exists():
            wf_master = ApprovalWorkflow.objects.create(
                id="WF-00-MASTER",
                name="Master Building Collapse Prevention Pipeline",
                status="System Enforced",
                description="Mandatory 5-stage collapse prevention gate."
            )
            WorkflowStep.objects.create(workflow=wf_master, step_order=1, title="Approval & Permit Gate", role="Agency Approvers", icon_name="ShieldCheck", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=2, title="Construction Oversight", role="Inspectors & Digital Eye", icon_name="HardHat", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=3, title="Deviation Detection", role="Automated Engine", icon_name="Search", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=4, title="Action & Stop-Work", role="System Escalation", icon_name="AlertTriangle", is_system_enforced=True)
            WorkflowStep.objects.create(workflow=wf_master, step_order=5, title="Corrective Verification", role="Review Board", icon_name="CheckCircle2", is_system_enforced=True)

            wf1 = ApprovalWorkflow.objects.create(
                id="WF-01",
                name="Standard Foundation Permit",
                status="Active",
                description="Foundation inspection & approval chain."
            )
            WorkflowStep.objects.create(workflow=wf1, step_order=1, title="Initial Submission", role="Developer/Contractor", icon_name="FileText")
            WorkflowStep.objects.create(workflow=wf1, step_order=2, title="Technical Review", role="Structural Engineer", icon_name="HardHat")
            WorkflowStep.objects.create(workflow=wf1, step_order=3, title="Final Sign-off", role="City Planner", icon_name="CheckCircle2")

        # 3. Seed Inspection Templates
        if not InspectionTemplate.objects.exists():
            tpl1 = InspectionTemplate.objects.create(
                id="TPL-091",
                name="Deep Foundation Pour Checklist",
                department="Structural",
                status="Active",
                version="v1.4"
            )
            ChecklistItem.objects.create(template=tpl1, item_order=1, title="Record concrete slump measurement (inches)", field_type="Number Input", is_required=True)
            ChecklistItem.objects.create(template=tpl1, item_order=2, title="Are all rebar ties secure and spaced according to plan?", field_type="Pass/Fail Toggle", is_required=True)
            ChecklistItem.objects.create(template=tpl1, item_order=3, title="Upload wide-angle photo of trench before pour", field_type="Photo Upload", is_required=False)

            tpl2 = InspectionTemplate.objects.create(
                id="TPL-088",
                name="Environmental Site Perimeter Check",
                department="Environmental",
                status="Active",
                version="v1.0"
            )
            ChecklistItem.objects.create(template=tpl2, item_order=1, title="Verify perimeter sediment control barriers", field_type="Pass/Fail Toggle", is_required=True)
            ChecklistItem.objects.create(template=tpl2, item_order=2, title="Measure ambient noise level (dB)", field_type="Number Input", is_required=True)

        # 4. Seed Compliance Standards
        if not ComplianceStandard.objects.exists():
            ComplianceStandard.objects.create(category="Environmental Limits", key="noise_daytime_db", label="Daytime Max (07:00 - 19:00)", num_value=85.0, unit="dB", alert_level="Warning")
            ComplianceStandard.objects.create(category="Environmental Limits", key="noise_nighttime_db", label="Nighttime Max (19:00 - 07:00)", num_value=70.0, unit="dB", alert_level="Critical")
            ComplianceStandard.objects.create(category="Structural Tolerances", key="max_concrete_slump_in", label="Max Concrete Slump", num_value=6.0, unit="Inches", alert_level="Critical")
            ComplianceStandard.objects.create(category="Structural Tolerances", key="min_curing_temp_f", label="Min Curing Temp", num_value=40.0, unit="°F", alert_level="Warning")
            ComplianceStandard.objects.create(category="SLA Thresholds", key="permit_review_sla_days", label="Permit Review SLA", num_value=14.0, unit="Days", alert_level="Warning")
            ComplianceStandard.objects.create(category="SLA Thresholds", key="defect_rectification_sla_days", label="Defect Rectification SLA", num_value=5.0, unit="Days", alert_level="Critical")

        # 5. Seed Statutory Documents
        if not StatutoryDocument.objects.exists():
            StatutoryDocument.objects.create(code="NBC-2006/2020", name="National Building Code of Nigeria", connected_features=["Structural Tolerances", "Fire Safety", "Enforcement & Administration"])
            StatutoryDocument.objects.create(code="SON-NIS-87", name="SON NIS 87:2007 - Standard for Sandcrete & Masonry Blocks", connected_features=["Compressive Crushing Strength", "Block Dimension Tolerances", "Mix Ratios"])
            StatutoryDocument.objects.create(code="SON-NIS-11", name="SON NIS 11:2014 / NIS 444 - Portland Cement Quality", connected_features=["Cement Grade CEM I / II", "Setting Time", "Compressive Strength"])
            StatutoryDocument.objects.create(code="SON-NIS-117", name="SON NIS 117:2004 - Steel Bars for Concrete Reinforcement", connected_features=["Yield Strength (460/500 N/mm2)", "Steel Elongation", "Tensile Ratio"])
            StatutoryDocument.objects.create(code="SON-NIS-156", name="SON NIS 156 / NIS 820 - Structural Concrete Aggregates", connected_features=["Aggregate Grading", "Silt Content (<3%)", "Slump Tolerances"])
            StatutoryDocument.objects.create(code="SON-NIS-74", name="SON NIS 74 / NIS 378 - Building Electrical Installations", connected_features=["Copper Cable Rating", "Insulation Resistance", "Earthing Protection"])
            StatutoryDocument.objects.create(code="SON-NIS-384", name="SON NIS 384 - Building Plumbing & Sanitary Systems", connected_features=["Pipe Pressure Ratings", "Backflow Prevention", "Drainage Gradients"])
            StatutoryDocument.objects.create(code="URP-Law 2019/2024", name="Lagos State Urban & Regional Planning Law", connected_features=["Zoning Controls", "Setbacks", "Building Density"])
            StatutoryDocument.objects.create(code="LASBCA-Regs", name="Lagos State Building Control Agency Regulations", connected_features=["Building Stage Certification", "Material Testing Logs", "Stop-Work Orders"])
            StatutoryDocument.objects.create(code="LSEPA-2023", name="State Environmental Protection Guidelines", connected_features=["Noise Limits", "Effluent Discharge", "Air Quality"])

        # 6. Seed Notification Routing & Preferences
        if not NotificationRoutingRule.objects.exists():
            NotificationRoutingRule.objects.create(
                trigger_event="Critical Alerts",
                primary_recipient="Agency Director",
                sla_timeline="Within 15 mins",
                escalation_target="Permanent Secretary",
                is_active=True
            )
            NotificationRoutingRule.objects.create(
                trigger_event="Stop-Work Order",
                primary_recipient="Chief Inspector",
                sla_timeline="Within 2 hours",
                escalation_target="Agency Director",
                is_active=True
            )

        if not NotificationPreferenceCategory.objects.exists():
            NotificationPreferenceCategory.objects.create(category="Critical Safety Incidents", event_label="In-App Dashboard Alerts", in_app=True, email=True, sms=True, is_locked=True)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="New Permit Application", in_app=True, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="Technical Review Required", in_app=True, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Permits & Approvals", event_label="Approval Decision Finalized", in_app=False, email=True, sms=False, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Inspection Requested (Contractor)", in_app=True, email=True, sms=True, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Failed Inspection (NCR Generated)", in_app=True, email=True, sms=True, is_locked=False)
            NotificationPreferenceCategory.objects.create(category="Field Inspections", event_label="Inspection Passed", in_app=True, email=False, sms=False, is_locked=False)
