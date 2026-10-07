"""
Operational zones (Districts) API — the state's zonal jurisdiction.

Routes:
  GET    districts/            the zone register
  POST   districts/            create a zone        (Director or above)
  GET    districts/<uuid>/     retrieve one
  PATCH  districts/<uuid>/     amend or retire one  (Director or above)

**There is deliberately no DELETE.** ``Profile.district`` and
``Project.district`` are both ``on_delete=SET_NULL``. A hard delete would
detach every project and every officer scoped to the zone without removing a
single one of them — the rows would survive, silently jurisdiction-less, and
nothing would record that the zone had ever existed. Retiring is
``PATCH {"is_active": false}``, which the model already supports and which both
``HQIntelligenceService.district_matrix()`` and the zone register already
filter on.

**Retiring a zone that still holds projects is refused**, with the count. A
retired district drops out of the HQ district heatmap, but its projects keep
pointing at it — so retiring one would take real projects off the map while
leaving them attached to the zone. Moving the projects first is the deliberate
act; the refusal is what makes it one.

Retiring a zone does **not** change what its officers can see:
``common.permissions.scoped_projects()`` scopes a district officer by the
``district`` foreign key, not by ``is_active``. Only the province of the zone
in listings changes.
"""
import logging

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from common.permissions import (
    IsGovernmentStaff, user_is_agency_head, user_is_director, user_role_name,
)

from .models import District
from .serializers import DistrictSerializer

logger = logging.getLogger(__name__)

# Every field a reader of the audit ledger would need to reconstruct what a
# zone was before someone changed it.
AUDITED_FIELDS = (
    'name', 'code', 'state_region', 'office_address',
    'lead_officer_name', 'lead_officer_email', 'is_active',
)


def _may_manage_zones(user):
    """Director, Agency Head, or superuser.

    The same bar this app already sets for issuing an inspector accreditation:
    a zone re-scopes what every officer inside it can see, so creating and
    retiring one belongs with the other jurisdiction-level acts rather than
    with ordinary record editing.
    """
    return user_is_director(user) or user_is_agency_head(user)


def _record_audit(user, action, obj_id, metadata=None):
    """Append an immutable AuditEvent; audit failures never break the request."""
    try:
        audit_user = user if (user and user.is_authenticated) else None
        AuditEvent.objects.create(
            user=audit_user,
            user_name=(audit_user.get_full_name() or audit_user.email) if audit_user else 'System',
            user_role=user_role_name(audit_user) if audit_user else 'System',
            action=action,
            resource_type='District',
            resource_id=str(obj_id),
            metadata=metadata or {},
        )
    except Exception:
        logger.exception('audit write failed for %s', action)


def _zone_queryset():
    """The register, with the two counts the serializer reports.

    Annotated rather than counted per row so listing the register stays one
    query however many zones exist.
    """
    return District.objects.annotate(
        project_count=Count('projects', distinct=True),
        staff_count=Count('staff', distinct=True),
    )


class DistrictListCreateView(APIView):
    """`GET districts/` and `POST districts/`."""

    permission_classes = [IsGovernmentStaff]

    @extend_schema(responses={200: DistrictSerializer(many=True)})
    def get(self, request):
        queryset = _zone_queryset()

        # Default to the live register: a retired zone is history, and the
        # client has to ask for it explicitly rather than being shown it by
        # default and having to filter it out.
        active = request.query_params.get('active', 'true').lower()
        if active in ('true', '1', 'yes'):
            queryset = queryset.filter(is_active=True)
        elif active in ('false', '0', 'no'):
            queryset = queryset.filter(is_active=False)
        # anything else (including 'all') is left unfiltered

        search = (request.query_params.get('search') or '').strip()
        if search:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(code__icontains=search) |
                Q(state_region__icontains=search) |
                Q(lead_officer_name__icontains=search)
            )

        return Response(DistrictSerializer(queryset[:500], many=True).data)

    @extend_schema(request=DistrictSerializer, responses={201: DistrictSerializer})
    def post(self, request):
        if not _may_manage_zones(request.user):
            return Response(
                {'detail': 'Creating an operational zone requires the Director '
                           'or Agency Head role.'},
                status=status.HTTP_403_FORBIDDEN)

        serializer = DistrictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        district = serializer.save()

        _record_audit(request.user, 'government.district.create', district.id, {
            'name': district.name,
            'code': district.code,
            'state_region': district.state_region,
        })
        return Response(DistrictSerializer(district).data,
                        status=status.HTTP_201_CREATED)


class DistrictDetailView(APIView):
    """`GET` and `PATCH` one zone. No `DELETE` — see the module docstring."""

    permission_classes = [IsGovernmentStaff]

    @extend_schema(responses={200: DistrictSerializer})
    def get(self, request, district_id):
        district = _zone_queryset().filter(pk=district_id).first()
        if not district:
            return Response({'detail': 'Operational zone not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(DistrictSerializer(district).data)

    @extend_schema(request=DistrictSerializer, responses={200: DistrictSerializer})
    def patch(self, request, district_id):
        if not _may_manage_zones(request.user):
            return Response(
                {'detail': 'Amending an operational zone requires the Director '
                           'or Agency Head role.'},
                status=status.HTTP_403_FORBIDDEN)

        district = District.objects.filter(pk=district_id).first()
        if not district:
            return Response({'detail': 'Operational zone not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        before = {field: getattr(district, field) for field in AUDITED_FIELDS}

        # Retiring a zone that still holds projects would drop those projects
        # off the HQ district heatmap while leaving them attached to the zone.
        # Refusing is what makes retiring a deliberate act.
        serializer = DistrictSerializer(district, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        retiring = (before['is_active'] and
                    serializer.validated_data.get('is_active') is False)
        if retiring:
            project_count = district.projects.count()
            if project_count:
                plural = project_count != 1
                return Response(
                    {'detail':
                        f'{district.name} still has {project_count} '
                        f'project{"s" if plural else ""} assigned to it. Move '
                        f'{"them" if plural else "it"} to another zone first — '
                        f'retiring the zone now would take '
                        f'{"those projects" if plural else "that project"} off '
                        f'the district heatmap while leaving '
                        f'{"them" if plural else "it"} attached to '
                        f'{district.code}.'},
                    status=status.HTTP_409_CONFLICT)

        district = serializer.save()

        changed = {field: {'from': before[field], 'to': getattr(district, field)}
                   for field in AUDITED_FIELDS
                   if getattr(district, field) != before[field]}
        # A PATCH that changes nothing is not an event worth recording, so the
        # ledger keeps meaning "someone altered this" rather than "someone
        # opened this and pressed save".
        if changed:
            _record_audit(request.user, 'government.district.amend', district.id, {
                'name': district.name,
                'code': district.code,
                'changed': changed,
            })
        return Response(DistrictSerializer(district).data)
