"""
Inspection Execution API views (implementation plan §5 Week 7).

Mobile inspection execution: dynamic checklists, mandatory GPS + timestamp
check-in, tamper-evident evidence submission with SHA-256 checksums,
cryptographic digital sign-off and independent integrity verification.
"""
import hashlib

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import scoped_projects

from .execution import ExecutionError, InspectionExecutionService
from .models import Inspection


def _get_scoped_inspection(request, inspection_id):
    return (Inspection.objects
            .filter(pk=inspection_id, project__in=scoped_projects(request.user))
            .select_related('project')
            .first())


class InspectionExecutionView(APIView):
    """
    GET /api/v1/inspections/{id}/execution/
    Current execution state: mandatory-checklist template, check-in status,
    submission + sign-off (with hash verification results).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, inspection_id):
        inspection = _get_scoped_inspection(request, inspection_id)
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        service = InspectionExecutionService
        data = {
            'inspection': inspection.inspection_reference,
            'status': inspection.status,
            'checklist': service.build_checklist(inspection),
            'gps_verified': inspection.gps_verified,
            'gps_latitude': inspection.gps_latitude,
            'gps_longitude': inspection.gps_longitude,
            'checkin_time': inspection.checkin_time,
        }
        submission = getattr(inspection, 'submission', None)
        if submission:
            data['submission'] = {
                'id': str(submission.id),
                'submitted_at': submission.submitted_at,
                'submission_hash': submission.submission_hash,
                'items': len(submission.checklist_results),
                'evidence_files': len(submission.evidence_files),
                'integrity_verified': service.verify_submission(submission),
            }
            signoff = getattr(submission, 'signoff', None)
            if signoff:
                data['signoff'] = {
                    'signed_by': signoff.signed_by_name,
                    'signed_at': signoff.signed_at,
                    'signoff_hash': signoff.signoff_hash,
                    'signature_verified': service.verify_signoff(signoff),
                }
        return Response(data)


class InspectionCheckinView(APIView):
    """
    POST /api/v1/inspections/{id}/execution/checkin/
    Mandatory GPS + timestamp check-in that starts the inspection.
    Body: {latitude, longitude, device_time?}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, inspection_id):
        inspection = _get_scoped_inspection(request, inspection_id)
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            inspection = InspectionExecutionService.checkin(
                inspection, request.user,
                latitude=request.data.get('latitude'),
                longitude=request.data.get('longitude'),
                device_time=request.data.get('device_time'),
            )
        except ExecutionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'status': inspection.status,
            'gps_verified': inspection.gps_verified,
            'gps_latitude': inspection.gps_latitude,
            'gps_longitude': inspection.gps_longitude,
            'checkin_time': inspection.checkin_time,
        })


class InspectionSubmitView(APIView):
    """
    POST /api/v1/inspections/{id}/execution/submit/
    Tamper-evident submission: mandatory GPS + device timestamp, item-by-item
    checklist results and evidence files. Evidence may be supplied either as
    previously uploaded references (file_name + sha256 + url) or as multipart
    uploads whose checksums are computed server-side from the real bytes.

    Body (JSON): {latitude, longitude, device_time?, checklist_results: [...],
                  evidence: [{file_name, sha256, url, file_type}], device_info?}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, inspection_id):
        inspection = _get_scoped_inspection(request, inspection_id)
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)

        evidence = list(request.data.get('evidence') or [])
        # Multipart evidence uploads: hash the actual bytes and store the file.
        if request.FILES:
            from django.core.files.storage import default_storage
            for uploaded in request.FILES.getlist('files') or []:
                digest = hashlib.sha256()
                for chunk in uploaded.chunks():
                    digest.update(chunk)
                stored_name = default_storage.save(
                    f'inspection_evidence/{inspection.id}/{uploaded.name}', uploaded)
                evidence.append({
                    'file_name': uploaded.name[:255],
                    'sha256': digest.hexdigest(),
                    'url': default_storage.url(stored_name),
                    'file_type': 'photo' if (uploaded.content_type or '').startswith('image/') else 'file',
                })

        try:
            submission = InspectionExecutionService.submit(
                inspection, request.user,
                checklist_results=request.data.get('checklist_results') or [],
                evidence_files=evidence,
                latitude=request.data.get('latitude'),
                longitude=request.data.get('longitude'),
                device_time=request.data.get('device_time'),
                device_info=request.data.get('device_info', '')
                             or request.META.get('HTTP_USER_AGENT', '')[:255],
            )
        except ExecutionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'submission_id': str(submission.id),
            'submission_hash': submission.submission_hash,
            'submitted_at': submission.submitted_at,
            'evidence_files': len(submission.evidence_files),
            'detail': 'Submission recorded. Complete the digital sign-off to finalise.',
        }, status=status.HTTP_201_CREATED)


class InspectionSignOffView(APIView):
    """
    POST /api/v1/inspections/{id}/execution/sign-off/
    Cryptographic digital sign-off by the inspector.
    Body: {signature_text?}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, inspection_id):
        inspection = _get_scoped_inspection(request, inspection_id)
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        submission = getattr(inspection, 'submission', None)
        if not submission:
            return Response({'detail': 'No submission to sign off — submit the inspection first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            signoff = InspectionExecutionService.sign_off(
                submission, request.user,
                signature_text=request.data.get('signature_text', ''),
            )
        except ExecutionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'signed_by': signoff.signed_by_name,
            'signed_at': signoff.signed_at,
            'signoff_hash': signoff.signoff_hash,
        }, status=status.HTTP_201_CREATED)


class InspectionVerifyView(APIView):
    """
    GET /api/v1/inspections/{id}/execution/verify/
    Independent integrity verification: recomputes the submission and sign-off
    hashes from stored content (tamper detection).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, inspection_id):
        inspection = _get_scoped_inspection(request, inspection_id)
        if not inspection:
            return Response({'detail': 'Inspection not found in your scope.'},
                            status=status.HTTP_404_NOT_FOUND)
        submission = getattr(inspection, 'submission', None)
        if not submission:
            return Response({'detail': 'No submission recorded for this inspection.'},
                            status=status.HTTP_404_NOT_FOUND)
        service = InspectionExecutionService
        result = {
            'submission_hash': submission.submission_hash,
            'submission_valid': service.verify_submission(submission),
        }
        signoff = getattr(submission, 'signoff', None)
        if signoff:
            result['signoff_hash'] = signoff.signoff_hash
            result['signoff_valid'] = service.verify_signoff(signoff)
        return Response(result)
