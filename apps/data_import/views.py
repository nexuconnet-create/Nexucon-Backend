"""
Manual import API views (Inspector PWA — Part 3.3).

Routes, exactly as the spec lists them:

  POST import/upload/                store a CSV/JSON/PDF and attest to it
  POST import/<uuid:batch_id>/validate/   parse and check every row
  POST import/<uuid:batch_id>/commit/     write the rows, atomically
  GET  import/<uuid:batch_id>/status/     counts and errors
  GET  import/templates/<record_type>/    the CSV/JSON template

Every route is scoped to the caller: a batch belongs to its inspector, and a
colleague's batch is a 404 rather than a 403 — a 403 would confirm the batch
exists, which is a fact that belongs to the person who uploaded it.

``templates/`` is declared **before** the router-less ``<uuid:batch_id>``
routes. The converters differ (``str`` vs ``uuid``) so a literal can never be
swallowed by them, but the ordering is kept explicit because the same mistake
is already recorded as a fixed bug in ``scans/urls.py`` and
``inspections/urls.py``, and a regression test asserts it here.
"""
import logging

from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from apps.inspections.models import Inspection
from apps.projects.models import Project
from common.permissions import scoped_projects, user_role_name

from .models import ImportBatch
from .registry import KNOWN_RECORD_TYPES
from .serializers import (
    ImportBatchSerializer, ImportBatchStatusSerializer, UploadRequestSerializer,
)
from .services import ImportService, ImportServiceError
from .templates import build_template

logger = logging.getLogger(__name__)


def _record_audit(user, action, resource_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type='ImportBatch',
            resource_id=str(resource_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


def _scoped_batch(user, batch_id):
    """The caller's own batch, or None.

    Scoped to ``inspector=user`` rather than to the project: a batch is a
    person's unfinished upload, and another inspector working the same project
    has no business reading a file they have not committed — nor committing one
    under someone else's name.
    """
    return ImportBatch.objects.filter(pk=batch_id, inspector=user).first()


class ImportUploadView(APIView):
    """`POST import/upload/` — store a file and attest to it.

    Parses nothing. The response is the batch with its server-computed hash, so
    the uploader can compare it against the file they still have before
    spending a validation pass on it.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(request=UploadRequestSerializer,
                   responses={201: ImportBatchSerializer})
    def post(self, request):
        serializer = UploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        project = scoped_projects(request.user).filter(pk=data['project']).first()
        if project is None:
            return Response(
                {'detail': 'That project is not in your scope.'},
                status=status.HTTP_404_NOT_FOUND)

        inspection = None
        inspection_id = data.get('inspection')
        if inspection_id:
            inspection = Inspection.objects.filter(
                pk=inspection_id, project=project).first()
            if inspection is None:
                return Response(
                    {'detail': 'That inspection was not found in this project.'},
                    status=status.HTTP_404_NOT_FOUND)

        try:
            batch, created = ImportService.upload(
                user=request.user, project=project,
                uploaded_file=data['file'], request=request,
                import_type=data.get('import_type') or '',
                record_type=data.get('record_type') or '',
                inspection=inspection,
            )
        except ImportServiceError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        if created:
            _record_audit(request.user, 'import.batch.upload', batch.id, {
                'file_name': batch.file_name,
                'import_type': batch.import_type,
                'record_type': batch.record_type,
                'sha256_hash': batch.sha256_hash,
                'file_size_bytes': batch.file_size_bytes,
            })

        body = ImportBatchSerializer(
            batch, context={'deduplicated': not created}).data
        return Response(body, status=(
            status.HTTP_201_CREATED if created else status.HTTP_200_OK))


class ImportValidateView(APIView):
    """`POST import/<uuid:batch_id>/validate/` — check every row.

    Writes no registry rows. A batch with one bad row comes back ``FAILED``
    with ``invalid_record_count`` set, so the inspector sees how close the file
    was rather than only that it was rejected.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: ImportBatchStatusSerializer})
    def post(self, request, batch_id):
        batch = _scoped_batch(request.user, batch_id)
        if batch is None:
            return Response({'detail': 'Import batch not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            ImportService.validate(batch, request)
        except ImportServiceError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, 'import.batch.validate', batch.id, {
            'import_status': batch.import_status,
            'record_count': batch.record_count,
            'valid_record_count': batch.valid_record_count,
            'invalid_record_count': batch.invalid_record_count,
        })
        return Response(ImportBatchStatusSerializer(batch).data)


class ImportCommitView(APIView):
    """`POST import/<uuid:batch_id>/commit/` — write the rows.

    All-or-nothing, and the batch's status is written in the same transaction
    as the rows. A 409 here means nothing was written.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: ImportBatchSerializer})
    def post(self, request, batch_id):
        batch = _scoped_batch(request.user, batch_id)
        if batch is None:
            return Response({'detail': 'Import batch not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            ImportService.commit(batch, request)
        except ImportServiceError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)

        _record_audit(request.user, 'import.batch.commit', batch.id, {
            'record_count': batch.valid_record_count,
            'record_type': batch.record_type,
        })
        return Response(ImportBatchSerializer(batch).data)


class ImportStatusView(APIView):
    """`GET import/<uuid:batch_id>/status/` — counts, and what went wrong."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[OpenApiParameter('error_limit', int,
                                     description='Max errors to return (default 200).')],
        responses={200: ImportBatchStatusSerializer},
    )
    def get(self, request, batch_id):
        batch = _scoped_batch(request.user, batch_id)
        if batch is None:
            return Response({'detail': 'Import batch not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        raw_limit = request.query_params.get('error_limit')
        try:
            error_limit = int(raw_limit) if raw_limit else None
        except (TypeError, ValueError):
            return Response({'detail': 'error_limit must be an integer.'},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response(ImportBatchStatusSerializer(
            batch, context={'error_limit': error_limit}).data)


class _TemplateNegotiation(DefaultContentNegotiation):
    """Content negotiation that leaves ``?format=`` alone.

    ``format`` is DRF's own URL format override, and DRF acts on it *before* the
    view runs: an unrecognised value raises ``Http404`` looking for a renderer
    named after it. So ``?format=pdf`` — the exact request this endpoint is
    supposed to answer with a 400 naming CSV and JSON — returned a bare 404 from
    the framework instead, and ``?format=csv`` depended on a CSV renderer being
    installed rather than on this view.

    Here ``format`` belongs to the template file, not to the response body. The
    view sets the response's content type from the format it serves, so nothing
    is lost by ignoring the suffix; the query parameter then means what the
    endpoint's own documentation says it means.
    """

    def filter_renderers(self, renderers, format):
        return renderers


class ImportTemplateView(APIView):
    """`GET import/templates/<record_type>/` — the file to fill in.

    ``format`` is a query parameter rather than a path segment so that adding a
    format later does not add a route. Only ``csv`` and ``json`` are served;
    ``pdf`` is refused with the reason, because a PDF template would be a
    promise the import cannot keep.
    """

    permission_classes = [IsAuthenticated]
    content_negotiation_class = _TemplateNegotiation

    @extend_schema(
        parameters=[OpenApiParameter('format', str, description='csv (default) or json.')],
        responses={200: bytes},
    )
    def get(self, request, record_type):
        record_type = (record_type or '').strip().upper()
        fmt = request.query_params.get('format') or 'csv'
        try:
            content, filename, content_type = build_template(record_type, fmt)
        except ValueError as exc:
            return Response({'detail': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)

        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class ImportBatchListView(APIView):
    """`GET import/batches/` — the caller's own uploads, newest first."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter('status', str, description='Filter by import_status.'),
            OpenApiParameter('project', str, description='Filter by project id.'),
        ],
        responses={200: ImportBatchSerializer(many=True)},
    )
    def get(self, request):
        queryset = ImportBatch.objects.filter(inspector=request.user)

        wanted_status = (request.query_params.get('status') or '').strip().upper()
        if wanted_status:
            valid = {value for value, _ in ImportBatch.STATUS_CHOICES}
            if wanted_status not in valid:
                return Response(
                    {'detail': f'status must be one of {", ".join(sorted(valid))}.'},
                    status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(import_status=wanted_status)

        project_id = request.query_params.get('project')
        if project_id:
            if not Project.objects.filter(pk=project_id).exists():
                return Response({'detail': 'No such project.'},
                                status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(project_id=project_id)

        return Response(ImportBatchSerializer(queryset, many=True).data)


class ImportRecordTypeListView(APIView):
    """`GET import/record-types/` — what a file may declare itself to be.

    The list a client renders its upload form from, served from the registry so
    the form and the parser cannot drift apart.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: None})
    def get(self, request):
        from .registry import REGISTRY
        return Response([
            {
                'record_type': entry.record_type,
                'model_label': entry.model_label,
                'description': entry.description,
                'columns': list(entry.columns),
                'required_columns': list(entry.required),
                'groups_consecutive_rows': entry.groups,
            }
            for entry in (REGISTRY[key] for key in KNOWN_RECORD_TYPES)
        ])
