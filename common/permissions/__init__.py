"""
Role-scoped authorization (implementation plan §4: Organizational Hierarchy &
AI Authorization Scoping).

Scoping chain: User Identity (JWT / 2FA) -> Assigned Role -> Agency Scope ->
District Scope -> Project Scope -> Evidence Scope -> role-scoped AI insights.

`scoped_projects(user)` is the single helper every list/aggregation endpoint
should use so multi-tenant district isolation (plan §8 Security Baseline) is
enforced consistently.
"""
from rest_framework.permissions import BasePermission

# Role names recognised by the platform (apps.government.Role / Profile).
ROLE_DIRECTOR = 'Director'
ROLE_INSPECTOR = 'Inspector'
ROLE_CLIENT_DEVELOPER = 'Client Developer'

GOVERNMENT_ROLES = {ROLE_DIRECTOR, ROLE_INSPECTOR}


def get_profile(user):
    """Return the user's government Profile (may be None for client users)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'government_profile', None)


def user_role_name(user):
    profile = get_profile(user)
    return profile.role.name if profile and profile.role else ''


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


def scoped_projects(user):
    """
    Projects visible to `user` under the hierarchy:
      State HQ / Director -> all projects
      District staff      -> projects in their district
      Inspector           -> projects they are assigned to (inspector FK or
                             assigned_inspector field), else their district
      Client developer    -> projects linked to their developer organization
      Everyone else       -> none
    """
    from apps.projects.models import Project

    if user is None or not getattr(user, 'is_authenticated', False):
        return Project.objects.none()
    if user.is_superuser or user_is_state_hq(user):
        return Project.objects.all()

    profile = get_profile(user)
    district = profile.district if profile else None
    role = user_role_name(user)

    if role == ROLE_INSPECTOR:
        from django.db.models import Q
        inspector_name = str(user.get_full_name() or user.email)
        q = Q(inspections__inspector=user) | Q(assigned_inspector=inspector_name)
        if district:
            q |= Q(district=district)
        return Project.objects.filter(q).distinct()

    # Client developer: projects tied to their developer record.
    from apps.stakeholders.models import Developer
    developer = Developer.objects.filter(user=user).first()
    if developer:
        return Project.objects.filter(
            developer_organization=developer.name,
        ).distinct()

    if district:
        return Project.objects.filter(district=district)
    return Project.objects.none()


class IsDirector(BasePermission):
    """Allows access only to Directors (or superusers)."""
    message = 'Director-level role required.'

    def has_permission(self, request, view):
        return user_is_director(request.user)


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
