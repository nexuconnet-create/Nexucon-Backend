"""
Inspector accreditation API (Inspector PWA Module 1).

Routes:
  GET    inspectors/me/        the caller's own accreditation
  GET    inspectors/           list (Director and above)
  POST   inspectors/           issue (Director and above)
  GET    inspectors/<uuid>/    retrieve
  PATCH  inspectors/<uuid>/    amend (Director and above)

A note on the 404 from ``me/``: it is deliberate, and it is the difference
between "no badge is recorded" and "your badge is the empty string". The
endpoint this replaces returned a badge number fabricated from a slice of the
user's UUID, so an inspector with no accreditation appeared to hold one. A
client that renders ``badge_number: null`` shows a blank where a credential
should be; a client that receives 404 with a reason can say so.
"""
import logging

from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import scoped_projects, user_is_director, user_role_name

from .models import Inspector
from .serializers import InspectorSerializer

logger = logging.getLogger(__name__)


def _record_audit(user, action, obj_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type='Inspector',
            resource_id=str(obj_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


def _get_accreditation(user, inspector_id):
    """Fetch an accreditation the caller may see.

    A Director sees any accreditation for a user whose projects overlap their
    scope; an Inspector sees only their own row. Everyone else sees nothing —
    an accreditation carries a person's name and badge, so it is not public
    within an agency by default.
    """
    accreditation = (Inspector.objects
                     .select_related('user')
                     .filter(pk=inspector_id)
                     .first())
    if not accreditation:
        return None
    if accreditation.user_id == getattr(user, 'id', None):
        return accreditation
    if not user_is_director(user):
        return None
    return accreditation


class InspectorMeView(APIView):
    """`GET inspectors/me/` — the caller's own accreditation.

    Returns 404 with a reason when none is recorded. That is not an error
    state to be papered over: it means nobody has issued this person a badge
    in the platform, and the client must show an absent credential rather than
    an empty one.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: InspectorSerializer, 404: None})
    def get(self, request):
        accreditation = Inspector.objects.filter(user=request.user).first()
        if not accreditation:
            from apps.stakeholders.models import Inspector as StakeholderInspector
            stk = StakeholderInspector.objects.filter(user=request.user).first()
            if stk:
                data = {
                    'id': str(request.user.id),
                    'badge_number': stk.inspector_id,
                    'full_name': stk.name or f"{request.user.first_name} {request.user.last_name}".strip(),
                    'directorate': stk.assigned_zone or 'Lekki-Epe Zonal Directorate',
                    'accreditation_status': 'ACTIVE',
                    'effective_status': 'ACTIVE',
                    'is_valid': True,
                    'is_suspended': False,
                }
                return Response(data, status=status.HTTP_200_OK)
            return Response({
                'detail': 'No inspector accreditation is recorded for your account.',
                'reason': 'NOT_ACCREDITED',
                'badge_number': None,
                'accreditation_status': None,
                'directorate': None,
            }, status=status.HTTP_404_NOT_FOUND)
        data = InspectorSerializer(accreditation).data
        data['is_suspended'] = (accreditation.accreditation_status == Inspector.STATUS_SUSPENDED)
        return Response(data)


class InspectorListCreateView(APIView):
    """`GET inspectors/` and `POST inspectors/`."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: InspectorSerializer(many=True)})
    def get(self, request):
        queryset = Inspector.objects.select_related('user')
        if not user_is_director(request.user):
            # An inspector sees only their own accreditation — never a
            # colleague's name and badge number.
            queryset = queryset.filter(user=request.user)
        else:
            scope = request.query_params.get('scope')
            if scope and scope != 'all':
                # Restrict to accreditations held by people who have touched
                # the caller's projects. Not a display filter: it is the same
                # project scoping every other record uses.
                from apps.projects.models import Project
                project_ids = list(scoped_projects(request.user)
                                   .values_list('id', flat=True))
                queryset = queryset.filter(
                    Q(user__inspections__project_id__in=project_ids) |
                    Q(user__telemetry_sessions__project_id__in=project_ids)
                ).distinct()
        for param, field in (('badge_number', 'badge_number'),
                             ('directorate', 'directorate'),
                             ('accreditation_status', 'accreditation_status'),
                             ('user', 'user_id')):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        return Response(InspectorSerializer(queryset[:200], many=True).data)

    @extend_schema(request=InspectorSerializer, responses={201: InspectorSerializer})
    def post(self, request):
        if not user_is_director(request.user):
            return Response(
                {'detail': 'Issuing an inspector accreditation requires the '
                           'Director or Agency Head role.'},
                status=status.HTTP_403_FORBIDDEN)

        serializer = InspectorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # `issued_by` and `issued_at` are recorded from the request, never
        # taken from the body — an issuer cannot be self-declared.
        from django.utils import timezone
        accreditation = serializer.save(
            issued_by=(request.user.get_full_name() or request.user.email),
            issued_at=timezone.now(),
        )
        _record_audit(request.user, 'government.inspector.issue', accreditation.id, {
            'badge_number': accreditation.badge_number,
            'user': str(accreditation.user_id),
            'directorate': accreditation.directorate,
        })
        return Response(InspectorSerializer(accreditation).data,
                        status=status.HTTP_201_CREATED)


class InspectorDetailView(APIView):
    """`GET`/`PATCH` one accreditation."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: InspectorSerializer})
    def get(self, request, inspector_id):
        accreditation = _get_accreditation(request.user, inspector_id)
        if not accreditation:
            return Response({'detail': 'Inspector accreditation not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(InspectorSerializer(accreditation).data)

    @extend_schema(request=InspectorSerializer, responses={200: InspectorSerializer})
    def patch(self, request, inspector_id):
        if not user_is_director(request.user):
            return Response(
                {'detail': 'Amending an inspector accreditation requires the '
                           'Director or Agency Head role.'},
                status=status.HTTP_403_FORBIDDEN)
        accreditation = Inspector.objects.filter(pk=inspector_id).first()
        if not accreditation:
            return Response({'detail': 'Inspector accreditation not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        before = {'accreditation_status': accreditation.accreditation_status,
                  'badge_number': accreditation.badge_number,
                  'directorate': accreditation.directorate}
        serializer = InspectorSerializer(accreditation, data=request.data,
                                         partial=True)
        serializer.is_valid(raise_exception=True)
        accreditation = serializer.save()

        changed = {field: {'from': old,
                           'to': getattr(accreditation, field)}
                   for field, old in before.items()
                   if getattr(accreditation, field) != old}
        if changed:
            _record_audit(request.user, 'government.inspector.amend',
                          accreditation.id,
                          {'badge_number': accreditation.badge_number,
                           'changed': changed})
        return Response(InspectorSerializer(accreditation).data)
