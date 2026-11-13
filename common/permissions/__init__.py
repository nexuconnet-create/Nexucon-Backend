"""
Role-scoped authorization (implementation plan §4: Organizational Hierarchy &
AI Authorization Scoping).

Scoping chain: User Identity (JWT / 2FA) -> Assigned Role -> Agency Scope ->
District Scope -> Project Scope -> Evidence Scope -> role-scoped AI insights.

`scoped_projects(user)` is the single helper every list/aggregation endpoint
should use so multi-tenant district isolation (plan §8 Security Baseline) is
enforced consistently.

Role standings (the written statement E3 asked for — kept in one place and
mirrored by apps/government/permissions.py):

* Agency Head  — head of a government agency (created at onboarding with
                 "admin"/"all.delete" permissions). Full visibility of
                 projects and full CRUD; the only government role that may
                 DELETE (apps.government.IsAgencyHead).
* Director     — State Headquarters Directorate: full visibility, full CRUD
                 except DELETE (apps.government.IsDirector).
* Inspector    — field inspector: the projects they are assigned to
                 (the `assigned_inspector_user` foreign key, or a real
                 inspection they conducted) union their district;
                 create/read/update, no DELETE.
* Client Developer — external developer organisation: only the projects
                 linked to their developer organisation; no government
                 standing.
* District staff (any other profile with a district) — read visibility of
                 their district's projects only.

Agency Head and Director see all projects because the platform has one
state-level project registry with no Project->Agency partition; every other
role is narrowed as above.
"""
from rest_framework.permissions import BasePermission

# Role names recognised by the platform (apps.government.Role / Profile).
ROLE_AGENCY_HEAD = 'Agency Head'
ROLE_DIRECTOR = 'Director'
ROLE_INSPECTOR = 'Inspector'
ROLE_CLIENT_DEVELOPER = 'Client Developer'

GOVERNMENT_ROLES = {ROLE_AGENCY_HEAD, ROLE_DIRECTOR, ROLE_INSPECTOR}


def get_profile(user):
    """Return the user's government Profile (may be None for client users)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'government_profile', None)


def user_role_name(user):
    profile = get_profile(user)
    return profile.role.name if profile and profile.role else ''


def user_is_agency_head(user):
    return user_role_name(user) == ROLE_AGENCY_HEAD


def user_is_director(user):
    return user_role_name(user) == ROLE_DIRECTOR or (user and user.is_superuser)


def user_is_inspector(user):
    return user_role_name(user) == ROLE_INSPECTOR


def user_is_state_hq(user):
    """State Headquarters Directorate — state-wide visibility."""
    profile = get_profile(user)
    if not profile:
        return user_is_director(user)
    return profile.is_state_hq or user_is_director(user)


def user_district(user):
    profile = get_profile(user)
    return profile.district if profile else None


def user_agency(user):
    profile = get_profile(user)
    return profile.agency if profile else None


IN_HOUSE_MOCK_NAMES = [
    'Eko Atlantic Marina Towers',
    'Victoria Island Financial Center',
    'Lekki Free Trade Zone Warehouse Complex',
    'Ikoyi Imperial Heights Luxury Condominiums',
    'Botanical Garden Road Ebute Metta, Lagos State.',
    'Reinforce Concrete Floor Slab on Laydown Area (Scope 01)',
    'Ladol Exxon Laydown Area',
    'test gps',
    'Lekki Horizon Tower',
]


def scoped_projects(user):
    """
    Projects visible to `user` under the multi-tenant hierarchy:
      Superuser           -> global administrative access
      SiteIQ in-house     -> in-house LASBCA / mock seed projects
      Agency Head / Staff -> strictly projects belonging to their own Agency or created by them
      District staff      -> projects in their district belonging to their agency
      Inspector           -> projects they are assigned to (`assigned_inspector_user`,
                             else a real inspection), else their district
      Client developer    -> projects linked to their developer organization
      Everyone else       -> none
    """
    from apps.projects.models import Project
    from django.db.models import Q

    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()

    # Global superuser administrative view
    if user.is_superuser:
        return Project.objects.all()

    user_email = (user.email or '').strip().lower()
    is_in_house_siteiq = (user_email == 'siteiq@nexucon.net')

    profile = get_profile(user)
    agency = profile.agency if profile else None
    role = user_role_name(user)
    role_lower = role.lower() if role else ''

    # 1. In-house Nexucon account (siteiq@nexucon.net) - sees in-house LASBCA records and seed test projects
    if is_in_house_siteiq:
        q_siteiq = (
            Q(client_contact__icontains='siteiq@nexucon.net') |
            Q(developer_email__iexact='siteiq@nexucon.net') |
            Q(created_by=user) |
            Q(name__in=IN_HOUSE_MOCK_NAMES)
        )
        if agency:
            q_siteiq |= Q(agency=agency) | Q(regulatory_authority__iexact=agency.name) | Q(regulatory_authority__iexact=agency.code)
        else:
            q_siteiq |= Q(agency__code='LASBCA') | Q(regulatory_authority__icontains='LASBCA')
        return Project.objects.filter(q_siteiq).distinct()

    # 2. Government Agency Staff & Agency Heads (external/new agency accounts, e.g. skprojects7061@gmail.com)
    # Strictly isolated per tenant agency — never leak in-house siteiq/LASBCA projects!
    if profile:
        q_agency = Q(created_by=user) | Q(client_contact__icontains=user.email) | Q(developer_email__iexact=user.email)
        if agency:
            q_agency |= Q(agency=agency)
            if agency.code and agency.code != 'LASBCA':
                q_agency |= Q(regulatory_authority__iexact=agency.code)
            if agency.name and agency.code != 'LASBCA':
                q_agency |= Q(regulatory_authority__iexact=agency.name)

        if 'inspector' in role_lower:
            q_agency |= Q(inspections__inspector=user) | Q(assigned_inspector_user=user)
            if profile.district:
                q_agency |= Q(district=profile.district)
        elif profile.district and agency:
            q_agency |= Q(district=profile.district, agency=agency)

        # Exclude in-house mock seed projects and in-house LASBCA projects from external agency users
        qs = Project.objects.filter(q_agency)
        if not (agency and agency.code == 'LASBCA'):
            qs = qs.exclude(
                (Q(name__in=IN_HOUSE_MOCK_NAMES) & ~Q(created_by=user)) |
                (Q(agency__code='LASBCA') & ~Q(created_by=user)) |
                (Q(client_contact__icontains='siteiq@nexucon.net') & ~Q(created_by=user))
            )
        return qs.distinct()

    # 3. Client developer: projects tied to their developer record.
    from apps.stakeholders.models import Developer
    developer = Developer.objects.filter(user=user).first()
    if developer:
        return Project.objects.filter(
            Q(developer_organization__iexact=developer.name) |
            Q(developer_name__iexact=developer.name) |
            Q(developer_email__iexact=user.email)
        ).distinct()

    return Project.objects.none()


class IsDirector(BasePermission):
    """Allows access only to Directors, Agency Heads (the two full-standing
    government roles — mirrors apps.government.IsDirector) or superusers."""
    message = 'Director-level role required.'

    def has_permission(self, request, view):
        return (user_is_director(request.user)
                or user_is_agency_head(request.user))


class IsGovernmentStaff(BasePermission):
    """Allows access to any government-staff user (has a Profile with a role)."""
    message = 'Government staff role required.'

    def has_permission(self, request, view):
        profile = get_profile(request.user)
        return bool(profile) or (request.user and request.user.is_superuser)


class IsDirectorOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return bool(request.user and request.user.is_authenticated)
        return user_is_director(request.user)


class HasProjectScope(BasePermission):
    """
    Object-level check: the requesting user's scoped_projects() must include
    the object's project. Use with `scope_field` on the view for models that
    reach the project via a non-standard field.
    """

    def has_object_permission(self, request, view, obj):
        project = getattr(obj, 'project', None)
        if project is None:
            return False
        return scoped_projects(request.user).filter(pk=project.pk).exists()
