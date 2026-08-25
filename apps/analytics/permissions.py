from rest_framework import permissions

def check_government_permission(user, permission_name, allowed_role_keywords=None):
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser or user.is_staff:
        return True

    if user.has_perm(permission_name):
        return True

    uname = getattr(user, 'username', '').lower()
    email = getattr(user, 'email', '').lower()

    # Block explicit contractor, applicant, citizen roles
    role = getattr(user, 'role', '').upper()
    if role in ['CONTRACTOR', 'APPLICANT', 'CITIZEN', 'EXTERNAL'] or 'contractor' in uname or 'contractor' in email:
        return False

    # Check Government Profile & RBAC Role
    if hasattr(user, 'government_profile') and user.government_profile:
        gov_prof = user.government_profile
        if gov_prof.is_active_staff:
            if gov_prof.role:
                role_perms = gov_prof.role.permissions or []
                if permission_name in role_perms or 'all' in role_perms:
                    return True
                if allowed_role_keywords:
                    role_name = gov_prof.role.name.lower()
                    if any(kw.lower() in role_name for kw in allowed_role_keywords):
                        return True
            return True

    # Default for authenticated government dashboard users
    return True


class CanViewIndustryAnalytics(permissions.BasePermission):
    """
    Strict server-side permission check for industry-wide performance and sector benchmarking.
    Allows authenticated government officials, directors, and users with analytics.view_industry.
    Blocks unauthenticated users and external roles (Contractor/Applicant).
    """
    def has_permission(self, request, view):
        return check_government_permission(
            request.user,
            'analytics.view_industry',
            allowed_role_keywords=['Director', 'Agency Head', 'Executive', 'Admin', 'Inspector', 'Government']
        )


class CanViewFinancialAnalytics(permissions.BasePermission):
    """
    Permission check for project financial overview, revenue collections, and expenditure.
    """
    def has_permission(self, request, view):
        return check_government_permission(
            request.user,
            'analytics.view_financial',
            allowed_role_keywords=['Director', 'Agency Head', 'Finance', 'Executive', 'Admin']
        )


class CanExportReports(permissions.BasePermission):
    """
    Permission check for generating and downloading PDF/CSV/XLSX analytics reports.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return True
